from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from compression_alerts import check, save


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.registry = self.home / 'registry'
        self.cfg = {'registry': str(self.registry), 'state_dir': str(self.home / 'alerts'),
                    'notifier_app': '/not/used/in/tests.app'}
        self.now = datetime(2026, 10, 15, 10, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.sent = []
        self.daily = {'run_id': 'daily1', 'apply': True, 'mode': 'daily', 'status': 'success',
                      'started_at': self.now.replace(hour=10, minute=0).isoformat(),
                      'finished_at': self.now.replace(hour=10, minute=1).isoformat(), 'files': []}
        self.write_daily()

    def write_daily(self):
        save(self.registry / 'last-daily.json', self.daily)

    def sender(self, cfg, payload):
        self.sent.append(payload)
        return {'method': 'test_sender'}

    def run_check(self, now=None):
        return check(self.cfg, now=now or self.now, sender=self.sender)

    def receipt(self, phase='local_replaced', age=20):
        row = {'phase': phase, 'relative': '测试/a.xlsx', 'output_sha256': 'version1',
               'local_write_started_at': (self.now - timedelta(minutes=age)).isoformat()}
        path = self.registry / 'receipts' / 'a.json'
        save(path, row)
        return path, row

    def test_healthy_task_is_silent(self):
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.assertEqual(self.sent, [])

    def test_enabling_after_daily_time_only_monitors_next_due_run(self):
        self.cfg['schedule_enabled_at'] = self.now.isoformat()
        (self.registry / 'last-daily.json').unlink()
        result = self.run_check()
        self.assertFalse(any(p['kind'] == 'daily_missing' for p in result['problems']))
        result = self.run_check(now=self.now + timedelta(days=1))
        self.assertTrue(any(p['kind'] == 'daily_missing' for p in result['problems']))

    def test_muted_errors_keep_current_list_and_history_without_sending(self):
        self.cfg['desktop'] = False
        self.daily.update(status='failed', error='test_task_failed')
        self.write_daily()
        self.receipt(age=20)
        result = self.run_check()
        self.assertFalse(result['desktop_enabled'])
        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.sent, [])
        home = Path(self.cfg['state_dir'])
        current = json.loads((home / '当前待处理.json').read_text())
        self.assertEqual(current['open_count'], 2)
        journal = home / ('events-' + self.now.date().isoformat() + '.jsonl')
        self.run_check()
        self.assertEqual(len(journal.read_text().splitlines()), 1)
        self.daily.update(status='success', files=[])
        self.daily.pop('error')
        self.write_daily()
        path, row = self.receipt()
        row['phase'] = 'client_verified'
        save(path, row)
        self.run_check()
        self.assertEqual(len(journal.read_text().splitlines()), 2)
        self.assertIn('当前无待处理问题', (home / '当前待处理.md').read_text())
        self.assertFalse((home / 'notifications').exists())
        self.assertEqual(self.sent, [])

    def test_configured_three_am_schedule_and_message_follow_new_time(self):
        self.cfg['daily_hour'] = 3
        tomorrow = (self.now + timedelta(days=1)).replace(hour=3, minute=14)
        self.assertEqual(self.run_check(tomorrow)['sent'], 0)
        result = self.run_check(tomorrow.replace(minute=15))
        self.assertEqual(result['sent'], 1)
        self.assertEqual([p['kind'] for p in result['problems']], ['daily_missing'])
        self.assertIn('03:00', result['problems'][0]['message'])

    def test_no_run_alert_after_grace_but_not_before_or_for_morning_dry_run(self):
        self.daily['started_at'] = self.now.replace(hour=9, minute=50).isoformat()
        self.write_daily()
        early = self.now.replace(hour=10, minute=14)
        self.assertEqual(self.run_check(early)['sent'], 0)
        result = self.run_check(self.now.replace(minute=15))
        self.assertEqual(result['problems'][0]['kind'], 'daily_missing')
        self.assertEqual(result['sent'], 1)

    def test_current_active_run_prevents_false_missing_alert_and_stall_is_detected(self):
        (self.registry / 'last-daily.json').unlink()
        active = {**self.daily, 'status': 'running'}
        active.pop('finished_at')
        save(self.registry / 'active-daily.json', active)
        self.assertEqual(self.run_check()['sent'], 0)
        result = self.run_check(self.now.replace(hour=11, minute=1))
        self.assertEqual([p['kind'] for p in result['problems']], ['daily_stalled'])

    def test_failed_file_alerts_once_per_day_and_changed_failure_alerts_again(self):
        self.daily.update(status='needs_attention', files=[{'relative':'测试/a.pdf','phase':'failed','error':'target_size_not_reached_without_resizing'}])
        self.write_daily()
        self.assertEqual(self.run_check()['sent'], 1)
        self.assertEqual(self.run_check()['sent'], 0)
        self.daily['files'][0]['error'] = 'pdf_render_validation_failed'
        self.write_daily()
        self.assertEqual(self.run_check()['sent'], 1)

    def test_pending_sync_has_grace_period_and_late_success_is_silent(self):
        path, row = self.receipt(age=5)
        self.assertEqual(self.run_check()['sent'], 0)
        row['local_write_started_at'] = (self.now - timedelta(minutes=20)).isoformat()
        save(path, row)
        self.assertEqual(self.run_check()['sent'], 1)
        self.assertEqual(self.run_check()['sent'], 0)
        row['phase'] = 'client_verified'
        save(path, row)
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.assertEqual(len(self.sent), 1)

    def test_cancelled_test_and_old_failure_superseded_by_receipt_do_not_alert(self):
        path, row = self.receipt('cancelled_test')
        self.daily.update(status='needs_attention', files=[{'relative':'测试/a.xlsx','phase':'failed','receipt':str(path)}])
        self.write_daily()
        self.assertEqual(self.run_check()['sent'], 0)

    def test_delivery_failure_is_not_marked_sent_and_next_check_retries(self):
        self.daily.update(status='failed', error='config_error')
        self.write_daily()
        def failing(*_):
            raise RuntimeError('native_app_not_ready')
        result = check(self.cfg, now=self.now, sender=failing)
        self.assertEqual(result['status'], 'delivery_failed')
        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.run_check()['sent'], 1)

    def test_previous_day_report_does_not_mask_missing_run(self):
        self.daily['started_at'] = (self.now - timedelta(days=1)).isoformat()
        self.write_daily()
        self.assertEqual(self.run_check()['problems'][0]['kind'], 'daily_missing')

    def test_dry_run_sends_nothing_and_does_not_consume_deduplication(self):
        self.daily.update(status='failed', error='test')
        self.write_daily()
        self.assertEqual(check(self.cfg, now=self.now, sender=self.sender, dry_run=True)['sent'], 0)
        self.assertEqual(self.run_check()['sent'], 1)

    def test_corrupt_report_or_time_is_reported_instead_of_crashing(self):
        (self.registry / 'last-daily.json').write_text('{invalid')
        result = self.run_check()
        self.assertIn('report_error', {p['kind'] for p in result['problems']})
        self.daily['started_at'] = 'not-a-time'
        self.write_daily()
        self.assertIn('report_error', {p['kind'] for p in self.run_check()['problems']})

    def test_unconfirmed_receipt_from_older_day_still_alerts(self):
        self.receipt(age=24*60)
        self.assertEqual(self.run_check()['problems'][0]['kind'], 'sync_pending')

    def test_repeated_error_is_allowed_again_next_day_without_success_notice(self):
        self.receipt(age=20)
        self.assertEqual(self.run_check()['sent'], 1)
        tomorrow = self.now + timedelta(days=1)
        result = self.run_check(tomorrow)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(len(self.sent), 2)


if __name__ == '__main__':
    unittest.main()
