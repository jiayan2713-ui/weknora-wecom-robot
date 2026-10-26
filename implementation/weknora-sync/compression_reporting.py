"""Human-readable daily results, refreshed by receipt-only recovery."""
import json
from pathlib import Path

from reconcile import save

LABELS = {'client_verified': '同步确认成功', 'cloud_verified': '回读确认成功',
          'failed': '失败', 'compression_blocked': '压缩失败，暂停重复尝试',
          'retry_deferred': '等待重试', 'conflict': '内容冲突，未覆盖',
          'attention_required': '需要处理', 'waiting_client': '等待客户端',
          'waiting_sync': '等待同步', 'prepared': '候选已准备，未覆盖',
          'not_needed': '不超过30MB，无需压缩', 'unsupported': '不支持的格式，跳过',
          'excluded': '按配置排除', 'metadata': '同步/系统元数据，跳过', 'symlink': '符号链接，跳过'}
FAILED = {'failed', 'compression_blocked', 'conflict', 'attention_required'}
WAITING = {'waiting_client', 'waiting_sync', 'retry_deferred', 'prepared',
           'local_write_intent', 'local_replaced', 'uncertain'}


def summarize(report):
    files = report.get('files', [])
    inventory = report.get('inventory', [])
    return {'同步成功': sum(f['phase'] in {'client_verified', 'cloud_verified'} for f in files),
            '无需压缩': sum(f['phase'] == 'not_needed' for f in inventory),
            '格式不支持': sum(f['phase'] == 'unsupported' for f in inventory),
            '其他跳过': sum(f['phase'] in {'excluded', 'metadata', 'symlink'} for f in inventory),
            '失败或需处理': sum(f['phase'] in FAILED for f in files),
            '等待处理': sum(f['phase'] in WAITING for f in files) + report.get('remaining_receipts', 0)}


def markdown(report):
    def cell(value):
        return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')
    lines = ['# 测试目录文档压缩每日结果', '',
             f"执行时间：{report['started_at']}；状态：{report['status']}",
             f"范围：{report['scope']}",
             f"最近更新：{report.get('updated_at', report.get('finished_at', '—'))}", '',
             '；'.join(f'{key} {value}' for key, value in report['summary'].items()), '',
             '大小单位为字节，阈值 30,000,000。同步成功指客户端文件ID、版本记录、MD5和大小核验通过。', '',
             '| 文件 | 结果 | 原大小 | 候选/现大小 | 原件备份 | 原因/下次重试 |',
             '|---|---|---:|---:|---|---|']
    for item in [*report.get('files', []), *report.get('inventory', [])]:
        reason = item.get('error', '')
        if item.get('next_retry_at'):
            reason += '；下次允许重试：' + item['next_retry_at']
        lines.append('| ' + ' | '.join(cell(v) for v in [item.get('relative', item.get('receipt')),
                     LABELS.get(item['phase'], item['phase']), item.get('original_bytes'),
                     item.get('output_bytes'), item.get('backup'), reason]) + ' |')
    if report.get('error'):
        lines.extend(['', '任务错误：' + cell(report['error'])])
    return '\n'.join(lines) + '\n'


def publish_daily(registry, report):
    if report['status'] == 'busy':
        return
    daily = report
    if report['mode'] == 'recovery':
        previous = registry / 'last-daily.json'
        if not previous.exists():
            return
        daily = json.loads(previous.read_text())
        # A recovery run updates only the exact receipts referenced by this day.
        changes = {f.get('receipt'): f for f in report['files'] if f.get('receipt')}
        for old in daily['files']:
            update = changes.get(old.get('receipt'))
            if update and old['phase'] in WAITING | {'attention_required'}:
                for key in ('error', 'error_type', 'next_retry_at'):
                    old.pop(key, None)
                old.update(update)
        daily['updated_at'] = report['finished_at']
        phases = {f['phase'] for f in daily['files']}
        if daily.get('error'):
            daily['status'] = 'failed'
        else:
            daily['status'] = ('needs_attention' if phases & FAILED else
                               'waiting' if phases & WAITING else 'success')
    daily['summary'] = summarize(daily)
    directory = registry / 'daily-reports'
    directory.mkdir(exist_ok=True)
    date = daily['started_at'][:10]
    save(directory / (date + '.json'), daily)
    save(registry / '每日结果_最新.json', daily)
    text = markdown(daily)
    (directory / (date + '.md')).write_text(text)
    (registry / '每日结果_最新.md').write_text(text)
    if report['mode'] == 'recovery':
        save(registry / 'last-daily.json', daily)
