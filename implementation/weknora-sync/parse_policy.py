"""Robot 5 parsing failures retain their cause without retrying fixed-input defects."""
import hashlib
import json
import urllib.error

VERSION = 'robot5-parser-quality-v2'


class ParseFailure(ValueError):
    def __init__(self, code, category, retryable, recovery, *, report_path=None, retry_at=None):
        super().__init__(code)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.recovery = recovery
        self.report_path = str(report_path) if report_path else None
        self.retry_at = retry_at

    def record(self):
        return {**({'context': self.context} if getattr(self,'context',None) else {}), 'code': self.code, 'category': self.category, 'retryable': self.retryable,
                'recovery': self.recovery, 'report_path': self.report_path, 'retry_at': self.retry_at}


def policy_signature(cfg):
    # Credentials and original document text never enter the signature/report.
    values = {k: cfg.get(k) for k in ('parser_config', 'llm_enabled', 'allow_model_calls',
                                     'reuse_robot1_artifacts', 'parse_limits','automatic_recovery_enabled')}
    from pathlib import Path
    implementations = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                       for name in ('parse_control.py','source_recovery.py','parse_artifacts.py','response_adapter.py','xlsx_recovery.py','parse_quality.py','source_catalog.py')}
    for name in ('model.py','documents.py','runtime.py','wps_checkboxes.py'):
        implementations['docflow/'+name]=hashlib.sha256((Path(__file__).resolve().parents[2]/'docflow'/name).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps([VERSION, values, implementations], sort_keys=True).encode()).hexdigest()


def report_summary(report):
    names = ('schema_version', 'policy_version', 'source_hash', 'method', 'status', 'coverage_scope',
             'expected_units', 'completed_units', 'text_units', 'blank_units', 'missing_body_units',
             'issue_units', 'processing_record_units', 'no_text_units', 'human_review')
    return {k: report[k] for k in names if k in report}


