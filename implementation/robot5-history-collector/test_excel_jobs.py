import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import collector
import excel_jobs
import crm_sync
import test_collector as fixtures


class ExcelJobTests(unittest.TestCase):
    setUp=fixtures.HistoryTests.setUp
    tearDown=fixtures.HistoryTests.tearDown
    message=fixtures.HistoryTests.message
    apply=fixtures.HistoryTests.apply

    def test_async_cycle_does_not_wait_for_export(self):
        snapshot={'rows':[self.message('u','user'),self.message('a','assistant')],
                  'source':{'tenant_id':1},'upper':'2026-10-23T07:00:00Z'}
        events=[]
        def queue(*args,**kwargs):events.append('queue');return {'status':'queued'}
        def crm(*args,**kwargs):events.append('crm');return {'status':'success'}
        with patch.object(collector,'query_source',return_value=snapshot),patch.object(collector,'export',side_effect=AssertionError('must not render synchronously')),patch.object(excel_jobs,'request_export',side_effect=queue),patch.object(crm_sync,'tick',side_effect=crm):
            result=collector.run_cycle(self.db,self.root,{**self.binding,'export_async':True})
        self.assertEqual(events,['crm','queue'])
        self.assertEqual(result['excel_export']['status'],'queued')
        self.assertEqual(len(collector.qa_records(self.db)),1)

    def test_busy_worker_is_not_spawned_twice(self):
        with (self.root/'excel.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(excel_jobs.subprocess,'Popen') as spawn:
                self.assertEqual(excel_jobs.request_export(self.root)['status'],'running')
                self.assertEqual(excel_jobs.run_export(self.root)['status'],'already_running')
                spawn.assert_not_called()

    def test_parent_releases_lock_before_starting_child(self):
        def child(*args,**kwargs):
            with (self.root/'excel.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with patch.object(excel_jobs.subprocess,'Popen',side_effect=child) as spawn:
            result=excel_jobs.request_export(self.root)
        self.assertEqual(result['status'],'queued')
        self.assertIn('export-job',spawn.call_args.args[0])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_worker_uses_snapshot_without_blocking_new_archive_writes(self):
        self.apply([self.message('u','user'),self.message('a','assistant')])
        def render(db,root,records,version,*args):
            self.assertFalse(db.in_transaction)
            writer=collector.connect(root)
            try:
                collector.apply_snapshot(writer,{'rows':[self.message('u2','user','req2'),self.message('a2','assistant','req2')],
                    'source':{'tenant_id':1},'upper':'2026-10-23T07:01:00Z'},self.binding)
            finally:writer.close()
            self.assertEqual(len(records),1)
            with db:db.execute("INSERT INTO state VALUES('export_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(version,))
        with patch.object(collector,'export_snapshot',side_effect=render):
            result=excel_jobs.run_export(self.root)
        self.assertTrue(result['more_pending'])
        self.assertEqual(result['records'],1)
        self.assertEqual(len(collector.qa_records(self.db)),2)
        self.assertNotEqual(self.db.execute("SELECT value FROM state WHERE key='export_version'").fetchone()[0],collector.export_version(self.db))

    def test_failed_export_preserves_previous_workbook_and_has_retry_state(self):
        target=self.root/'机器人5问答.xlsx';target.write_bytes(b'last-good-book')
        with patch.object(collector,'export_snapshot',side_effect=RuntimeError('private source text')):
            result=excel_jobs.run_export(self.root)
        self.assertEqual(result['status'],'failed')
        self.assertGreater(result['next_retry'],0)
        self.assertEqual(target.read_bytes(),b'last-good-book')
        self.assertNotIn('private source text',(self.root/'excel-health.json').read_text())
        with patch.object(excel_jobs.subprocess,'Popen') as spawn:
            self.assertEqual(excel_jobs.request_export(self.root)['status'],'retry_pending')
            spawn.assert_not_called()

    def test_start_failure_is_recorded_without_failing_collection(self):
        with patch.object(excel_jobs.subprocess,'Popen',side_effect=OSError('cannot start')):
            result=excel_jobs.request_export(self.root)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['code'],'EXPORT_WORKER_START_FAILED')

    def test_database_open_failure_has_durable_error_state(self):
        with patch.object(collector,'connect',side_effect=sqlite3.OperationalError('locked')):
            result=excel_jobs.run_export(self.root)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_type'],'OperationalError')
        self.assertGreater(result['next_retry'],0)


if __name__=='__main__':unittest.main()
