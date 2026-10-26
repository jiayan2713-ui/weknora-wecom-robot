"""Local failure journal and best-effort macOS notices; no external messaging."""
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

BEIJING = dt.timezone(dt.timedelta(hours=8))
FAILURE_STATES = {'original_ingest_hold','parse_blocked_hold', 'parse_retry_wait', 'retry_wait', 'no_text_hold',
                  'model_authorization_hold', 'platform_failed_hold', 'upload_uncertain_hold',
                  'unsupported_changed_format_hold'}
REASONS = {
    'ORIGINAL_TABLE_INCOMPLETE': ('原件直传验证', '平台解析或索引未完整覆盖原生表格；旧知识保留', '查看direct-validation报告；修复平台能力后定向重试，不转Markdown。'),
    'MODEL_OUTCOME_UNKNOWN': ('模型请求确认', '先前请求结果未知，已阻止自动重发', '已保留请求账本与成功单元；先确认供应商结果，避免重复计费。'),
    'PARSE_BUDGET_EXCEEDED': ('解析预算', '该来源版本的解析预算已耗尽', '查看阶段、次数和缓存命中；调整明确预算后定向恢复，不清空历史。'),
    'PARSER_STOPPED': ('解析停机', '已停止派发，保留检查点', '启动后从原任务恢复。'),
    'PDF_COMPATIBILITY_FAILED': ('PDF兼容修复', 'PDF自动兼容修复失败', '原件与旧版保留；查看解析报告中的兼容修复原因后定向重试。'),
    'PDF_COMPATIBILITY_VALIDATION_FAILED': ('PDF兼容验证', '兼容副本未通过完整性或逐页一致性验证', '已阻止发布；检查页数、画面、文字及指纹，保留原件。'),
    'PDF_COMPATIBILITY_LIMIT': ('PDF兼容修复', 'PDF自动修复超时或超过资源上限', '原件未修改；查看兼容验证报告，调整预算或分区后定向重试。'),
    'PDF_COMPATIBILITY_ENVIRONMENT': ('PDF兼容环境', 'PDF自动修复运行环境不可用', '检查pdf_compatibility_python和PyMuPDF安装；修复后定向重试。'),
    'UNIT_COVERAGE_INCOMPLETE': ('解析', '解析单元缺正文或覆盖不完整', '查看完整性报告，修复后定向重试；保留已有可用版本。'),
    'NO_READABLE_TEXT': ('解析', '未识别到可用文字', '对照原件确认；有文字时检查分区与清晰度，确无文字时保留原件。'),
    'CAPACITY_EXCEEDED': ('解析', '超出解析容量', '检查报告，拆分解析副本或调整分区；不要修改原件。'),
    'IMAGE_ASSETS_UNSUPPORTED': ('解析', '图片资产暂未适配', '检查内嵌图片和解析报告，完成适配后定向重试。'),
    'MODEL_TRUNCATED': ('解析', '模型输出被截断', '检查输出长度与分区；修复后定向重试，不发布半份正文。'),
    'MISSING_BODY': ('解析', '模型未返回可用正文', '查看解析报告和原件清晰度，补齐正文后通过服务入口定向重试。'),
    'MODEL_AUTHORIZATION_REQUIRED': ('模型授权', '缺少模型调用授权', '核对当前目录及既有模型授权范围。'),
    'PLATFORM_PARSE_FAILED': ('WeKnora索引', '平台解析或索引失败', '查看WeKnora文档处理详情，保留旧版后排查。'),
    'UPLOAD_UNCERTAIN': ('上传确认', '上传结果尚未确认', '先查询平台实际记录，避免重复上传。'),
    'UNSUPPORTED_FORMAT': ('格式', '当前文件格式未适配', '检查真实格式及原件版本，完成适配后重试。'),
    'RETRIEVAL_PROBE_FAILED': ('检索验证', '新条目未通过检索验证', '检查索引与分块状态；旧版继续保留。'),
    'REMOTE_STATUS_UNAVAILABLE': ('WeKnora访问', '无法确认平台文档状态', '检查本地WeKnora、Docker及访问权限；服务会继续重试。'),
    'ROOT_UNAVAILABLE': ('来源目录', '来源目录不可用或目录身份变化', '检查企微本地同步目录与权限；不要把取消同步当作删除。'),
    'MASS_MISSING_PROTECTION': ('来源目录', '大量文件缺失，触发保护暂停', '检查企微同步、权限与目录；不要清空映射或下游资料。'),
    'NETWORK_ERROR': ('网络或平台访问', '网络或平台请求失败', '检查网络及平台运行状态；按日志记录的计划等待重试。'),
    'ARTIFACT_INTEGRITY': ('产物校验', '产物或上传响应的指纹不一致', '保留当前版本，检查解析产物与平台原文件，核对后再重试。'),
    'DIRECTORY_ALIGNMENT': ('目录对齐', '平台目录未完成对齐', '检查目标知识库目录及写入权限，不要手工重建映射。'),
}


