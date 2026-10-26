"""Explicit batch settings; secrets are never part of this object."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
import re

from .common import WorkflowError, fingerprint

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
DEFAULTS = {
    "model": "qwen3.8-flash",
    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "api_key_env": "DASHSCOPE_API_KEY",
    "temperature": 0.1, "max_tokens": 20000, "timeout_seconds": 120,
    "conversion_timeout_seconds": 180, "render_timeout_seconds": 60,
    "conversion_retry_timeout_seconds": 300, "max_unit_text_chars": 16000,
    "office_static_fallback": True,
    "local_ocr_python": str(PROJECT / ".local/docflow-ocr/bin/python") if (PROJECT / ".local/docflow-ocr/bin/python").is_file() else None,
    "input_route": "multimodal",
    "max_attempts": 3, "max_repairs": 2,
    "model_review": False, "parser_workers": 4,
    "document_workers": 1, "render_workers": 1, "empty_body_policy": "review",
    "single_step_questions": 32, "multi_step_questions": 8,
    "out_of_kb_questions": 5, "ambiguous_questions": 5, "min_analytical_multistep": 4,
    "id_prefix": "QA", "cache_policy": "reuse",
    "allowed_url_schemes": ["http", "https"], "capacity_profile": None,
    "parser_prompt": str(PROJECT / "提示词/通用文档解析提示词_v3.1.md"),
    "evaluation_prompt": str(PROJECT / "提示词/评测集生成提示词.md"),
    "soffice": str(RUNTIME / "bin/override/soffice"),
    "pdftoppm": str(RUNTIME / "bin/override/pdftoppm"),
}


def configuration(values=None):
    values = {} if values is None else values
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise WorkflowError("CONFIG_ERROR", "配置包含未知字段；密钥只能放在环境变量中")
    result = deepcopy(DEFAULTS)
    result.update(values)
    if type(result["office_static_fallback"]) is not bool:
        raise WorkflowError("CONFIG_ERROR", "office_static_fallback必须为布尔值")
    if result['input_route'] not in ('multimodal', 'text_experiment'):
        raise WorkflowError('CONFIG_ERROR', 'input_route无效')
    if type(result['max_unit_text_chars']) is not int or not 1000 <= result['max_unit_text_chars'] <= 100000:
        raise WorkflowError('CONFIG_ERROR', '单元文字预算必须在1000到100000之间')
    if result['local_ocr_python'] is not None and (not isinstance(result['local_ocr_python'], str) or not Path(result['local_ocr_python']).is_absolute()):
        raise WorkflowError('CONFIG_ERROR', '本地OCR Python必须是绝对路径或null')
    if type(result["model_review"]) is not bool:
        raise WorkflowError("CONFIG_ERROR", "model_review必须为布尔值")
    if type(result["parser_workers"]) is not int or not 1 <= result["parser_workers"] <= 8:
        raise WorkflowError("CONFIG_ERROR", "parser_workers必须为1到8的整数")
    for key, maximum in [("document_workers", 8), ("render_workers", 4)]:
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise WorkflowError("CONFIG_ERROR", f"{key}必须为1到{maximum}的整数")
    if result["empty_body_policy"] not in {"review", "recover"}:
        raise WorkflowError("CONFIG_ERROR", "empty_body_policy必须为review或recover")
    for key in ["model", "endpoint", "api_key_env", "parser_prompt", "evaluation_prompt", "soffice", "pdftoppm"]:
        if not isinstance(result[key], str) or not result[key].strip():
            raise WorkflowError("CONFIG_ERROR", f"{key}必须是非空字符串")
    for key in ["single_step_questions", "multi_step_questions", "out_of_kb_questions",
                "ambiguous_questions", "min_analytical_multistep"]:
        if type(result[key]) is not int or result[key] < 0:
            raise WorkflowError("CONFIG_ERROR", f"{key}必须是非负整数")
    result["in_kb_questions"] = result["single_step_questions"] + result["multi_step_questions"]
    result["total_questions"] = result["in_kb_questions"] + result["out_of_kb_questions"] + result["ambiguous_questions"]
    if not result["total_questions"] or result["min_analytical_multistep"] > result["multi_step_questions"]:
        raise WorkflowError("CONFIG_ERROR", "题量或多步分析数量不一致")
    if not isinstance(result["id_prefix"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", result["id_prefix"]) or result["id_prefix"].endswith("-"):
        raise WorkflowError("CONFIG_ERROR", "编号前缀无效")
    if result["cache_policy"] not in {"reuse", "refresh"}:
        raise WorkflowError("CONFIG_ERROR", "cache_policy只能为reuse或refresh")
    for key in ["timeout_seconds", "conversion_timeout_seconds", "conversion_retry_timeout_seconds", "render_timeout_seconds", "max_tokens"]:
        if type(result[key]) not in {int, float} or not 0 < result[key] < 1000000:
            raise WorkflowError("CONFIG_ERROR", f"{key}必须有界且为正数")
    if type(result["max_tokens"]) is not int:
        raise WorkflowError("CONFIG_ERROR", "max_tokens必须为整数")
    if type(result["temperature"]) not in {int, float} or not 0 <= result["temperature"] <= 2:
        raise WorkflowError("CONFIG_ERROR", "temperature必须在0到2之间")
    if type(result["max_attempts"]) is not int or not 1 <= result["max_attempts"] <= 3:
        raise WorkflowError("CONFIG_ERROR", "模型实际尝试上限为3")
    if type(result["max_repairs"]) is not int or not 0 <= result["max_repairs"] <= 2:
        raise WorkflowError("CONFIG_ERROR", "候选修正上限为2")
    try:
        endpoint = urlsplit(result["endpoint"])
        endpoint.port
    except ValueError as exc:
        raise WorkflowError("CONFIG_ERROR", "模型端点格式无效") from exc
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None or endpoint.password is not None or endpoint.query or endpoint.fragment:
        raise WorkflowError("CONFIG_ERROR", "模型端点必须为无凭据、无查询参数的HTTPS地址")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["api_key_env"]):
        raise WorkflowError("CONFIG_ERROR", "凭据环境变量名称无效")
    if not isinstance(result["model"], str) or not result["model"].strip():
        raise WorkflowError("CONFIG_ERROR", "模型名称不能为空")
    if not isinstance(result["allowed_url_schemes"], list) or not all(
            isinstance(x, str) and re.fullmatch(r"[a-z][a-z0-9+.-]*", x) for x in result["allowed_url_schemes"]):
        raise WorkflowError("CONFIG_ERROR", "URL协议列表无效")
    if set(result["allowed_url_schemes"]) & {"javascript", "data", "vbscript", "about"}:
        raise WorkflowError("CONFIG_ERROR", "不能将主动内容协议写入来源超链接")
    profile = result["capacity_profile"]
    if profile is not None:
        keys = {"context_budget", "reserved_output_tokens", "safety_reserve_tokens", "estimator_version"}
        if not isinstance(profile, dict) or set(profile) != keys or profile["estimator_version"] != "utf8-bytes-upper-bound-v1":
            raise WorkflowError("CONFIG_ERROR", "容量配置必须指定受支持的估算器及完整预算")
        if any(type(profile[k]) is not int or profile[k] < 0 for k in keys - {"estimator_version"}):
            raise WorkflowError("CONFIG_ERROR", "容量预算必须为非负整数")
        if profile["context_budget"] <= profile["reserved_output_tokens"] + profile["safety_reserve_tokens"]:
            raise WorkflowError("CONFIG_ERROR", "上下文预算必须大于输出和安全预留")
    fingerprint(result)
    return result


def source_url(value, config):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise WorkflowError("SOURCE_URL_INVALID", "来源URL格式无效")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError as exc:
        raise WorkflowError("SOURCE_URL_INVALID", "来源URL格式无效") from exc
    if parts.scheme.lower() not in config["allowed_url_schemes"]:
        raise WorkflowError("SOURCE_URL_UNSUPPORTED", "来源URL协议不在本批允许范围")
    if parts.username is not None or parts.password is not None:
        raise WorkflowError("SOURCE_URL_INVALID", "来源URL不得携带凭据")
    if parts.scheme in {"http", "https"} and not parts.hostname:
        raise WorkflowError("SOURCE_URL_INVALID", "来源URL地址无效")
    return value
