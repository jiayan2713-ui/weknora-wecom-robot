"""Freeze the exact reading set, prompt and evidence allow-list for Codex."""
import json
import re
import uuid

from .common import WorkflowError, artifact, atomic_text, read_json, verify, write_json
from .config import source_url
from .review import check_review


def prepare_generation(store, batch, review_ref, generation_id, codex_model):
    review = check_review(store, batch, review_ref)
    config = batch["config"]
    directory = store.path(f'batches/{batch["id"]}/generations/{generation_id}')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "generation-manifest.json"
    if target.exists():
        manifest = read_json(target)
        for ref in manifest["artifacts"]:
            verify(store.root, ref)
        return artifact(store.root, target)
    if not isinstance(codex_model, str) or not codex_model.strip():
        raise WorkflowError("MODEL_RECORD_REQUIRED", "请记录当前Codex实际模型；不可取得时明确填写unavailable")
    table = {}
    for doc in review["documents"]:
        url = source_url(doc["url"], config)
        for unit in doc["units"]:
            key = doc["name"] + ".md#" + unit["label"]
            if key in table:
                raise WorkflowError("EVIDENCE_COLLISION", "来源位置存在歧义，不能生成允许表")
            table[key] = {"document_id": doc["id"], "unit_id": unit["id"], "body": unit["body"],
                          "original_name": doc["name"], "location": unit["label"], "url": url}
    # Excel bounds for fixed metadata are checked before any candidate is generated.
    from .evaluation import SUPPORTED_CANDIDATE_SCHEMAS, excel_error
    for value in table.values():
        for name in ["original_name", "location", "url"]:
            if value[name] and excel_error(value[name]):
                raise WorkflowError("SOURCE_NOT_REPRESENTABLE", "固定来源信息无法无损写入Excel", scope=name)
    table_path = directory / "evidence-table.json"
    write_json(table_path, table)
    template = verify(store.root, batch["evaluation_prompt"]).read_text(encoding="utf-8")
    if "## 模型提示词" not in template:
        raise WorkflowError("PROMPT_INVALID", "评测提示词缺少模型提示词分区")
    declarations = re.findall(r"^候选规则：(.*)$", template.split("## 模型提示词", 1)[0], re.MULTILINE)
    # Pre-v1.4 templates have no declaration; preserve their v2 behavior.
    candidate_schema = declarations[0].strip() if declarations else "candidate_v2"
    if len(declarations) > 1 or candidate_schema not in SUPPORTED_CANDIDATE_SCHEMAS:
        raise WorkflowError("PROMPT_INVALID", "评测提示词候选规则版本未知或重复")
    model_template = template.split("## 模型提示词", 1)[1].strip()
    values = {k: config[k] for k in ["total_questions", "in_kb_questions", "single_step_questions", "multi_step_questions",
              "out_of_kb_questions", "ambiguous_questions", "min_analytical_multistep", "id_prefix"]}
    values.update(review_root=str(store.path(f'batches/{batch["id"]}/reviews/{review["id"]}/documents')),
                  required_documents="\n".join(f'{i+1}. {d["name"]}.md' for i, d in enumerate(review["documents"])),
                  document_count=len(review["documents"]), evidence_table_path=str(table_path),
                  excluded_scope=json.dumps({"documents": review["excluded"], "units": {d["name"]: d["excluded_units"] for d in review["documents"] if d["excluded_units"]}}, ensure_ascii=False))
    variables = set(re.findall(r"\{\{([a-z_]+)\}\}", model_template))
    if variables != set(values):
        raise WorkflowError("PROMPT_VARIABLES_INVALID", "评测模板变量与约定不一致")
    expanded = re.sub(r"\{\{([a-z_]+)\}\}", lambda match: str(values[match.group(1)]), model_template)
    prompt_path = directory / "prompt.md"
    atomic_text(prompt_path, expanded)
    size = len(expanded.encode()) + len(table_path.read_bytes()) + sum(d["body_ref"]["size"] for d in review["documents"])
    profile = config["capacity_profile"]
    if profile and size + profile["reserved_output_tokens"] + profile["safety_reserve_tokens"] > profile["context_budget"]:
        raise WorkflowError("CAPACITY_EXCEEDED", f"保守输入估算{size}加输出及安全预留超过预算{profile['context_budget']}")
    first_request = "s" + uuid.uuid4().hex
    inbox = directory / "inbox" / first_request
    inbox.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "id": generation_id, "batch_id": batch["id"], "review": review_ref,
                "candidate_schema": candidate_schema, "fact_checks_schema": "fact_checks_v1", "codex_model": codex_model,
                "config": config, "documents": review["documents"], "required_documents": [d["id"] for d in review["documents"]],
                "evidence_table": artifact(store.root, table_path), "prompt": artifact(store.root, prompt_path),
                "artifacts": [artifact(store.root, table_path), artifact(store.root, prompt_path)] + [d["body_ref"] for d in review["documents"]],
                "input_bytes": size, "capacity_check": "passed" if profile else "preflight_unavailable",
                "first_submission_id": first_request}
    check_review(store, batch, review_ref)
    write_json(target, manifest, replace=False)
    return artifact(store.root, target)


def load_generation(store, reference):
    manifest = read_json(verify(store.root, reference))
    for item in manifest["artifacts"]:
        verify(store.root, item)
    verify(store.root, manifest["review"])
    for document in manifest['documents']:
        verify(store.root, document['source_ref'])
    return manifest, read_json(verify(store.root, manifest["evidence_table"]))
