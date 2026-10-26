import unittest
from unittest.mock import patch
import crm_sync as crm
import crm_web
from answer_format import render_answer
import test_crm_web as web_tests
import test_collector as collector_tests
import collector


class WebLatencyTests(unittest.TestCase):
    setUp=web_tests.WebSyncTests.setUp
    tearDown=web_tests.WebSyncTests.tearDown
    sync=web_tests.WebSyncTests.sync

    def test_planning_does_not_consume_upload_budget(self):
        clock=[0.0]
        def slow_render(source):
            clock[0]+=25
            return render_answer(source)
        with patch.object(crm,'Client',return_value=self.client),patch.object(self.client,'list_access',return_value=self.schema),patch.object(crm_web,'render_answer',side_effect=slow_render),patch.object(crm_web.time,'monotonic',side_effect=lambda:clock[0]):
            result=crm_web.run(self.db,[self.qa],self.config,limit=10,budget=20)
        self.assertEqual(result['verified'],1)

    def test_restart_reuses_persisted_html_without_rendering_history(self):
        self.sync();render_answer.cache_clear()
        with patch.object(crm_web,'render_answer',side_effect=AssertionError('history must use persistent cache')):
            self.assertEqual(self.sync()['attempted'],0)

    def test_new_row_contains_both_answer_formats_on_create(self):
        qa={**self.qa,'assistant_message_id':'new','record_id':'new','user_message_id':'new-user'}
        with patch.object(self.client,'list_access',return_value=self.schema):
            crm.run(self.db,[qa],self.config,self.client,limit=10,budget=30)
        self.assertEqual(self.client.rows['remote2']['answer'],qa['answer'])
        self.assertEqual(self.client.rows['remote2']['web'],render_answer(qa['answer']))


class CollectionLatencyTests(unittest.TestCase):
    setUp=collector_tests.HistoryTests.setUp
    tearDown=collector_tests.HistoryTests.tearDown
    message=collector_tests.HistoryTests.message

    def test_crm_runs_after_archive_commit_before_slow_export(self):
        import crm_sync
        events=[]
        snapshot={'rows':[self.message('u','user'),self.message('a','assistant')],
                  'source':{'tenant_id':1},'upper':'2026-10-23T07:00:00Z'}
        def crm_tick(*args,**kwargs):
            self.assertEqual(len(collector.qa_records(self.db)),1)
            events.append('crm');return {'status':'success'}
        def export(*args,**kwargs):
            events.append('export');raise RuntimeError('slow_export_timeout')
        with patch.object(collector,'query_source',return_value=snapshot),patch.object(collector,'export',side_effect=export),patch.object(crm_sync,'tick',side_effect=crm_tick):
            with self.assertRaisesRegex(RuntimeError,'slow_export_timeout'):
                collector.run_cycle(self.db,self.root,self.binding)
        self.assertEqual(events,['crm','export'])
