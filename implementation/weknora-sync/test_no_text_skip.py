import unittest
import test_worker as fixtures
from worker import no_text_state
from parse_policy import ParseFailure
from failure_alerts import FailureAlerts

class NoTextSkipTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def fail_no_text(self, *args):
        raise ParseFailure('NO_READABLE_TEXT', 'no_text', False, 'no text confirmed')

    def test_skip_is_terminal_for_same_source_but_changed_content_reenters(self):
        f=self.f
        path=f.source/'photo.jpg'
        path.write_text('original')
        f.worker.renderer=self.fail_no_text
        f.cycle();f.cycle()
        record=next(iter(f.worker.state['records'].values()))
        self.assertEqual(record['state'],'no_text_skipped')
        self.assertEqual(f.api.upload_calls,0)
        self.assertEqual(path.read_text(),'original')
        f.worker.renderer=lambda *a: self.fail('same source reparsed')
        record['blocked_parser_signature']='older-parser-version'
        f.cycle()
        self.assertEqual(record['state'],'no_text_skipped')
        f.worker=f.create_worker()
        f.worker.renderer=f.renderer
        path.write_text('now contains usable body')
        f.cycle();f.cycle()
        self.assertEqual(f.api.upload_calls,1)

    def test_existing_no_text_issue_closes_as_skip_not_success(self):
        f=self.f
        monitor=FailureAlerts({'state_dir':str(f.home),'failure_notifications':{'enabled':True,'desktop':False}})
        r={'path':'photo.jpg','state':'no_text_hold'}
        monitor.observe({'s':r})
        r['state']='no_text_skipped'
        status=monitor.observe({'s':r})
        self.assertEqual(status['open_count'],0)
        text=(f.home/'failure-alerts/当前问题.md').read_text()
        self.assertIn('已跳过',text)
        self.assertNotIn('已恢复同步',text)

    def test_default_and_opt_out(self):
        self.assertEqual(no_text_state({}),'no_text_skipped')
        self.assertEqual(no_text_state({'skip_no_text':False}),'no_text_hold')
