"""Operations over a single-writer, restartable file workspace."""
from __future__ import annotations

import copy
import importlib.util
import os
import shutil
import zipfile
import uuid
from pathlib import Path

from .common import (WorkflowError, artifact, atomic_text, digest, fingerprint, inventory, now,
                     read_json, safe_id, stable_copy, verify, write_json)
from .config import DEFAULTS, PROJECT, configuration, source_url
from .documents import SUPPORTED, SKIPPED_MEDIA
from .evaluation import LEGACY_XLSX_FIELDS, XLSX_FIELDS, validate
from .export import export_workbook, verify_workbook
from .generation import prepare_generation, load_generation
from .model import ModelClient
from .parser import markdown, parse_document, verify_cache
from .reliability import FATAL_CODES, is_fatal, quality_summary
from .review import approve
from .storage import Store


class Workflow:
    def __init__(self, root=None, *, client_factory=None):
        self.store = Store(root or PROJECT / "workflow-data")
        self.client_factory = client_factory

    def _save_batch(self, batch):
        self.store.write(f'batches/{batch["id"]}/batch.json', batch)

    def _save_request(self, request):
        self.store.write(f'requests/{request["id"]}.json', request)

    def _response(self, batch, request_id=None, outcome=None):
        phase = batch["phase"]
        next_step = {"processing": "resume", "awaiting_review": "审核Markdown后明确确认", "awaiting_codex": "当前Codex读取固定资料，生成两份JSON并submit", "completed": "查看XLSX；业务审核在工作流外"}[phase]
        response = {"interface_version": 1, "batch_id": batch["id"], "request_id": request_id,
                    "revision": batch["revision"], "phase": phase,
                    "outcome": outcome or {"processing": "failed", "awaiting_review": "needs_user", "awaiting_codex": "needs_codex", "completed": "ok"}[phase],
                    "next_step": next_step, "errors": batch.get("errors", []), "warnings": batch.get("warnings", []),
                    "documents": [{"id": d["id"], "name": d["name"], "state": d["state"], "units_done": d.get("units_done", 0),
                                   "units_total": d.get("units_total"), "cache_hit": d.get("cache_hit", False),
                                   "working": str(self.store.path(d["working_path"])) if d.get("working_path") else None,
                                   'original':str(self.store.path(d['source']['path'])),
                                   'notes':str(self.store.path(d['notes_ref']['path'])) if d.get('notes_ref') else None,
                                   "issues": d.get("issues", []), "quality": d.get("quality", {"content_status": "not_recorded"}), "reading_delivery": d.get("reading_delivery")} for d in batch["documents"]],
                    "last_verified_at": batch.get("verified_at"), "user_copy_may_have_changed": phase == "completed"}
        known_quality = [d['quality'] for d in batch['documents'] if d.get('quality')]
        response['quality_summary'] = {
            'documents_with_quality': len(known_quality),
            'documents_quality_unknown': len(batch['documents'])-len(known_quality),
            'empty_units': sum(q['empty_units'] for q in known_quality),
            'needs_confirmation_units': sum(q['needs_confirmation_units'] for q in known_quality),
            'review_status': 'approved_version_exists' if batch.get('review') else 'not_recorded'}
        if batch.get("generation"):
            # Status reads saved paths, without scanning source hashes or model response files.
            meta = batch["generation_info"]
            response["generation"] = meta
            if batch.get("next_submission_id"):
                request = batch["next_submission_id"]
                inbox = self.store.path(f'batches/{batch["id"]}/generations/{meta["id"]}/inbox/{request}')
                response["submission"] = {"request_id": request, "candidate_file": str(inbox / "candidate.json"), "facts_file": str(inbox / "fact_checks.json")}
        if batch.get("export"):
            response["artifacts"] = batch["export_info"]
        if batch.get("validation"):
            response["validation"] = batch["validation"]
        return response

    def status(self, batch_id=None):
        if batch_id:
            result = self._response(self.store.batch(batch_id))
            result["writer_active"] = self.store.locked()
            return result
        directory = self.store.path("batches")
        records = []
        if directory.exists():
            for p in sorted(directory.glob("*/batch.json")):
                try:
                    b = self.store.batch(p.parent.name)
                    records.append({k: b[k] for k in ["id", "project", "phase", "revision"]})
                except WorkflowError as error:
                    records.append({"id": p.parent.name, "error": error.record()})
        return {"interface_version": 1, "outcome": "ok", "batches": records, "writer_active": self.store.locked()}

    def doctor(self):
        config = configuration()
        modules = {name: bool(importlib.util.find_spec(name)) for name in ["PIL", "pypdf", "openpyxl", "pptx", "docx", "lxml"]}
        tools = {name: Path(config[name]).is_file() for name in ["soffice", "pdftoppm"]}
        base = self.store.root if self.store.root.exists() else self.store.root.parent
        return {"interface_version": 1, "outcome": "ok", "modules": modules, "tools": tools,
                "network_sandbox_installed": Path("/usr/bin/sandbox-exec").exists(),
                "credential_env": config["api_key_env"], "credential_present": bool(os.getenv(config["api_key_env"])),
                "parser_model_default": config["model"], "local_ocr_available": bool(config.get("local_ocr_python") and Path(config["local_ocr_python"]).is_file()), "evaluation_executor": "current_codex",
                "prompts": {k: Path(config[k]).is_file() for k in ["parser_prompt", "evaluation_prompt"]},
                "disk_free_bytes": shutil.disk_usage(base).free, "writer_active": self.store.locked(),
                "note": "只读存在性检查；未联网、未转换、未验证实际认证"}

    def execute(self, operation, args):
        if not isinstance(args, dict):
            raise WorkflowError('REQUEST_FIELDS', '请求必须为JSON对象')
        if operation == "import-review":
            from .review_import import import_reading_set
            return import_reading_set(self, args)
        if operation == "status":
            return self.status(args.get("batch_id"))
        if operation == "doctor":
            return self.doctor()
        if operation not in {"start", "approve", "submit", "resume"} or not isinstance(args, dict):
            raise WorkflowError("INVALID_OPERATION", "未知操作或请求格式")
        request_id = safe_id(args.get("request_id"))
        allowed = {
            "start": {"request_id", "project", "sources", "urls", "config"},
            "approve": {"request_id", "batch_id", "expected_revision", "confirmed", "statement", "exclude", "exclude_units", "reuse_review", "reuse_map", "codex_model"},
            "submit": {"request_id", "batch_id", "expected_revision", "generation_id", "candidate_file", "facts_file"},
            "resume": {"request_id", "batch_id", "expected_revision", "mode", "retry_failed", "config", "codex_model"},
        }
        if set(args) - allowed[operation]:
            raise WorkflowError("REQUEST_FIELDS", "请求包含未知字段")
        with self.store.lock():
            batch = None if operation == "start" else self.store.batch(args.get("batch_id"))
            if batch and batch.get('last_request_id'):
                self._reconcile_last_result(batch)
            plan = inventory(args.get("sources")) if operation == "start" else None
            mutable = {}
            if operation == "submit":
                for key in ["candidate_file", "facts_file"]:
                    if not isinstance(args.get(key), str):
                        raise WorkflowError("SUBMISSION_MISSING", "必须同时提供两份提交文件")
                    mutable[key] = digest(Path(args[key]))
            if operation == "approve":
                for doc in batch["documents"]:
                    if doc.get("working_path"):
                        mutable[doc["id"]] = digest(self.store.path(doc["working_path"]))
            identity = fingerprint({"operation": operation, "args": args, "source_plan": plan, "mutable": mutable})
            request_path = self.store.path(f"requests/{request_id}.json")
            if request_path.exists():
                previous = self.store.read(f"requests/{request_id}.json")
                if previous["input_fingerprint"] != identity:
                    raise WorkflowError("REQUEST_CONFLICT", "同一request_id不能提交不同输入")
                if previous.get("result"):
                    if previous.get('status') == 'completed':
                        for ref in previous.get('result_refs', []):
                            verify(self.store.root, ref)
                    return previous["result"]
                batch_path = self.store.path(f'batches/{previous["batch_id"]}/batch.json')
                if not batch_path.exists() and previous.get('execution_operation') == 'start' and previous.get('initial_batch'):
                    batch = copy.deepcopy(previous['initial_batch'])
                    batch['pending'] = request_id
                    self._save_batch(batch)
                    return self._execute_registered(batch, previous)
                batch = self.store.batch(previous["batch_id"])
                if batch.get("last_request_id") == request_id:
                    for ref in batch.get('last_result_refs', []):
                        verify(self.store.root, ref)
                    previous["result"] = batch["last_result"]
                    previous['status'] = 'completed'
                    previous['result_refs'] = batch.get('last_result_refs', [])
                    self._save_request(previous)
                    return previous["result"]
                raise WorkflowError("REQUEST_INTERRUPTED", "该请求中断，请使用resume恢复")
            if batch is not None and (type(args.get("expected_revision")) is not int or args["expected_revision"] != batch["revision"]):
                raise WorkflowError("REVISION_CONFLICT", "批次revision已变化，请先查询当前状态")
            request = {"id": request_id, "operation": operation, "args": copy.deepcopy(args), "input_fingerprint": identity,
                       "created_at": now(), "batch_id": batch["id"] if batch else "b"+uuid.uuid4().hex,
                       "review_id": "r"+uuid.uuid4().hex, "generation_id": "g"+uuid.uuid4().hex,
                       "export_id": "e"+uuid.uuid4().hex, "submission_id": request_id, "plan": plan,
                       "xlsx_columns": list(XLSX_FIELDS),
                       "execution_operation": operation, "execution_args": copy.deepcopy(args),
                       "mutable_hashes": mutable, "checkpoints": {"working_hashes": dict(mutable)} if operation == 'approve' else {}, "status": "running"}
            if batch and batch.get("pending") and operation not in {"resume", "approve"}:
                raise WorkflowError("PENDING_OPERATION", "已有未完成步骤，请先resume")
            if operation == "resume" and args.get("mode", "continue") == "continue" and batch.get("pending"):
                parent = self.store.read(f'requests/{batch["pending"]}.json')
                for key in ["review_id", "generation_id", "export_id", "submission_id", "plan", "execution_operation", "execution_args", "checkpoints"]:
                    request[key] = copy.deepcopy(parent[key])
                request['mutable_hashes'] = copy.deepcopy(parent.get('mutable_hashes', {}))
                request['xlsx_columns'] = copy.deepcopy(parent.get('xlsx_columns', LEGACY_XLSX_FIELDS))
                request["resumes_request"] = parent["id"]
            if batch is None:
                config = configuration(args.get("config"))
                if not isinstance(args.get("project"), str) or not args["project"].strip():
                    raise WorkflowError("PROJECT_REQUIRED", "必须提供项目名称")
                batch = {"id": request["batch_id"], "project": args["project"], "config": config, "phase": "processing", "revision": 0,
                         "documents": [], "reviews": [], "errors": [], "created_at": request["created_at"]}
                request['initial_batch'] = copy.deepcopy(batch)
            self._save_request(request)
            batch["pending"] = request_id
            self._save_batch(batch)
            return self._execute_registered(batch, request)

    def _execute_registered(self, batch, request):
        def checkpoint(key, value):
            request['checkpoints'][key] = value
            self._save_request(request)
        failed = False
        try:
            outcome = self._run(batch, request, checkpoint)
            batch['errors'] = []
            batch['pending'] = None
        except WorkflowError as error:
            failed = True
            batch['errors'] = [error.record()]
            outcome = error.outcome
        except (OSError, UnicodeError) as error:
            failed = True
            batch['errors'] = [WorkflowError('IO_ERROR','本地读写失败，已完成检查点会保留',scope=type(error).__name__,outcome='failed').record()]
            outcome = 'failed'
        return self._finish(batch, request, outcome, failed=failed)

    def _finish(self, batch, request, outcome, failed=False):
        batch["revision"] += 1
        batch["verified_at"] = now()
        result = self._response(batch, request["id"], outcome)
        result["operation"] = request["operation"]
        batch["last_request_id"], batch["last_result"] = request["id"], result
        references = [batch[key] for key in ['sources', 'parser_prompt', 'evaluation_prompt', 'review', 'generation'] if batch.get(key)]
        references += [doc['source'] for doc in batch['documents']]
        if batch.get('export'):
            references.append(batch['export']['generated'])
        if batch.get('candidate'):
            references += list(batch['candidate'].values())
        request['result_refs'] = references
        batch['last_result_refs'] = references
        self._save_batch(batch)
        request.update(result=result, status="failed" if failed else "completed")
        self._save_request(request)
        return result

    def _reconcile_last_result(self, batch):
        prior = self.store.read(f'requests/{safe_id(batch["last_request_id"]) }.json')
        if not prior.get('result'):
            prior.update(result=batch['last_result'], result_refs=batch.get('last_result_refs', []),
                         status='failed' if batch['last_result'].get('errors') else 'completed')
            self._save_request(prior)

    def _prompts(self, batch, request, names=('parser_prompt','evaluation_prompt')):
        for name in names:
            target = self.store.path(f'batches/{batch["id"]}/sources/prompts/{request["id"]}/{name}.md')
            if not target.exists():
                stable_copy(Path(batch["config"][name]), target)
            batch[name] = artifact(self.store.root, target)
        self._save_batch(batch)

    def _parse_one(self, batch, doc, prompt, client, save_progress, render_semaphore=None):
        from PIL import UnidentifiedImageError
        from PIL.Image import DecompressionBombError
        config = batch['config']
        if doc['state'] == 'skipped':
            return
        if doc['extension'] not in SUPPORTED:
            doc.update(state='failed', issues=['UNSUPPORTED_FORMAT'])
            save_progress()
            return
        try:
            cached = parse_document(self.store, doc, config, prompt, save_progress, client, **({"render_semaphore": render_semaphore} if render_semaphore is not None else {}))
            text = markdown(doc, cached)
            auto = self.store.path(f'batches/{batch["id"]}/parsing/auto/{doc["cache_version"]}/{doc["name"]}.md')
            if not auto.exists():
                atomic_text(auto, text, replace=False)
            elif auto.read_text(encoding="utf-8") != text:
                raise WorkflowError("INTEGRITY_ERROR", "自动正文已变化")
            doc["auto_ref"] = artifact(self.store.root, auto)
            if not doc.get("working_path"):
                working = self.store.path(f'batches/{batch["id"]}/parsing/working/{doc["cache_version"]}/{doc["name"]}.md')
                if not working.exists():
                    stable_copy(auto, working, doc["auto_ref"]["sha256"])
                doc["working_path"] = str(working.relative_to(self.store.root))
            doc["quality"] = quality_summary(cached["units"])
            doc.update(state="parsed", units_done=len(cached['units']), units_total=len(cached['units']), issues=[issue for u in cached["units"] for issue in u["issues"]])
            notes = "\n\n".join(f'{u["label"]}\n{u["notes"]}' for u in cached["units"] if u["notes"])
            notes_path = auto.with_suffix('.notes.md')
            if notes_path.exists() and notes_path.read_text(encoding='utf-8') != notes:
                raise WorkflowError('INTEGRITY_ERROR', '已保存解析记录发生变化')
            if not notes_path.exists():
                atomic_text(notes_path, notes, replace=False)
            doc['notes_ref'] = artifact(self.store.root, notes_path)
            from .delivery import publish_draft, render_reading
            reading, provenance = render_reading(doc, cached['units'], verify(self.store.root, doc['source']))
            delivered = publish_draft(self.store.path(f'batches/{batch["id"]}/reading/{doc["id"]}'), '阅读稿.md', reading)
            provenance_path = Path(delivered['latest_auto']).with_name('provenance.json')
            if not provenance_path.exists():
                write_json(provenance_path, {'source': doc['source'], 'units': provenance})
            doc['reading_delivery'] = delivered
        except WorkflowError as error:
            doc.update(state="failed", issues=[error.record()])
            if is_fatal(error):
                save_progress()
                raise
        except DecompressionBombError:
            doc.update(state='failed', issues=[WorkflowError('CAPACITY_EXCEEDED','图像解压像素超过安全处理容量').record()])
        except (KeyError, ValueError, zipfile.BadZipFile, UnidentifiedImageError) as error:
            doc.update(state='failed', issues=[WorkflowError('INVALID_DOCUMENT','文件结构或内容无法读取',scope=type(error).__name__).record()])
        save_progress()

    def _parse(self, batch, request):
        prompt = verify(self.store.root, batch['parser_prompt']).read_text(encoding='utf-8')
        config = batch['config']
        if config.get('document_workers', 1) > 1:
            from .pipeline import parse_parallel
            parse_parallel(self, batch, request, prompt)
        else:
            client = self.client_factory(config) if self.client_factory else ModelClient(config, retry_terminal=request['args'].get('retry_failed', False))
            for doc in batch['documents']:
                self._parse_one(batch, doc, prompt, client, lambda: self._save_batch(batch))
        failures = [d['name'] for d in batch['documents'] if d['state'] == 'failed']
        self.store.write(f'batches/{batch["id"]}/parsing/parse-manifest.json', {"batch_id": batch["id"], "documents": batch["documents"]})
        if failures:
            batch["phase"] = "processing"
            raise WorkflowError("PARSE_INCOMPLETE", "部分资料未解析完成，请查询失败范围后重试或明确排除", scope=failures)
        batch["phase"] = "awaiting_review"
        return "needs_user"

    def _run(self, batch, request, checkpoint):
        op, args = request["execution_operation"], request["execution_args"]
        if op == "start":
            if not request["checkpoints"].get("sources_done"):
                urls = args.get("urls", {})
                if not isinstance(urls, dict) or set(urls) - {p["name"] for p in request["plan"]}:
                    raise WorkflowError("SOURCE_URL_MAPPING", "来源URL映射必须使用本批原件相对名称")
                self._prompts(batch, request)
                records = []
                for i, source in enumerate(request["plan"], 1):
                    url = source_url(urls.get(source["name"]), batch["config"])
                    target = self.store.path(f'batches/{batch["id"]}/sources/originals/{source["name"]}')
                    if not target.exists():
                        stable_copy(source["input_path"], target, source["sha256"])
                    if digest(target) != source["sha256"]:
                        raise WorkflowError("CONTENT_CHANGED", "已归档原件与排定输入不一致")
                    extension = Path(source["name"]).suffix.lower()
                    records.append({"id": f"d{i:04}", "name": source["name"], "extension": extension,
                                    "source": artifact(self.store.root, target), "url": url,
                                    "state": "skipped" if extension in SKIPPED_MEDIA else "pending",
                                    "issues": ["音视频或录制辅助文件已归档，跳过解析"] if extension in SKIPPED_MEDIA else []})
                batch["documents"] = records
                manifest = self.store.path(f'batches/{batch["id"]}/sources/source-manifest.json')
                if not manifest.exists():
                    write_json(manifest, {"schema_version": 1, "documents": records, "created_at": request["created_at"]}, replace=False)
                batch["sources"] = artifact(self.store.root, manifest)
                self._save_batch(batch)
                checkpoint("sources_done", True)
            return self._parse(batch, request)
        if op == "approve":
            if not request["checkpoints"].get("review"):
                ref = approve(self.store, batch, {**request, "args": args}, checkpoint)
                checkpoint("review", ref)
            ref = request["checkpoints"]["review"]
            if ref not in batch["reviews"]:
                batch["reviews"].append(ref)
            batch["review"] = ref
            review_record = read_json(verify(self.store.root, ref))
            for doc in batch['documents']:
                if doc['id'] in review_record['working_hashes']:
                    doc['last_confirmed_working_hash'] = review_record['working_hashes'][doc['id']]
            self._save_batch(batch)
            gen = prepare_generation(self.store, batch, ref, request["generation_id"], args.get("codex_model"))
            self._bind_generation(batch, gen)
            return "needs_codex"
        if op == "submit":
            return self._submit(batch, request, args, checkpoint)
        if op == "resume":
            mode = args.get("mode", "continue")
            if mode == "continue":
                if batch["phase"] == "completed":
                    verify(self.store.root, batch["export"]["generated"])
                    return "ok"
                return "needs_user" if batch["phase"] == "awaiting_review" else "needs_codex"
            if mode == "upgrade_parser":
                from .migration import upgrade_document
                if batch.get("review"):
                    raise WorkflowError("UPGRADE_REVIEWED", "已审核批次不能进行解析迁移")
                prompt = verify(self.store.root, batch["parser_prompt"]).read_text(encoding="utf-8")
                for doc in batch["documents"]:
                    if doc["state"] != "skipped":
                        upgrade_document(self.store, doc, batch["config"], prompt, request["id"])
                        self._save_batch(batch)
                return self._parse(batch, request)
            if mode == "reparse":
                if not request["checkpoints"].get("reset"):
                    merged = {k: v for k, v in batch["config"].items() if k in DEFAULTS}
                    merged.update(args.get("config", {}))
                    merged["cache_policy"] = "refresh"
                    batch["config"] = configuration(merged)
                    self._prompts(batch, request)
                    for doc in batch["documents"]:
                        if doc["state"] == "skipped":
                            continue
                        for key in ["cache_ref", "cache_key", "cache_version", "prepared_ref", "unit_refs", "auto_ref", "working_path"]:
                            doc.pop(key, None)
                        doc["state"] = "pending"
                    batch.update(phase="processing", generation=None, review=None, next_submission_id=None, candidate=None, export=None)
                    self._save_batch(batch)
                    checkpoint("reset", True)
                return self._parse(batch, request)
            if mode == "regenerate":
                if not batch.get("review"):
                    raise WorkflowError("REVIEW_CONFIRMATION_REQUIRED", "还没有审核版本")
                changes = args.get("config", {})
                permitted = {"single_step_questions", "multi_step_questions", "out_of_kb_questions", "ambiguous_questions", "min_analytical_multistep", "id_prefix", "capacity_profile", "evaluation_prompt"}
                if not isinstance(changes, dict) or set(changes) - permitted:
                    raise WorkflowError("CONFIG_ERROR", "重新出题只能修改评测配置；解析配置变化需重新解析")
                base = {k: v for k, v in batch["config"].items() if k in DEFAULTS}
                batch["config"] = configuration({**base, **changes})
                self._prompts(batch, request, names=('evaluation_prompt',))
                gen = prepare_generation(self.store, batch, batch["review"], request["generation_id"], args.get("codex_model"))
                self._bind_generation(batch, gen)
                return "needs_codex"
            if mode == "reexport":
                if not batch.get("candidate"):
                    raise WorkflowError("CANDIDATE_MISSING", "没有通过检查的候选可导出")
                gen, table = load_generation(self.store, batch["generation"])
                self._export(batch, request, gen, table, batch["candidate"], checkpoint)
                return "ok"
            raise WorkflowError("INVALID_MODE", "resume模式只能为continue/upgrade_parser/reparse/regenerate/reexport")
        raise WorkflowError("INVALID_OPERATION", "未知恢复步骤")

    def _bind_generation(self, batch, reference):
        gen, _ = load_generation(self.store, reference)
        batch.update(generation=reference, phase="awaiting_codex", candidate=None, validation=None, export=None,
                     next_submission_id=gen["first_submission_id"], repairs_used=0,
                     generation_info={"id": gen["id"], "prompt": str(self.store.path(gen["prompt"]["path"])),
                                      "evidence_table": str(self.store.path(gen["evidence_table"]["path"])),
                                      "documents": [str(self.store.path(d["body_ref"]["path"])) for d in gen["documents"]],
                                      "capacity_check": gen["capacity_check"], "codex_model": gen["codex_model"]})
        self._save_batch(batch)

    def _submit(self, batch, request, args, checkpoint):
        if not batch.get("generation") or args.get("generation_id") != batch["generation_info"]["id"]:
            raise WorkflowError("GENERATION_MISMATCH", "候选不属于当前生成任务")
        gen, table = load_generation(self.store, batch["generation"])
        submission = request["submission_id"]
        snapshots = self.store.path(f'batches/{batch["id"]}/generations/{gen["id"]}/submissions/{submission}')
        pair = request["checkpoints"].get("pair")
        if not pair:
            if batch["phase"] != "awaiting_codex" or submission != batch.get("next_submission_id"):
                raise WorkflowError("SUBMISSION_CONFLICT", "该生成任务不再接受此提交标识")
            expected_dir = self.store.path(f'batches/{batch["id"]}/generations/{gen["id"]}/inbox/{submission}')
            inputs = {"candidate": Path(args["candidate_file"]), "facts": Path(args["facts_file"])}
            for name, filename in [("candidate", "candidate.json"), ("facts", "fact_checks.json")]:
                if os.path.abspath(inputs[name]) != str(expected_dir / filename):
                    raise WorkflowError("SUBMISSION_PATH", "提交文件必须来自当前生成任务的专属目录")
            hashes = {'candidate':request.get('mutable_hashes', {}).get('candidate_file'),
                      'facts':request.get('mutable_hashes', {}).get('facts_file')}
            if not all(isinstance(value, str) and len(value)==64 for value in hashes.values()):
                raise WorkflowError('INTEGRITY_ERROR', '提交缺少请求登记时的文件指纹')
            if any(digest(path) != hashes[name] for name,path in inputs.items()):
                raise WorkflowError('CONTENT_CHANGED', '请求登记后提交文件已变化')
            refs = {}
            for name, source in inputs.items():
                target = snapshots / (name + ".json")
                if not target.exists():
                    stable_copy(source, target, hashes[name])
                if digest(target) != hashes[name]:
                    raise WorkflowError("CONTENT_CHANGED", "已暂存的提交内容变化，请使用新提交版本")
                refs[name] = artifact(self.store.root, target)
            try:
                candidate = read_json(verify(self.store.root, refs["candidate"]))
                facts = read_json(verify(self.store.root, refs["facts"]))
                errors = validate(candidate, facts, gen, table, include_quotes=request.get('xlsx_columns', LEGACY_XLSX_FIELDS)==XLSX_FIELDS)
                errors += self._repair_scope_errors(candidate, facts, batch.get('validation'))
            except WorkflowError as error:
                errors = [error.record()]
            if any(digest(path) != hashes[name] for name, path in inputs.items()):
                raise WorkflowError("CONTENT_CHANGED", "整组复核发现提交文件变化，未绑定候选")
            report = snapshots / "validation.json"
            write_json(report, {"errors": errors, "mechanical_only": True, "pair": refs})
            refs["validation"] = artifact(self.store.root, report)
            if errors:
                batch["repairs_used"] = batch.get("repairs_used", 0) + (0 if submission == gen["first_submission_id"] else 1)
                batch["validation"] = {"path": str(report), "errors": errors, "previous_candidate": str(self.store.path(refs["candidate"]["path"])),
                                       "previous_facts": str(self.store.path(refs["facts"]["path"])),
                                       'previous_candidate_ref':refs['candidate'], 'previous_facts_ref':refs['facts'],
                                       'repair_base': (batch.get('validation') or {}).get('repair_base') or {'candidate':refs['candidate'],'facts':refs['facts']},
                                       'allowed_ids':(batch.get('validation') or {}).get('allowed_ids', sorted({e['question_id'] for e in errors if isinstance(e.get('question_id'),str) and e.get('code')!='UNRELATED_REPAIR'})),
                                       'global_repair':(batch.get('validation') or {}).get('global_repair', any(e.get('code') in {'QUESTION_COUNT','CATEGORY_COUNTS','REASONING_COUNTS','DOCUMENT_UNCOVERED'} for e in errors))}
                can_repair = batch["repairs_used"] < batch["config"]["max_repairs"]
                next_id = "s"+uuid.uuid4().hex if can_repair else None
                batch["next_submission_id"] = next_id
                if next_id:
                    self.store.path(f'batches/{batch["id"]}/generations/{gen["id"]}/inbox/{next_id}').mkdir(parents=True)
                return "needs_codex" if can_repair else "needs_user"
            pair = refs
            checkpoint("pair", pair)
            batch["candidate"] = pair
            batch["validation"] = {"path": str(report), "errors": []}
            self._save_batch(batch)
        batch['candidate'] = pair
        self._save_batch(batch)
        self._export(batch, request, gen, table, pair, checkpoint)
        return "ok"

    def _repair_scope_errors(self, candidate, facts, previous):
        if not previous or not previous.get('errors') or not isinstance(candidate, list) or not isinstance(facts, list):
            return []
        try:
            baseline = previous.get('repair_base') or {'candidate':previous['previous_candidate_ref'],'facts':previous['previous_facts_ref']}
            old = read_json(verify(self.store.root, baseline['candidate']))
            old_facts = read_json(verify(self.store.root, baseline['facts']))
        except KeyError as exc:
            raise WorkflowError('INTEGRITY_ERROR', '上一候选缺少完整引用记录') from exc
        except WorkflowError as error:
            if error.code == 'INVALID_JSON':
                return []
            raise
        if not isinstance(old, list) or not isinstance(old_facts, list):
            return []
        # These errors can require changing additional questions to restore a global invariant.
        global_codes = {'QUESTION_COUNT', 'CATEGORY_COUNTS', 'REASONING_COUNTS', 'DOCUMENT_UNCOVERED'}
        if previous.get('global_repair', any(error.get('code') in global_codes for error in previous['errors'])):
            return []
        affected = set(previous.get('allowed_ids', [error.get('question_id') for error in previous['errors'] if isinstance(error.get('question_id'), str) and error.get('code')!='UNRELATED_REPAIR']))
        old_by_id = {item['id']: item for item in old if isinstance(item, dict) and isinstance(item.get('id'), str)}
        def grouped(records, identifier):
            return [r for r in records if isinstance(r, dict) and r.get('question_id') == identifier]
        errors = []
        unknown_fact_schema = any(e.get('code') == 'FACT_SCHEMA' for e in previous['errors'])
        for item in candidate:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            identifier = item['id']
            if identifier not in affected and identifier in old_by_id:
                changed = item != old_by_id[identifier]
                changed |= not unknown_fact_schema and grouped(facts, identifier) != grouped(old_facts, identifier)
                if changed:
                    errors.append({'code':'UNRELATED_REPAIR','question_id':identifier,'field':None,
                                   'message':'局部修正改变了无关题目或事实记录，请保留原值'})
        return errors

    def _export(self, batch, request, gen, table, pair, checkpoint):
        candidate = read_json(verify(self.store.root, pair["candidate"]))
        facts = read_json(verify(self.store.root, pair["facts"]))
        columns = request.get('xlsx_columns', LEGACY_XLSX_FIELDS)
        if columns not in (LEGACY_XLSX_FIELDS, XLSX_FIELDS):
            raise WorkflowError('EXPORT_SCHEMA', '请求中保存的导出列契约无效')
        errors = validate(candidate, facts, gen, table, include_quotes=columns==XLSX_FIELDS)
        if errors:
            raise WorkflowError("CANDIDATE_INVALID", "已固定候选不再满足完整检查")
        directory = self.store.path(f'batches/{batch["id"]}/exports/{request["export_id"]}')
        generated = directory / "generated.xlsx"
        if not request["checkpoints"].get("generated"):
            if generated.exists():
                checks = verify_workbook(generated, candidate, facts, gen, table, columns=columns)
            else:
                checks = export_workbook(generated, candidate, facts, gen, table, columns=columns)
            checkpoint("generated", {"reference": artifact(self.store.root, generated), "checks": checks})
        generated_ref = request["checkpoints"]["generated"]["reference"]
        if request['checkpoints']['generated']['checks']['columns'] != columns:
            raise WorkflowError('EXPORT_MISMATCH', '导出检查点列数与请求契约不一致')
        verify(self.store.root, generated_ref)
        deliverable = self.store.path(f'batches/{batch["id"]}/deliverables/{request["export_id"]}.xlsx')
        if not deliverable.exists():
            stable_copy(generated, deliverable, generated_ref["sha256"])
        if digest(deliverable) != generated_ref["sha256"]:
            raise WorkflowError("DELIVERABLE_CHANGED", "交付副本已变化，不覆盖；请明确重新导出")
        export = {"schema_version": 1, "id": request["export_id"], "generation": batch["generation"], "candidate": pair,
                  "generated": generated_ref, "deliverable": artifact(self.store.root, deliverable), "checks": request["checkpoints"]["generated"]["checks"]}
        manifest = directory / "export-manifest.json"
        if not manifest.exists():
            write_json(manifest, export, replace=False)
        elif read_json(manifest) != export:
            raise WorkflowError('INTEGRITY_ERROR', '已发布导出记录与本次产物不一致')
        history = batch.setdefault('exports', [])
        export_ref = artifact(self.store.root, manifest)
        if export_ref not in history:
            history.append(export_ref)
        batch.update(export=export, export_info={"original": str(generated), "deliverable": str(deliverable), "manifest": str(manifest)},
                     phase="completed", next_submission_id=None)
