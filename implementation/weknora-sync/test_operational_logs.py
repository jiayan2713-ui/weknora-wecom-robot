import contextlib
import datetime as dt
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from operational_logs import OperationalLogs


class OperationalLogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.now=dt.datetime(2026,10,16,tzinfo=dt.timezone.utc).timestamp()
        self.cfg={'state_dir':str(self.home),'source_root':str(self.home/'source'),
                  'operational_logging':{'enabled':True,'segment_bytes':1024}}
        self.logs=OperationalLogs(self.cfg,clock=lambda:self.now)
        self.sid='a'*24;self.sha='b'*64

    def test_durable_events_are_not_limited_to_state_ring(self):
        for i in range(1105):self.logs.record('phase_completed',{'source_id':self.sid,'source_hash':self.sha,'attempts':i})
        rows=[]
        for p in (self.logs.root/'audit').glob('*.jsonl'):rows.extend(json.loads(x) for x in p.read_text().splitlines())
        self.assertEqual(len(rows),1105)
        self.assertGreater(len(list((self.logs.root/'audit').glob('*.jsonl'))),1)
        self.assertTrue(all(p.stat().st_mode & 0o777==0o600 for p in (self.logs.root/'audit').iterdir()))

    def test_legacy_import_is_once_and_does_not_invent_version(self):
        legacy=[{'time':self.now-100,'kind':'version_activated','source_id':self.sid,'remote_ids':['old-id']}]
        self.logs.bootstrap(legacy)
        newer=OperationalLogs(self.cfg,clock=lambda:self.now)
        newer.bootstrap(legacy)
        rows=self.logs.query(self.sid)['events']
        self.assertEqual(len(rows),1)
        self.assertNotIn('source_hash',rows[0])
        self.assertEqual(rows[0]['knowledge_ids'],['old-id'])
        self.assertTrue(rows[0]['legacy_import'])
        self.assertEqual(rows[0]['timestamp'],self.now-100)

    def test_trace_follows_rename_and_filters_content_version(self):
        self.logs.record('source_discovered',{'source_id':self.sid,'source_hash':self.sha,'path':'旧.txt'})
        self.logs.record('rename_detected',{'source_id':self.sid,'source_hash':self.sha,'old':'旧.txt','new':'新.txt'})
        self.logs.record('version_activated',{'source_id':self.sid,'source_hash':'c'*64,'path':'新.txt'})
        self.assertEqual(len(self.logs.query(path='旧.txt')['events']),3)
        self.assertEqual(len(self.logs.query(path='新.txt',source_hash=self.sha)['events']),2)

    def test_initial_snapshot_keeps_published_and_pending_versions_distinct(self):
        records={self.sid:{'path':'旧文件.txt','source_hash':self.sha,'latest_observed_hash':'c'*64,
                          'remote_ids':['1'],'state':'learning','pending':{'source_hash':'d'*64,'remote_ids':['2']},'latest_parse_report_path':str(self.home/'jobs'/('e'*64)/'parse-report.json')}}
        self.logs.baseline(records,'kb')
        self.logs.baseline(records,'kb')
        for value in (self.sha,'c'*64,'d'*64):
            rows=self.logs.query(path='旧文件.txt',source_hash=value)['events']
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['event'],'logging_baseline_snapshot')
            self.assertEqual(rows[0]['knowledge_ids'],['1'])
            self.assertEqual(rows[0]['pending_knowledge_ids'],['2'])
            self.assertEqual(rows[0]['job_id'],'e'*64)

    def test_retention_archives_audit_but_only_purges_generated_routine_logs(self):
        self.logs.record('version_activated',{'source_id':self.sid,'source_hash':self.sha})
        self.logs.record('cycle_completed',{},runtime=True)
        keep=self.logs.root/'runtime/manual-evidence.txt';keep.write_text('keep')
        raw=self.home/'jobs/original.txt';raw.parent.mkdir();raw.write_text('private original')
        self.now+=8*86400;self.logs.maintain(force=True)
        self.assertTrue(list((self.logs.root/'audit').glob('*.gz')))
        self.assertEqual(len(self.logs.query(self.sid)['events']),1)
        self.now+=40*86400;self.logs.maintain(force=True)
        self.assertEqual(list((self.logs.root/'runtime').glob('runtime-*')),[])
        self.assertEqual(len(self.logs.query(self.sid)['events']),1)
        self.assertEqual(keep.read_text(),'keep');self.assertEqual(raw.read_text(),'private original')

    def test_existing_raw_output_is_private_archived_without_sparse_output(self):
        raw=self.home/'watch.stdout.log';raw.write_text('old private diagnostics')
        cfg={**self.cfg,'operational_logging':{**self.cfg['operational_logging'],'runtime_files':[str(raw)]}}
        logs=OperationalLogs(cfg,clock=lambda:self.now)
        with raw.open('a') as stream,contextlib.redirect_stdout(stream):
            logs.bootstrap();print('new output',flush=True)
        archives=list((logs.root/'private-legacy').glob('*.gz'))
        self.assertEqual(len(archives),1)
        with gzip.open(archives[0],'rt') as stream:self.assertEqual(stream.read(),'old private diagnostics')
        self.assertEqual(raw.read_text(),'new output\n')
        self.now+=100*86400;logs.maintain(force=True)
        self.assertTrue(archives[0].exists())

    def test_raw_rotation_refuses_source_files_and_non_log_names(self):
        source=self.home/'source';source.mkdir()
        a=source/'watch.stdout.log';a.write_text('original data')
        b=self.home/'api-key.txt';b.write_text('private key')
        cfg={**self.cfg,'operational_logging':{'enabled':True,'runtime_files':[str(a),str(b)]}}
        OperationalLogs(cfg,clock=lambda:self.now).bootstrap()
        self.assertEqual(a.read_text(),'original data');self.assertEqual(b.read_text(),'private key')

    def test_metadata_allowlist_and_support_export_exclude_private_contents(self):
        secret='sk-fixture-never-share'
        self.logs.record('parse_failure',{'source_id':self.sid,'source_hash':self.sha,'code':'MISSING_BODY',
                         'body':secret,'question':secret,'answer':secret,'headers':{'Authorization':secret},
                         'error':secret,'config':{'api_key':secret}})
        private=self.logs.root/'private-legacy/raw.log';private.write_text(secret)
        evidence=self.home/'jobs/source.txt';evidence.parent.mkdir();evidence.write_text(secret)
        fake=self.logs.root/'runtime/runtime-2026-10-16.9999.jsonl'
        fake.write_text(json.dumps({'event_id':'f'*32,'service_run_id':'d'*32,'timestamp':self.now,
                                    'event':'cycle_completed','body':secret,'pid':123})+'\n')
        failures=self.home/'failure-alerts';failures.mkdir()
        (failures/'current.json').write_text(json.dumps({'issues':[{'path':'例子.xlsx','source_id':self.sid,
            'code':'MISSING_BODY','reason':secret,'body':secret,'report_path':str(self.home/'jobs/job/parse-report.json')},]}))
        destination=self.home/'support.zip';self.logs.export(destination)
        self.assertEqual(destination.stat().st_mode & 0o777,0o600)
        with zipfile.ZipFile(destination) as archive:
            self.assertTrue(all(name.startswith(('audit/','runtime/','failures/')) or name=='说明.md' for name in archive.namelist()))
            self.assertNotIn(secret,b''.join(archive.read(name) for name in archive.namelist()).decode())
            rows=[json.loads(line) for line in archive.read('runtime/'+fake.name).decode().splitlines()]
            self.assertEqual(rows[0]['service_run_id'],'d'*32);self.assertEqual(rows[0]['pid'],123)
            failure=json.loads(archive.read('failures/current.json'))['issues'][0]
            self.assertEqual(failure['report_ref'],'jobs/job/parse-report.json')
            self.assertNotEqual(failure['reason'],secret)
        self.assertEqual(evidence.read_text(),secret)
        with self.assertRaises(ValueError):self.logs.export(destination)

    def test_export_refuses_wedrive_destination(self):
        with self.assertRaises(ValueError):self.logs.export(self.home/'source/leak.zip')

    def test_corrupt_records_are_reported_and_logging_failure_does_not_escape(self):
        self.logs.record('service_started')
        path=next((self.logs.root/'audit').glob('*.jsonl'))
        with path.open('a') as stream:stream.write('{broken\nnull\n')
        query=self.logs.query();self.assertEqual(len(query['events']),1);self.assertEqual(query['read_errors'],2)
        with patch.object(self.logs,'locked',side_effect=OSError('private detail')):
            self.logs.record('source_discovered',{'source_id':self.sid})
        self.assertEqual(self.logs.last_error,'OSError')


if __name__=='__main__':unittest.main()
