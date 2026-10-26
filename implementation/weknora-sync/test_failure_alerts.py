import copy
import json
from pathlib import Path
import tempfile
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from failure_alerts import FailureAlerts, desktop_notice


class FailureAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.now=1792116000
        self.sent=[]
        self.cfg={'state_dir':str(self.root),'failure_notifications':{'enabled':True,'desktop':True}}
        self.monitor=self.create()

    def create(self,notifier=None):
        return FailureAlerts(self.cfg,clock=lambda:self.now,
                             notifier=notifier or (lambda *args:self.sent.append(args) or 'submitted'))

    def failed(self,code='UNIT_COVERAGE_INCOMPLETE',version='a'*64):
        return {'path':'咨询skill/示例.xlsx','state':'parse_blocked_hold','blocked_source_hash':version,
                'parse_failure':{'code':code,'category':'quality','retryable':False},
                'parse_attempts':1,'remote_ids':['old-version'],
                'latest_parse_report_path':str(self.root/'jobs/job/parse-report.json')}

    def current(self):
        return json.loads((self.monitor.root/'current.json').read_text())

    def events(self):
        return [json.loads(line) for p in self.monitor.root.glob('events-*.jsonl') for line in p.read_text().splitlines()]

    def test_backlog_is_grouped_and_survives_restart_without_repeated_notices(self):
        rows={str(i):self.failed() for i in range(7)}
        self.monitor.observe(rows)
        self.assertEqual(len(self.sent),1)
        self.assertIn('7项',self.sent[0][1])
        self.monitor.observe(rows,complete=True)
        self.now+=120
        self.create().observe(rows,complete=True)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len([e for e in self.events() if e['event']=='failure']),7)
        self.assertEqual(self.current()['open_count'],7)
        self.assertTrue(all(p.stat().st_mode & 0o777==0o600 for p in self.monitor.root.iterdir() if p.is_file()))
        summary=(self.monitor.root/'当前问题.md').read_text()
        self.assertIn('解析单元缺正文或覆盖不完整',summary)
        self.assertIn('已有活动版本：保留',summary)

    def test_same_episode_error_oscillation_does_not_repeat_and_new_version_notifies(self):
        self.monitor.observe({'s':self.failed()})
        self.now+=61
        self.monitor.observe({'s':self.failed('MODEL_TRUNCATED')})
        self.now+=61
        self.monitor.observe({'s':self.failed()})
        self.assertEqual(len(self.sent),2)
        self.now+=61
        self.monitor.observe({'s':self.failed(version='b'*64)})
        self.assertEqual(len(self.sent),3)

    def test_recovery_requires_active_without_pending_and_recurrence_notifies(self):
        self.monitor.observe({'s':self.failed()})
        self.monitor.observe({'s':{'path':'示例.xlsx','state':'queued'}})
        self.monitor.observe({'s':{'path':'示例.xlsx','state':'active','pending':{'phase':'learning'}}})
        self.assertEqual(self.current()['open_count'],1)
        self.monitor.observe({'s':{'path':'示例.xlsx','state':'active'}})
        self.assertEqual(self.current()['open_count'],0)
        self.assertEqual(len([e for e in self.events() if e['event']=='recovered']),1)
        self.now+=61
        self.monitor.observe({'s':self.failed()})
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.current()['issues'][0]['episode'],2)

    def test_source_removed_is_closed_without_claiming_recovery(self):
        self.monitor.observe({'s':self.failed()})
        self.monitor.observe({'s':{'state':'deleted'}})
        self.assertEqual(self.current()['open_count'],0)
        self.assertEqual(self.events()[-1]['event'],'closed_source_removed_or_excluded')

    def test_transient_retries_log_immediately_but_popup_only_on_escalation(self):
        row={'path':'例子.txt','state':'retry_wait','attempts':1,'error':'ConnectionError: private detail',
             'latest_observed_hash':'c'*64,'next_attempt':self.now+30}
        self.monitor.observe({'s':row})
        self.assertEqual(len(self.sent),0)
        self.assertEqual(self.current()['issues'][0]['status'],'正在自动重试')
        row['attempts']=3
        self.monitor.observe({'s':row})
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.current()['issues'][0]['code'],'NETWORK_ERROR')
        self.assertNotIn('private detail',(self.monitor.root/'当前问题.md').read_text())

    def test_service_failure_and_recovery_do_not_close_on_partial_observation(self):
        self.monitor.observe(service_error=ValueError('source_root_identity_changed_protective_pause'))
        self.assertEqual(self.current()['issues'][0]['code'],'ROOT_UNAVAILABLE')
        self.monitor.observe({'s':{'state':'active'}})
        self.assertEqual(self.current()['open_count'],1)
        self.monitor.observe({'s':{'state':'active'}},complete=True)
        self.assertEqual(self.current()['open_count'],0)

    def test_unknown_exception_and_recovery_text_are_not_copied_into_logs(self):
        secret='sk-private-test-do-not-log'
        self.monitor.observe(service_error=RuntimeError('Authorization: Bearer '+secret))
        row=self.failed();row['parse_failure']['recovery']=secret;row['error']='ValueError:'+secret
        self.monitor.observe({'s':row})
        for path in self.monitor.root.iterdir():
            if path.is_file():self.assertNotIn(secret,path.read_text())

    def test_desktop_failure_is_recorded_and_retried_after_backoff(self):
        attempts=[]
        def notifier(*args):
            attempts.append(args)
            if len(attempts)==1:raise RuntimeError('notification helper unavailable')
            return 'submitted'
        monitor=self.create(notifier)
        monitor.observe({'s':self.failed()})
        self.assertEqual(self.current()['desktop_status'],'command_failed')
        self.now+=299;monitor.observe({'s':self.failed()});self.assertEqual(len(attempts),1)
        self.now+=1;monitor.observe({'s':self.failed()});self.assertEqual(len(attempts),2)
        monitor.observe({'s':self.failed()});self.assertEqual(len(attempts),2)
        self.assertFalse(self.current()['banner_delivery_verified'])

    def test_expected_wait_and_excluded_states_do_not_open_issues(self):
        self.monitor.observe({str(i):{'path':'例子','state':state} for i,state in enumerate(
            ['active','queued','waiting_stable','missing_hold','excluded_by_policy','compression_cloud_hold'])})
        self.assertEqual(self.current()['open_count'],0)
        self.assertEqual(self.sent,[])

    def test_disabled_monitor_does_not_create_files_or_send(self):
        cfg={'state_dir':str(self.root/'disabled')}
        FailureAlerts(cfg,notifier=lambda *args:self.fail('sent')).observe({'s':self.failed()})
        self.assertFalse((self.root/'disabled').exists())

    def test_desktop_script_uses_arguments_not_interpolated_source(self):
        message='文件名含 " 引号 $(not-a-command) `data`'
        with patch('failure_alerts.sys.platform','darwin'),patch('failure_alerts.subprocess.run',
                return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(desktop_notice('同步提醒',message),'submitted')
        args=execute.call_args.args[0]
        self.assertEqual(args[-1],message)
        self.assertNotIn(message,args[2])
        self.assertNotIn('shell',execute.call_args.kwargs)

    def test_report_adds_version_matched_coverage_without_original_body(self):
        report=self.root/'jobs/job/parse-report.json';report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'source_hash':'a'*64,'expected_units':97,'completed_units':95,
            'missing_body_units':2,'units':[{'label':'破冰 A1:R24','status':'no_readable_text','body':'do-not-copy-original'}]}))
        self.monitor.observe({'s':self.failed()})
        summary=(self.monitor.root/'当前问题.md').read_text()
        self.assertIn('95/97',summary);self.assertIn('破冰 A1:R24',summary)
        self.assertNotIn('do-not-copy-original',summary)
        self.monitor.observe({'s':self.failed(version='b'*64)})
        self.assertIsNone(self.current()['issues'][0]['coverage'])

    def test_migrated_journal_keeps_deduplication(self):
        self.monitor.observe({'s':self.failed()})
        target=self.root/'new-machine';target.mkdir()
        shutil.copytree(self.monitor.root,target/'failure-alerts')
        new=FailureAlerts({**self.cfg,'state_dir':str(target)},clock=lambda:self.now+120,
                          notifier=lambda *a:self.fail('duplicate notice after migration'))
        new.observe({'s':self.failed()})
        self.assertEqual(json.loads((new.root/'current.json').read_text())['open_count'],1)


if __name__=='__main__':unittest.main()