def _classify(error):
    if isinstance(error, ParseFailure):
        return error
    code = getattr(error, 'code', None)
    scope = getattr(error, 'scope', None)
    if isinstance(error, dict):
        code, scope = error.get('code'), error.get('scope')
    scope = scope if isinstance(scope, dict) else {}
    legacy = str(error)
    aliases = {'model_authorization_required_hold': 'MODEL_AUTHORIZATION_REQUIRED',
               'no_readable_text_hold': 'NO_READABLE_TEXT', 'image_assets_hold': 'IMAGE_ASSETS_UNSUPPORTED',
               'direct_local_text_format_unsupported': 'UNSUPPORTED_FORMAT',
               'empty_or_binary_text': 'INVALID_DOCUMENT', 'body_recovery_incomplete': 'MISSING_BODY',
               'source_recovery_body_invalid': 'MISSING_BODY',
               'missing_text_body': 'MISSING_BODY', 'parse_incomplete': 'PARSE_INCOMPLETE',
               'model_secret_file_permissions': 'CREDENTIAL_CONFIGURATION',
               'unexpected_model_secret_fields': 'CREDENTIAL_CONFIGURATION',
               'snapshot_fingerprint_mismatch': 'SOURCE_CHANGED',
               'xlsx_source_changed_during_compaction': 'SOURCE_CHANGED'}
    code = code or aliases.get(legacy)
    if code in {'MODEL_HTTP_ERROR', 'DOCUMENT_REQUEST_REJECTED'}:
        status, category = scope.get('http_status'), scope.get('category')
        if category in {'content_rejected', 'capacity', 'invalid_request'}:
            return ParseFailure(code, category, False, '检查原件或解析参数后定向重试')
        if status in (401, 403) or category == 'authentication':
            return ParseFailure(code, 'credentials', False, '修复凭据或授权后定向重试')
        if status == 404 or category == 'endpoint':
            return ParseFailure(code, 'configuration', False, '修复模型端点后定向重试')
        if status is None or status in (408, 429) or isinstance(status, int) and status >= 500:
            return ParseFailure(code, 'network', True, '保留检查点并按退避策略重试；沿用模型尝试上限')
        return ParseFailure(code, 'request', False, '检查模型请求配置后定向重试')
    if code in {'RETRY_AFTER', 'SOURCE_CHANGED', 'IO_ERROR', 'READ_FAILED', 'CONVERSION_TIMEOUT'}:
        return ParseFailure(code, 'transient', True, '等待来源或运行环境稳定后重试')
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return ParseFailure('NETWORK_ERROR', 'network', True, '网络恢复后自动重试')
    if code in {'CAPACITY_EXCEEDED', 'UNIT_TEXT_CAPACITY', 'RESPONSE_TOO_LARGE'} or legacy in {
            'xlsx_expanded_budget_exceeded', 'xlsx_drawing_metadata_too_large',
            'xlsx_sheet_streaming_budget_exceeded', 'xlsx_visible_extent_exceeds_compaction_bound',
            'xlsx_has_no_oversized_empty_tail', 'xlsx_content_beyond_compaction_bound'}:
        return ParseFailure(code or 'XLSX_CAPACITY', 'capacity', False, '保留原件；拆分或修复解析副本后定向重试')
    if code in {'CREDENTIAL_MISSING', 'MODEL_AUTHORIZATION_REQUIRED', 'CREDENTIAL_CONFIGURATION'}:
        return ParseFailure(code, 'credentials', False, '修复凭据或解析授权后定向重试')
    if code in {'CONFIG_ERROR', 'CONFIG_CHANGED', 'PROMPT_INVALID', 'CONVERSION_TOOL_MISSING',
                'CONVERSION_SAFETY_UNAVAILABLE', 'DISK_SPACE_LOW'}:
        return ParseFailure(code, 'environment', False, '修复工具、空间或配置后定向重试')
    if code == 'PARSER_STOPPED':
        return ParseFailure(code, 'stopped', True, '停止已落盘，启动后从原任务继续')
    if code == 'MODEL_OUTCOME_UNKNOWN':
        return ParseFailure(code, 'outcome_unknown', False, '先查响应与请求账本；未知请求不自动重发')
    if code == 'PARSE_BUDGET_EXCEEDED':
        return ParseFailure(code, 'budget', False, '保留成功单元；检查报告并显式调整该文件预算')
    if code == 'NO_READABLE_TEXT':
        return ParseFailure(code, 'no_text', False, '保留原件；未识别到文字，不上传空正文')
    if code in {'MODEL_TRUNCATED', 'MODEL_RESPONSE_INVALID', 'MODEL_INPUT_CHANGED', 'ATTEMPTS_EXHAUSTED',
                'INTEGRITY_ERROR', 'PARSE_INCOMPLETE', 'MISSING_BODY', 'UNIT_COVERAGE_INCOMPLETE',
                'RESPONSE_STORAGE_FAILED'}:
        return ParseFailure(code, 'quality', False, '检查原件、完整性报告或检查点后定向重试')
    if code in {'INVALID_DOCUMENT', 'UNSUPPORTED_FORMAT', 'IMAGE_ASSETS_UNSUPPORTED'}:
        return ParseFailure(code, 'document', False, '修复原件或格式适配后定向重试')
    if isinstance(error, OSError):
        return ParseFailure('LOCAL_IO_ERROR', 'environment', False, '检查本地文件权限或磁盘后定向重试')
    return ParseFailure(code or 'PARSE_UNEXPECTED_ERROR', 'unknown', False,
                        '检查解析日志后定向重试，避免重复调用同一失败输入')


def classify(error):
    failure=_classify(error)
    scope=error.get('scope') if isinstance(error,dict) else getattr(error,'scope',None)
    scope=scope if isinstance(scope,dict) else {}
    import re
    context={'error_type':type(error).__name__}
    for name in ('stage','unit_id','request_hash','outcome_certainty'):
        value=scope.get(name)
        if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):context[name]=value
    if len(context)>1 or failure.code=='PARSE_UNEXPECTED_ERROR':failure.context=context
    return failure
