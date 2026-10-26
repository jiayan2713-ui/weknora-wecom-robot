"""Human confirmation is tied to exact editable inputs and immutable snapshots."""
from pathlib import Path

from .common import WorkflowError, artifact, atomic_text, digest, fingerprint, read_json, stable_copy, verify, write_json, now
from .parser import reviewed_units, verify_cache


def approve(store, batch, request, checkpoint):
    args = request["args"]
    if args.get("confirmed") is not True or not isinstance(args.get("statement"), str) or not args["statement"].strip():
        raise WorkflowError("REVIEW_CONFIRMATION_REQUIRED", "需用户明确确认本批解析内容，并提供确认声明")
    excluded = args.get("exclude", {})
    excluded_units = args.get("exclude_units", {})
    known = {d["id"] for d in batch["documents"]}
    if not isinstance(excluded, dict) or set(excluded) - known or any(not isinstance(v, str) or not v.strip() for v in excluded.values()):
        raise WorkflowError("REVIEW_SCOPE_INVALID", "排除项必须为本批文档及非空理由")
    if not isinstance(excluded_units, dict) or set(excluded_units) - known:
        raise WorkflowError("REVIEW_SCOPE_INVALID", "单元排除范围无效")
    if any(not isinstance(value, dict) for value in excluded_units.values()):
        raise WorkflowError("REVIEW_SCOPE_INVALID", "单元排除项必须包含标识与理由")
    selected = [d for d in batch["documents"] if d["id"] not in excluded and d["state"] != "skipped"]
    if not selected or any(d["state"] != "parsed" for d in selected):
        raise WorkflowError("PARSE_INCOMPLETE", "纳入资料中仍有未完成解析的文件；不能自动忽略")
    reuse_map = args.get("reuse_map", {})
    if not isinstance(reuse_map, dict) or set(reuse_map) - {d["id"] for d in selected}:
        raise WorkflowError("REVIEW_SCOPE_INVALID", "沿用文件对应关系无效")
    old_review = None
    if reuse_map:
        reference = args.get("reuse_review")
        if not isinstance(reference, dict) or set(reference) != {"batch_id", "review_id"}:
            raise WorkflowError("REVIEW_SCOPE_INVALID", "沿用需明确指定批次与审核版本")
        from .common import safe_id
        old_path = f'batches/{safe_id(reference["batch_id"])}/reviews/{safe_id(reference["review_id"])}/review-manifest.json'
        old_batch = store.batch(reference["batch_id"])
        old_ref = next((item for item in old_batch.get("reviews", []) if item["path"] == old_path), None)
        if not old_ref:
            raise WorkflowError("REUSE_REVIEW_MISMATCH", "来源审核版没有已登记的完整版本引用")
        old_review = read_json(verify(store.root, old_ref))
    directory = store.path(f'batches/{batch["id"]}/reviews/{request["review_id"]}')
    directory.mkdir(parents=True, exist_ok=True)
    if "working_hashes" not in request["checkpoints"]:
        hashes = {d["id"]: digest(store.path(d["working_path"])) for d in selected}
        checkpoint("working_hashes", hashes)
    expected = request["checkpoints"]["working_hashes"]
    documents = []
    for doc in selected:
        verify(store.root, doc["source"])
        cache = verify_cache(store, doc["cache_ref"])
        for key in ['reading_input', 'provenance_input']:
            if doc.get(key):
                imported = doc[key]
                verify(store.root, imported['archived'])
                if digest(Path(imported['path'])) != imported['sha256']:
                    raise WorkflowError('CONTENT_CHANGED', '已导入阅读稿或溯源已改变，需按新版本重新导入', scope=doc['name'])
        if doc.get('reading_delivery'):
            delivered = doc['reading_delivery']
            reading_copy = Path(delivered['working'])
            if not reading_copy.exists() or digest(reading_copy) != delivered.get('published_sha256'):
                raise WorkflowError('READING_EDITED', '阅读稿已有修改，请先import-review绑定该阅读版本；不能确认旧工作稿', scope=doc['name'])
        working = store.path(doc["working_path"])
        if digest(working) != expected[doc["id"]]:
            raise WorkflowError("CONTENT_CHANGED", "审核内容在确认后变化", scope=doc["name"])
        units = reviewed_units(working.read_text(encoding="utf-8"), doc, cache)
        inherited_exclusions = {}
        origin = None
        inherited_issues = doc.get('issues', [])
        if doc["id"] in reuse_map:
            previous = next((d for d in old_review["documents"] if d["id"] == reuse_map[doc["id"]]), None)
            if not previous or previous["source_hash"] != doc["source"]["sha256"] or previous["cache_key"] != doc["cache_key"]:
                raise WorkflowError("REUSE_REVIEW_MISMATCH", "原件或解析条件与旧审核版不一致")
            if previous["auto_hash"] != doc["auto_ref"]["sha256"] and previous["unit_body_hash"] != fingerprint(cache["units"]):
                raise WorkflowError("REUSE_REVIEW_MISMATCH", "自动正文或位置关系变化")
            baselines = {doc.get("last_confirmed_working_hash"), doc["auto_ref"]["sha256"]}
            if expected[doc["id"]] not in baselines:
                raise WorkflowError("WORKING_EDITED", "当前工作稿有人工修改；沿用不会覆盖这些内容")
            verify(store.root, previous["body_ref"])
            units = previous["units"]
            inherited_exclusions = previous.get("excluded_units", {})
            inherited_issues = previous.get('issues', inherited_issues)
            origin = {**args["reuse_review"], "document_id": previous["id"]}
        removed = {**inherited_exclusions, **excluded_units.get(doc["id"], {})}
        ids = {u["id"] for u in cache['units']}
        if set(removed) - ids or any(not isinstance(v, str) or not v.strip() for v in removed.values()):
            raise WorkflowError("REVIEW_SCOPE_INVALID", "单元排除理由或标识无效")
        included = [u for u in units if u["id"] not in removed]
        if not included or any(not u["body"].strip() or "本单元未取得解析正文，请对照原件核查。" in u["body"] for u in included):
            raise WorkflowError("EMPTY_REVIEW_CONTENT", "纳入范围存在空正文；需审核补齐或明确排除")
        output = directory / "documents" / (doc["name"] + ".md")
        # Rebinding only the program-owned source line keeps the reviewed body byte-for-byte intact.
        text = "\n\n".join(f'<!-- docflow:{u["id"]} -->\n来源：{doc["name"]}；{u["label"]}\n\n{u["body"]}\n<!-- /docflow:{u["id"]} -->' for u in included) + "\n"
        if output.exists():
            if output.read_text(encoding="utf-8") != text:
                raise WorkflowError("INTEGRITY_ERROR", "已有暂存审核正文不一致")
        else:
            atomic_text(output, text, replace=False)
        documents.append({"id": doc["id"], "name": doc["name"], "source_hash": doc["source"]["sha256"],
                          "source_ref": doc["source"], "url": doc["url"], "working_hash": expected[doc["id"]],
                          "cache_key": doc["cache_key"], "auto_hash": doc["auto_ref"]["sha256"],
                          "unit_body_hash": fingerprint(cache["units"]), "body_ref": artifact(store.root, output),
                          "units": included, "excluded_units": removed, "reuse_origin": origin, 'issues': inherited_issues, 'reading_input': doc.get('reading_input'),
                          'provenance_input': doc.get('provenance_input')})
    for doc in selected:
        for key in ['reading_input', 'provenance_input']:
            if doc.get(key) and digest(Path(doc[key]['path'])) != doc[key]['sha256']:
                raise WorkflowError('CONTENT_CHANGED', '导入阅读版本在审核发布前改变')
        if digest(store.path(doc["working_path"])) != expected[doc["id"]]:
            raise WorkflowError("CONTENT_CHANGED", "整组复核发现审核稿变化；未发布审核版本")
    manifest = {"schema_version": 1, "id": request["review_id"], "batch_id": batch["id"], "documents": documents,
                "excluded": excluded, "statement": args["statement"], "working_hashes": expected,
                "created_at": request["created_at"]}
    target = directory / "review-manifest.json"
    if target.exists():
        if read_json(target) != manifest:
            raise WorkflowError("INTEGRITY_ERROR", "审核版本与当前确认不一致")
    else:
        write_json(target, manifest, replace=False)
    return artifact(store.root, target)


def check_review(store, batch, reference, *, check_working=True):
    review = read_json(verify(store.root, reference))
    current = {d["id"]: d for d in batch["documents"]}
    for item in review["documents"]:
        verify(store.root, item["body_ref"])
        verify(store.root, item["source_ref"])
        active = current.get(item['id'])
        if not active or active['source']['sha256'] != item['source_hash'] or active.get('cache_key') != item['cache_key']:
            raise WorkflowError('CONTENT_CHANGED', '当前解析版本不再符合活动审核版本，请重新确认')
        if active.get('auto_ref', {}).get('sha256') != item['auto_hash']:
            raise WorkflowError('CONTENT_CHANGED', '自动正文版本已变化，请重新确认')
        if check_working and digest(store.path(current[item["id"]]["working_path"])) != review["working_hashes"][item["id"]]:
            raise WorkflowError("CONTENT_CHANGED", "已确认的工作稿发生变化，请重新审核确认", scope=item["name"])
    return review
