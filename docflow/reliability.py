"""Shared failure scope and content-quality accounting, independent of approval."""
FATAL_CODES = {'RESPONSE_STORAGE_FAILED', 'CREDENTIAL_MISSING', 'MODEL_HTTP_ERROR',
               'MODEL_INPUT_CHANGED', 'CONFIG_ERROR', 'DISK_SPACE_LOW', 'RETRY_AFTER',
               'INTEGRITY_ERROR', 'BATCH_STOPPED'}


def http_category(status, body=''):
    value = body.lower()
    if status in (401, 403):
        return 'authentication'
    if status == 404:
        return 'endpoint'
    if 'data_inspection_failed' in value or 'inappropriate content' in value:
        return 'content_rejected'
    if status == 413 or any(s in value for s in ('context_length', 'maximum context', 'token limit')):
        return 'capacity'
    if status == 429:
        return 'rate_limit'
    if status is None or status in (408, 502, 503, 504) or status >= 500:
        return 'transient'
    return 'invalid_request'


def quality_summary(units):
    counts = {'units_total': len(units), 'nonempty_units': 0, 'empty_units': 0,
              'needs_confirmation_units': 0, 'local_ocr_units': 0}
    for unit in units:
        empty = not unit.get('body', '').strip()
        counts['empty_units'] += empty
        counts['nonempty_units'] += not empty
        counts['needs_confirmation_units'] += bool(empty or unit.get('issues') or
            unit.get('content_status') == 'needs_manual_confirmation')
        counts['local_ocr_units'] += unit.get('method') == 'local_ocr'
    counts['content_status'] = 'needs_confirmation' if counts['needs_confirmation_units'] else 'transcribed'
    counts['human_review_approved'] = False
    return counts


def is_fatal(error):
    if error.code == 'MODEL_HTTP_ERROR' and isinstance(error.scope, dict):
        if error.scope.get('category') in {'content_rejected', 'capacity', 'invalid_request'}:
            return False
    return error.code in FATAL_CODES