def save(path, value):
    temp = path.with_suffix(path.suffix+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def stamp(value):
    return dt.datetime.fromtimestamp(value, BEIJING).isoformat(timespec='seconds')


def error_code(value):
    """Only recognize codes, never archive arbitrary exception strings or credentials."""
    value = str(value or '')
    exact = {'root_identity_changed_or_unavailable': 'ROOT_UNAVAILABLE',
             'source_root_identity_changed_protective_pause': 'ROOT_UNAVAILABLE',
             'mass_missing_protective_pause': 'MASS_MISSING_PROTECTION',
             'mass_source_missing_protective_pause': 'MASS_MISSING_PROTECTION',
             'retrieval_probe_failed': 'RETRIEVAL_PROBE_FAILED',
             'remote_status_unavailable': 'REMOTE_STATUS_UNAVAILABLE',
             'image_assets_hold': 'IMAGE_ASSETS_UNSUPPORTED',
             'model_authorization_required_hold': 'MODEL_AUTHORIZATION_REQUIRED',
             'direct_local_text_format_unsupported': 'UNSUPPORTED_FORMAT',
             'upload_response_hash_mismatch': 'ARTIFACT_INTEGRITY',
             'prepared_markdown_changed': 'ARTIFACT_INTEGRITY',
             'markdown_copy_mismatch': 'ARTIFACT_INTEGRITY',
             'directory_alignment_failed': 'DIRECTORY_ALIGNMENT',
             'directory_alignment_not_verified': 'DIRECTORY_ALIGNMENT'}
    tail = value.split(':', 1)[-1]
    if tail in exact:
        return exact[tail]
    match = re.fullmatch(r'(?:upload_rejected_|inventory_http_)(\d{3})', tail)
    if match:
        return ('UPLOAD_HTTP_' if tail.startswith('upload_') else 'PLATFORM_HTTP_') + match[1]
    if value.split(':', 1)[0] in {'TimeoutError', 'URLError', 'ConnectionError'}:
        return 'NETWORK_ERROR'
    return 'SYNC_ERROR'


def report_coverage(path, version):
    if not path or not version:
        return None
    try:
        file = Path(path)
        if file.stat().st_size > 4*1024*1024:
            return None
        report = json.loads(file.read_text())
        if report.get('source_hash') != version:
            return None
        counts = {key:report[key] for key in ('expected_units','completed_units','missing_body_units')
                  if type(report.get(key)) is int}
        if 'expected_units' not in counts:
            return None
        counts['missing_locations'] = [str(unit.get('label',''))[:160] for unit in report.get('units',[])
                                       if unit.get('status') in ('missing','missing_body','no_readable_text','duplicate')][:5]
        return counts
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def describe(identity, record, home):
    state = record.get('state')
    if state not in FAILURE_STATES:
        return None
    failure = record.get('direct_failure') or record.get('parse_failure') or record.get('last_parse_failure') or {}
    if state == 'parse_blocked_hold' and not failure and record.get('parse_retry_requested'):
        return None
    code = failure.get('code')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code):
        code = {'no_text_hold': 'NO_READABLE_TEXT', 'platform_failed_hold': 'PLATFORM_PARSE_FAILED',
                'upload_uncertain_hold': 'UPLOAD_UNCERTAIN',
                'unsupported_changed_format_hold': 'UNSUPPORTED_FORMAT',
                'model_authorization_hold': 'MODEL_AUTHORIZATION_REQUIRED'}.get(
                    state, error_code(record.get('error') or record.get('blocked_reason')))
    category = failure.get('category')
    fallback = ('解析' if state.startswith('parse_') else '同步', '处理异常，尚未完成同步',
                '查看服务日志和解析报告，修复后通过服务入口重试。')
    if category == 'credentials':
        fallback = ('模型或平台认证', '凭据或权限校验失败', '检查凭据及权限，不要把密钥贴入日志或聊天。')
    elif category == 'quality':
        fallback = ('解析', '解析质量检查未通过', '查看完整性报告，修复后定向重试；保留已有可用版本。')
    elif category == 'capacity':
        fallback = REASONS['CAPACITY_EXCEEDED']
    stage, reason, action = REASONS.get(code, fallback)
    context=failure.get('context',{})
    if context.get('stage'):stage+=' / '+str(context['stage'])
    if code.startswith(('UPLOAD_HTTP_', 'PLATFORM_HTTP_')):
        stage, reason, action = 'WeKnora上传或访问', '平台请求返回HTTP '+code[-3:], '检查平台状态及限流情况；服务会按退避计划继续重试。'
    if code.endswith(('_401', '_403')):
        stage, reason, action = 'WeKnora上传或访问', '平台拒绝访问或写入', '检查同步API Key的目标知识库与能力授权，不要记录密钥内容。'
    retrying = state in {'retry_wait', 'parse_retry_wait'}
    attempts = record.get('parse_attempts', 0) if state == 'parse_retry_wait' else record.get('attempts', 0)
    attention = not retrying or attempts >= 3
    report = (record.get('direct_failure') or {}).get('report_path') or record.get('latest_parse_report_path')
    if report and (not isinstance(report,str) or not Path(report).resolve().is_relative_to((home/'jobs').resolve())):
        report = None
    version = record.get('direct_blocked_hash') or record.get('blocked_source_hash') or record.get('parse_retry_hash') or record.get('latest_observed_hash') or record.get('source_hash')
    if not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version):
        version = None
    exception_type = str(record.get('error') or '').split(':', 1)[0]
    if not re.fullmatch(r'[A-Z][A-Za-z]*(?:Error|Exception)', exception_type):
        exception_type = None
    return {'source_id': identity, 'path': record.get('desired_path') or record.get('path', ''),
            'source_hash': version, 'stage': stage, 'code': code, 'reason': reason, 'action': action,
            'status': '正在自动重试' if retrying else '已暂停，需处理', 'needs_attention': attention,
            'attempts': attempts, 'next_attempt': record.get('next_attempt') if retrying else None,
            'exception_type': exception_type,
            'old_version_retained': bool(record.get('remote_ids')), 'report_path': report,
            'coverage': report_coverage(report, version)}


