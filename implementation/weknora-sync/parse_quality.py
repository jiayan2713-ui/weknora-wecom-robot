"""Prepared-unit coverage checks, separate from source or human fact review."""
from __future__ import annotations

from collections import Counter
import json
import re

from docflow.parser import split_notes


MAX_ISSUES = 20
MAX_ISSUE_CHARS = 512
MAX_WARNINGS = 100
PROCESSING_RECORD_LABEL = '转换处理记录（非原文）'
BLANK_ISSUE = '已检测空白单元，待人工确认'
BLANK_NOTE = '本单元无可提取正文且渲染图像全白'
NO_TEXT_NOTE = '模型报告无可辨认文字'
NO_TEXT_ISSUE = '模型报告无可辨认文字，待人工确认'


def _bounded_issues(value):
    """Retain useful diagnostics without copying arbitrary result fields."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values[:MAX_ISSUES]:
        if isinstance(item, dict):
            selected = {name: item[name] for name in ('code', 'message', 'reason') if name in item}
            item = json.dumps(selected or {'code': 'structured_issue'}, ensure_ascii=False,
                              default=lambda _: '<non-json-value>')
        elif not isinstance(item, str):
            item = str(item)
        text = item[:MAX_ISSUE_CHARS]
        if text and text not in result:
            result.append(text)
    if len(values) > MAX_ISSUES:
        result.append(f'另有 {len(values) - MAX_ISSUES} 条疑点未展开')
    return result


def _body_content(result):
    body = result.get('body')
    if not isinstance(body, str):return ''
    if result.get('body_is_source') is True or result.get('method') in ('native_text', 'native_cells'):return body.strip()
    from response_adapter import response_parts
    return response_parts(body)[0]



def _confirmed_blank(expected, result):
    explicit_states = {'confirmed_blank', 'blank_confirmed', 'empty_render', 'confirmed_native_blank'}
    if result.get('empty_render') is True or expected.get('empty_render') is True:
        return True
    if result.get('content_status') in explicit_states:
        return True
    if result.get('native_blank_confirmed') is True or expected.get('native_blank_confirmed') is True:
        return True
    # parser.confirmed_blank_unit only emits this pair after checking both the
    # text layer/native cell metadata and every rendered pixel.
    return (BLANK_ISSUE in _bounded_issues(result.get('issues')) and
            isinstance(result.get('notes'), str) and BLANK_NOTE in result['notes'])


def _explicit_no_text(result, body):
    if body == '[[DOCFLOW_NO_TEXT]]':
        return True
    if body:
        return False
    notes = result.get('notes')
    return ((isinstance(notes, str) and NO_TEXT_NOTE in notes) or
            NO_TEXT_ISSUE in _bounded_issues(result.get('issues')))


def summarize_units(expected_units, result_units, source_hash, method='docflow_llm'):
    """Check IDs and usable bodies; never attest to original-source completeness.

    Counts refer to prepared units. Processing records are accounted for but
    excluded from text/blank counts. Missing/duplicate/unexpected IDs always
    block completeness. An explicit all-no-text result is a hold condition,
    while an unexplained empty body remains incomplete.
    """
    expected = list(expected_units)
    results = list(result_units)
    expected_ids = Counter(unit.get('id') for unit in expected
                           if isinstance(unit, dict) and isinstance(unit.get('id'), str) and unit['id'])
    result_ids = Counter(unit.get('id') for unit in results
                         if isinstance(unit, dict) and isinstance(unit.get('id'), str) and unit['id'])
    by_id = {unit['id']: unit for unit in results
             if isinstance(unit, dict) and isinstance(unit.get('id'), str) and unit['id']}
    missing_ids = [identity for identity in expected_ids if identity not in result_ids]
    duplicate_ids = list(dict.fromkeys([identity for identity, count in expected_ids.items() if count > 1] +
                                      [identity for identity, count in result_ids.items() if count > 1]))
    unexpected_ids = [identity for identity in result_ids if identity not in expected_ids]
    malformed = sum(not isinstance(unit, dict) or not isinstance(unit.get('id'), str) or not unit.get('id')
                    for unit in expected + results)
    report = {'schema_version': 1, 'source_hash': source_hash, 'method': method,
              'status': 'incomplete', 'expected_units': len(expected), 'completed_units': 0,
              'text_units': 0, 'blank_units': 0, 'missing_body_units': 0, 'issue_units': 0,
              'processing_record_units': 0, 'no_text_units': 0,
              'missing_ids': missing_ids, 'duplicate_ids': duplicate_ids,
              'unexpected_ids': unexpected_ids, 'units': [], 'warnings': [],
              'human_review': False, 'coverage_scope': 'prepared_units'}

    def warning(text):
        if len(report['warnings']) < MAX_WARNINGS:
            report['warnings'].append(text[:MAX_ISSUE_CHARS])

    for unit in expected:
        if not isinstance(unit, dict) or not isinstance(unit.get('id'), str) or not unit.get('id'):
            report['units'].append({'id': None, 'label': '', 'status': 'invalid_id',
                                    'issues': ['prepared_unit_id_invalid']})
            continue
        identity = unit['id']
        label = unit.get('label', '')
        label = label if isinstance(label, str) else ''
        result = by_id.get(identity)
        issues = _bounded_issues(result.get('issues')) if result else []
        status = 'missing'
        if identity in duplicate_ids:
            status = 'duplicate'
            issues.append('unit_id_duplicated')
        elif result is not None:
            body = _body_content(result)
            if label == PROCESSING_RECORD_LABEL:
                status = 'processing_record'
                report['processing_record_units'] += 1
                report['completed_units'] += 1
                issues.append('non_source_processing_record')
            elif result.get('content_state') in ('native_visual_conflict','unreadable_text'):
                status = 'native_visual_conflict'
                report['missing_body_units'] += 1
                issues.append('native_visual_conflict')
            elif body and body != '[[DOCFLOW_NO_TEXT]]':
                status = 'text'
                report['text_units'] += 1
                report['completed_units'] += 1
            elif _confirmed_blank(unit, result):
                status = 'blank'
                report['blank_units'] += 1
                report['completed_units'] += 1
            elif (not body and isinstance(result.get('no_text_verification'), dict)
                  and result['no_text_verification'].get('method') == 'second_source_image_read'
                  and result['no_text_verification'].get('verified') is True
                  and all(isinstance(result['no_text_verification'].get(k), str)
                          and re.fullmatch(r'[a-f0-9]{64}', result['no_text_verification'][k])
                          for k in ('image_sha256', 'response_sha256'))):
                status = 'verified_no_text'
                report['no_text_units'] += 1
                report['completed_units'] += 1
                issues.append('二次原图复核无可读文字；未人工审核')
            elif _explicit_no_text(result, body):
                status = 'no_readable_text'
                report['no_text_units'] += 1
                report['missing_body_units'] += 1
                issues.append('explicit_no_readable_text')
            else:
                status = 'missing_body'
                report['missing_body_units'] += 1
                issues.append('usable_body_missing')
        if status == 'missing':
            issues.append('prepared_unit_result_missing')
        if issues:
            report['issue_units'] += 1
            warning(f'{identity}: ' + '; '.join(issues))
        report['units'].append({'id': identity, 'label': label, 'status': status,
                                'issues': issues[:MAX_ISSUES + 1]})
    for identity in unexpected_ids:
        unit = by_id[identity]
        issues = _bounded_issues(unit.get('issues'))
        issues.append('unexpected_result_unit')
        report['units'].append({'id': identity,
                                'label': unit.get('label', '') if isinstance(unit.get('label', ''), str) else '',
                                'status': 'unexpected', 'issues': issues[:MAX_ISSUES + 1]})
        warning(f'{identity}: unexpected_result_unit')
    if malformed:
        warning(f'发现 {malformed} 条无有效单元 ID 的记录')
    content_units = len(expected) - report['processing_record_units']
    structural_error = bool(missing_ids or duplicate_ids or unexpected_ids or malformed)
    if structural_error or not expected:
        report['status'] = 'incomplete'
    elif not content_units or (not report['text_units'] and (
            report['blank_units'] + report['no_text_units'] == content_units)):
        report['status'] = 'no_readable_text'
    elif report['missing_body_units']:
        report['status'] = 'incomplete'
    elif report['issue_units']:
        report['status'] = 'complete_with_warnings'
    else:
        report['status'] = 'complete'
    return report
