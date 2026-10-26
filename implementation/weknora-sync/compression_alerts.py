"""Independent, read-only compression watchdog with local macOS exception alerts."""
import argparse
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
PENDING = {'prepared', 'local_write_intent', 'local_replaced', 'uncertain', 'attention_required'}
FAILURES = {'failed', 'compression_blocked', 'retry_deferred', 'conflict', 'attention_required'}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.strptime(value, '%Y%m%d_%H%M%S_%f')
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def issue(kind, identity, message):
    key = hashlib.sha256(json.dumps([kind, identity], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'key': key, 'kind': kind, 'message': message}


def inspect(cfg, now):
    """Inspect receipts/reports only; never scan, compress, touch, or retry source files."""
    registry = Path(cfg['registry'])
    problems = []

    def read(path):
        if not path.exists():
            return None

        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('expected JSON object')
            return value
        except (OSError, ValueError) as error:
            problems.append(issue('report_error', [str(path), str(error)], f'无法读取任务记录：{path.name}（{type(error).__name__}）'))
            return None

    def read_time(value, identity):
        try:
            return timestamp(value)
        except (ValueError, TypeError):
            problems.append(issue('report_error', identity, f'任务时间记录无效：{identity}'))
            return None

    due = now.replace(hour=cfg.get('daily_hour', 10), minute=cfg.get('daily_minute', 0), second=0, microsecond=0)
    daily = read(registry / 'last-daily.json')
    active = read(registry / 'active-daily.json')
    valid = []
    for report in (daily, active):
        if report and report.get('apply') and report.get('mode') == 'daily':
            started = read_time(report.get('started_at'), report.get('run_id', 'daily'))
            if started and due <= started <= now:
                valid.append(report)
    enabled_at = read_time(cfg.get('schedule_enabled_at'), 'schedule_enabled_at')
    scheduled_today = not enabled_at or due >= enabled_at
    if cfg.get('apply_enabled', True) and scheduled_today and now >= due + timedelta(minutes=cfg.get('missing_grace_minutes', 15)) and not valid:
        problems.append(issue('daily_missing', now.date().isoformat(),
                              f"当天{due:%H:%M}任务尚无启动记录。请检查电脑登录状态和系统定时任务。"))
    if active and active.get('apply') and active.get('status') == 'running':
        started = read_time(active.get('started_at'), active.get('run_id', 'active'))
        finished = daily and daily.get('run_id') == active.get('run_id') and daily.get('finished_at')
        if started and not finished and now - started >= timedelta(minutes=cfg.get('running_timeout_minutes', 60)):
            problems.append(issue('daily_stalled', active.get('run_id'),
                                  f"压缩任务已启动超过{cfg.get('running_timeout_minutes', 60)}分钟，仍无完成记录。"))
    if daily and daily.get('apply'):
        started = read_time(daily.get('started_at'), daily.get('run_id', 'daily'))
        if started and started.date() == now.date():
            if daily.get('status') == 'failed' or daily.get('error'):
                problems.append(issue('task_failed', [daily.get('run_id'), daily.get('error')],
                                      '压缩任务执行失败：' + str(daily.get('error', '请查看处理结果'))))
            for item in daily.get('files', []):
                if item.get('phase') not in FAILURES:
                    continue
                # Test/cancelled/successful receipts may supersede a dated run record.
                receipt = read(Path(item['receipt'])) if item.get('receipt') else None
                if receipt and receipt.get('phase') in {'cancelled_test', 'client_verified', 'cloud_verified'}:
                    continue
                problems.append(issue('file_failed', [item.get('relative'), item.get('error'),
                                      item.get('failure_record'), item.get('receipt')],
                                      f"文件处理失败：{item.get('relative', '未知文件')}；{item.get('error', item['phase'])}"))
    for path in sorted((registry / 'receipts').glob('*.json')):
        row = read(path)
        if not row or row.get('phase') not in PENDING | {'conflict'}:
            continue
        relative = Path(row.get('relative', ''))
        if relative.is_absolute() or '..' in relative.parts or not relative.is_relative_to(cfg.get('scope_relative', '测试')):
            problems.append(issue('receipt_invalid', str(path), '发现不属于配置目录的异常回执，已跳过处理。'))
            continue
        since = read_time(row.get('local_write_started_at') or row.get('created_at'), str(path))
        immediate = row['phase'] in {'attention_required', 'conflict', 'uncertain'}
        if immediate or not since or now - since >= timedelta(minutes=cfg.get('sync_grace_minutes', 15)):
            problems.append(issue('sync_pending', [row.get('relative'), row.get('output_sha256')],
                                  f"同步尚未确认：{relative}；状态 {row['phase']}。请检查企业微信和同步状态。"))
    return list({p['key']: p for p in problems}.values())


def deliver(cfg, payload):
    path = Path(payload['payload_file'])
    shown = path.with_suffix('.shown.json')
    acknowledged = path.with_suffix('.ack.json')
    subprocess.run(['/usr/bin/open', '-n', '-a', cfg['notifier_app'], '--args', str(path)],
                   check=True, capture_output=True, text=True, timeout=15)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if shown.exists() and json.loads(shown.read_text()).get('shown'):
            return {'method': 'macos_alert_dialog', 'shown_receipt': str(shown)}
        if acknowledged.exists() and json.loads(acknowledged.read_text()).get('acknowledged'):
            return {'method': 'macos_alert_dialog', 'acknowledged_receipt': str(acknowledged)}
        time.sleep(0.2)
    raise RuntimeError('native_alert_window_not_confirmed')


def check(cfg, *, now=None, sender=deliver, dry_run=False):
    now = now or datetime.now(TZ)
    home = Path(cfg['state_dir'])
    home.mkdir(parents=True, exist_ok=True)
    with (home / 'monitor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy', 'sent': 0}
        path = home / 'state.json'
        try:
            state = json.loads(path.read_text()) if path.exists() else {'sent': {}}
            if not isinstance(state.get('sent'), dict):
                raise ValueError('invalid notification state')
            state_problem = []
        except (OSError, ValueError, AttributeError) as error:
            state = {'sent': {}}
            state_problem = [issue('monitor_error', 'state', f'提醒去重记录异常，已重建：{type(error).__name__}')]
        try:
            problems = inspect(cfg, now) + state_problem
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
            problems = [issue('monitor_error', [type(error).__name__, str(error)], '任务记录检查失败：' + str(error))] + state_problem
        date = now.date().isoformat()
        unseen = [p for p in problems if state['sent'].get(p['key'], {}).get('date') != date]
        result = {'checked_at': now.isoformat(), 'status': 'healthy' if not problems else 'attention',
                  'problems': problems, 'new_problems': len(unseen), 'sent': 0, 'dry_run': dry_run,
                  'desktop_enabled': cfg.get('desktop', True)}
        if not dry_run:
            current = {'checked_at': now.isoformat(), 'desktop_enabled': result['desktop_enabled'],
                       'open_count': len(problems), 'problems': problems}
            save(home / '当前待处理.json', current)
            text = ('# 压缩与同步待处理清单\n\n更新时间：' + now.isoformat() +
                    '\n\n桌面弹窗：' + ('开启' if result['desktop_enabled'] else '关闭，仅记录日志') +
                    '\n\n' + ('\n\n'.join(p['message'] for p in problems) if problems else '当前无待处理问题。') + '\n')
            (home / '当前待处理.md').write_text(text)
            (home / '提醒记录_最新.md').write_text(text)
            keys = sorted(p['key'] for p in problems)
            if state.get('last_problem_keys') != keys:
                with (home / ('events-' + date + '.jsonl')).open('a') as stream:
                    stream.write(json.dumps({'at': now.isoformat(), 'event': 'problems_changed',
                                             'problems': problems}, ensure_ascii=False) + '\n')
                state['last_problem_keys'] = keys
        if unseen and not dry_run and result['desktop_enabled']:
            stamp = now.strftime('%Y%m%d_%H%M%S_%f')
            payload_file = home / 'notifications' / (stamp + '.json')
            body = '\n\n'.join(p['message'] for p in unseen[:5])
            if len(unseen) > 5:
                body += f'\n\n另有{len(unseen) - 5}项异常，详见提醒记录。'
            payload = {'title': cfg.get('title_prefix', '') + '微盘压缩异常提醒',
                       'body': body[:2400], 'issues': unseen, 'at': now.isoformat(),
                       'report_file': str(Path(cfg['registry']) / '每日结果_最新.md'),
                       'details_file': str(home / '提醒记录_最新.md'), 'payload_file': str(payload_file)}
            save(payload_file, payload)
            (home / '提醒记录_最新.md').write_text('# 微盘压缩异常提醒\n\n' + now.isoformat() + '\n\n' + '\n\n'.join(p['message'] for p in problems) + '\n')
            try:
                result['delivery'] = sender(cfg, payload)
                for p in unseen:
                    state['sent'][p['key']] = {'date': date, 'at': now.isoformat(), 'payload': str(payload_file)}
                result['sent'] = 1
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result.update(status='delivery_failed', delivery_error=str(error))
        # Bounded history; the per-payload logs remain available independently.
        cutoff = (now - timedelta(days=30)).date().isoformat()
        state['sent'] = {k: v for k, v in state['sent'].items() if v['date'] >= cutoff}
        if not dry_run:
            save(path, state)
            save(home / 'health.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    result = check(cfg, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result['status'] == 'delivery_failed':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