def desktop_notice(title, message):
    if sys.platform != 'darwin':
        return 'unsupported_platform'
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script, title, message],
                                capture_output=True, timeout=5)
        # Exit zero proves submission only; Focus/notification permissions can suppress banners.
        return 'submitted' if result.returncode == 0 else 'command_failed'
    except (OSError, subprocess.TimeoutExpired):
        return 'command_failed'


class FailureAlerts:
    def __init__(self, cfg, clock=time.time, notifier=desktop_notice):
        self.home = Path(cfg['state_dir']).resolve()
        self.root = self.home/'failure-alerts'
        options = cfg.get('failure_notifications', {})
        self.options = options if isinstance(options, dict) else {}
        self.enabled = self.options.get('enabled') is True
        self.clock, self.notifier = clock, notifier

    def _event(self, data, kind, fields):
        event = {'time_beijing': stamp(self.clock()), 'event': kind, **fields}
        path = self.root/('events-'+stamp(self.clock())[:10]+'.jsonl')
        with path.open('a', encoding='utf-8') as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(event, ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        data['recent'] = (data.get('recent', [])+[event])[-50:]

    def observe(self, records=None, *, complete=False, service_error=None):
        if not self.enabled:
            return {'enabled': False}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with (self.root/'writer.lock').open('a') as lock:
            os.chmod(self.root/'writer.lock', 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            file = self.root/'state.json'
            data = json.loads(file.read_text()) if file.exists() else {'issues': {}, 'episodes': {}, 'recent': []}
            rows = {identity: describe(identity, row, self.home) for identity, row in (records or {}).items()}
            if service_error is not None:
                code = error_code(type(service_error).__name__+':'+str(service_error))
                stage, reason, action = REASONS.get(code, ('同步服务', '本轮同步未完成或处于保护暂停',
                                                        '检查来源目录、Docker及平台连接；保留已有映射和资料。'))
                rows['__service__'] = {'source_id': '__service__', 'path': '整个同步服务', 'source_hash': None,
                    'stage': stage, 'code': code, 'reason': reason, 'action': action,
                    'status': '服务保护暂停', 'needs_attention': True, 'exception_type': type(service_error).__name__}
            for identity, row in rows.items():
                if row is None:
                    original = (records or {}).get(identity, {})
                    terminal = original.get('state') in {'deleted', 'excluded_by_policy', 'no_text_skipped'}
                    recovered = original.get('state') == 'active' and not any(original.get(k) for k in
                        ('pending', 'parse_retry_requested', 'parse_recovery_checkpoint', 'parse_failure'))
                    if identity in data['issues'] and (terminal or recovered):
                        issue = data['issues'].pop(identity)
                        self._event(data, 'recovered' if recovered else 'skipped_no_text' if original.get('state') == 'no_text_skipped' else 'closed_source_removed_or_excluded',
                                    {k: issue.get(k) for k in ('source_id', 'path', 'code', 'episode')})
                    continue
                fingerprint = hashlib.sha256(json.dumps([identity, row['source_hash'], row['stage'], row['code']], ensure_ascii=False).encode()).hexdigest()
                old = data['issues'].get(identity)
                if old is None:
                    episode = data['episodes'].get(identity, 0)+1
                    data['episodes'][identity] = episode
                    issue = {**row, 'episode': episode, 'first_seen': stamp(self.clock()),
                             'notified_fingerprints': [], 'observations': 0}
                else:
                    issue = {**old, **row}
                issue.update(fingerprint=fingerprint, last_seen=stamp(self.clock()), observations=issue['observations']+1)
                data['issues'][identity] = issue
                if old is None or old['fingerprint'] != fingerprint or old['needs_attention'] != row['needs_attention']:
                    self._event(data, 'failure' if old is None else 'failure_updated',
                                {k: v for k, v in issue.items() if k != 'notified_fingerprints'})
            if complete and '__service__' in data['issues']:
                issue = data['issues'].pop('__service__')
                self._event(data, 'recovered', {k: issue.get(k) for k in ('source_id', 'path', 'code', 'episode')})
            pending = [issue for issue in data['issues'].values() if issue['needs_attention'] and
                       issue['fingerprint'] not in issue['notified_fingerprints']]
            cooldown = self.options.get('desktop_cooldown_seconds', 60)
            if type(cooldown) is not int or not 10 <= cooldown <= 3600:
                cooldown = 60
            if self.options.get('desktop') is True and pending and self.clock() >= data.get('next_notice_at', 0):
                try:
                    status = self.notifier('机器人5同步失败提醒', f'有{len(pending)}项新问题需要处理；详情见同步日志“当前问题.md”。')
                except Exception:
                    status = 'command_failed'
                data.update(last_notice_status=status, last_notice_at=stamp(self.clock()),
                            next_notice_at=self.clock()+(cooldown if status == 'submitted' else 300))
                self._event(data, 'desktop_notice', {'status': status, 'issue_count': len(pending),
                                                   'banner_delivery_verified': False})
                if status == 'submitted':
                    for issue in pending:
                        issue['notified_fingerprints'].append(issue['fingerprint'])
            save(file, data)
            current = {'updated_at_beijing': stamp(self.clock()), 'open_count': len(data['issues']),
                       'issues': list(data['issues'].values()), 'recent_events': data['recent'],
                       'desktop_status': data.get('last_notice_status', 'not_submitted'),
                       'banner_delivery_verified': False}
            save(self.root/'current.json', current)
            self._summary(current)
            return {'enabled': True, 'open_count': current['open_count'],
                    'desktop_status': current['desktop_status'], 'summary_path': str(self.root/'当前问题.md')}

    def _summary(self, current):
        def text(value):
            return re.sub(r'[\x00-\x1f\x7f]', ' ', html.escape(str('' if value is None else value)))
        desktop_label = {'submitted':'已向macOS提交', 'command_failed':'提交失败，稍后重试',
                         'unsupported_platform':'当前系统不支持桌面提醒', 'not_submitted':'尚未提交'}.get(current['desktop_status'],current['desktop_status'])
        lines = ['# 机器人5同步问题', '', '更新时间（北京时间）：'+current['updated_at_beijing'],
                 '', f'当前未解决：{current["open_count"]} 项。',
                 '桌面提醒状态：'+desktop_label+'（横幅显示仍受系统通知设置和专注模式控制）。', '']
        for issue in sorted(current['issues'], key=lambda value: value['path']):
            for label, key in [('文件', 'path'), ('状态', 'status'), ('阶段', 'stage'), ('错误码', 'code'),
                               ('原因', 'reason'), ('建议', 'action'), ('首次发现', 'first_seen'),
                               ('最近检查', 'last_seen'), ('异常类型', 'exception_type'),
                               ('重试次数', 'attempts'), ('解析报告', 'report_path')]:
                if issue.get(key) is not None:
                    lines.append(f'{label}：{text(issue[key])}  ')
            lines.extend(['已有活动版本：'+('保留' if issue.get('old_version_retained') else '无或不适用'), ''])
            coverage = issue.get('coverage')
            if coverage:
                lines.append('准备单元覆盖：'+str(coverage.get('completed_units','未知'))+'/'+str(coverage['expected_units'])+
                             '；缺正文：'+str(coverage.get('missing_body_units','未知'))+'  ')
                lines.extend('未完成定位：'+text(label)+'  ' for label in coverage['missing_locations'])
                lines.append('')
            if issue.get('next_attempt'):
                lines.extend(['下次重试（北京时间）：'+stamp(issue['next_attempt']), ''])
        lines.extend(['最近记录（完整历史见同目录按日期分组的events日志）：', ''])
        for event in reversed(current['recent_events'][-12:]):
            label = {'skipped_no_text':'无文字，已跳过（未入库）','failure':'发现失败','failure_updated':'问题变化','recovered':'已恢复同步',
                     'closed_source_removed_or_excluded':'来源删除或已排除，关闭提醒','desktop_notice':'桌面提醒提交'}.get(event['event'],event['event'])
            lines.append(text(event['time_beijing']+' '+label+' '+event.get('path', ''))+'  ')
        temp = self.root/'summary.tmp'
        temp.write_text('\n'.join(lines)+'\n', encoding='utf-8')
        temp.chmod(0o600)
        os.replace(temp, self.root/'当前问题.md')
