import contextlib
import copy
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from worker import Sync, rename_pairs
from parse_policy import ParseFailure


class Opener:
    def __init__(self, api):
        self.api = api

    def open(self, request, timeout=None):
        identity = request.full_url.split('/knowledge/')[1].split('/')[0]
        return contextlib.closing(io.BytesIO(self.api.raw[identity]))


class Remote:
    def __init__(self):
        self.docs = {}
        self.raw = {}
        self.deleted = []
        self.upload_calls = 0
        self.drop_response_once = False
        self.probe_ok = True
        self.base = 'http://fake/api/v1'
        self.auth = {}
        self.opener = Opener(self)

    def inventory(self):
        return list(self.docs.values())

    def complete(self, identity):
        self.docs[identity].update(parse_status='completed', enable_status='enabled', pending_subtasks_count=0)

    def request(self, route, data=None, headers=None, method=None):
        if route.endswith('/knowledge/file'):
            self.upload_calls += 1
            message = BytesParser(policy=default).parsebytes(
                ('Content-Type: ' + headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + data)
            parts = {p.get_param('name', header='content-disposition'): p.get_payload(decode=True) for p in message.iter_parts()}
            raw = parts['file']
            fingerprint = hashlib.md5(raw).hexdigest()
            existing = next((d for d in self.docs.values() if d['file_hash'] == fingerprint), None)
            if existing:
                return 409, {'success': False, 'data': existing}
            identity = str(len(self.raw) + 1)
            relative = parts['fileName'].decode()
            self.raw[identity] = raw
            doc = {'id': identity, 'knowledge_base_id': 'kb', 'file_hash': fingerprint, 'file_size': len(raw),
                   'file_type': 'md', 'file_name': Path(relative).name, 'folder_path': str(Path(relative).parent),
                   'parse_status': 'processing', 'enable_status': 'disabled', 'pending_subtasks_count': 1}
            self.docs[identity] = doc
            if self.drop_response_once:
                self.drop_response_once = False
                raise ConnectionError('response_lost')
            return 201, {'success': True, 'data': doc}
        if route == '/knowledge/folder':
            value = json.loads(data)
            for identity in value['knowledge_ids']:
                self.docs[identity]['folder_path'] = value['folder_path']
            return 200, {'success': True}
        if route.startswith('/knowledge/'):
            identity = route.split('/')[-1]
            if method == 'DELETE':
                self.docs.pop(identity, None)
                self.deleted.append(identity)
                return 200, {'success': True}
            return (200, {'success': True, 'data': self.docs[identity]}) if identity in self.docs else (404, {})
        if route.startswith('/chunks/'):
            identity = route.split('/')[2].split('?')[0]
            return 200, {'data': [{'chunk_type': 'text', 'is_enabled': True, 'content': self.raw[identity].decode()}]}
        if route.endswith('/hybrid-search'):
            value = json.loads(data)
            return 200, {'data': [{'knowledge_id': value['knowledge_ids'][0]}] if self.probe_ok else []}
        raise AssertionError(route)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / 'source'
        self.source.mkdir()
        self.md = base / 'markdown'
        self.md.mkdir()
        self.home = base / 'state'
        self.baseline = base / 'baseline.json'
        self.baseline.write_text('[]')
        self.now = 0
        self.api = Remote()
        self.parse_calls = 0
        self.cfg = {'source_root': str(self.source), 'markdown_root': str(self.md), 'state_dir': str(self.home),
                    'knowledge_base_id': 'kb', 'weknora_base_url': 'http://fake', 'folder_root': '项目to咨询',
                    'baseline_mapping': str(self.baseline), 'stable_seconds': 0, 'delete_grace_seconds': 0,
                    'max_jobs_per_cycle': 10, 'llm_enabled': False}
        self.worker = self.create_worker()

    def create_worker(self):
        return Sync(self.cfg, api=self.api, renderer=self.renderer, clock=lambda: self.now)

    def renderer(self, cfg, source, relative, digest, work):
        self.parse_calls += 1
        work.mkdir(parents=True, exist_ok=True)
        artifact = work / 'publication.md'
        artifact.write_text(source.read_text())
        return artifact, {'method': 'test_local_text', 'source_hash': digest}

    def cycle(self):
        self.now += 1
        return self.worker.cycle()

    def initial(self, content='V1', filename='x.txt'):
        path = self.source / filename
        path.write_text(content)
        self.cycle()
        self.cycle()
        self.api.complete('1')
        self.cycle()
        return path

    def test_update_waits_for_index_before_retiring_old(self):
        path = self.initial()
        path.write_text('V2')
        self.cycle()
        self.cycle()
        self.assertNotIn('1', self.api.deleted)
        self.api.complete('2')
        self.cycle()
        self.assertIn('1', self.api.deleted)
        self.assertEqual((self.md / 'x.txt.md').read_text(), 'V2')

    def bulk_fixture(self):
        self.cfg.update(automatic_deletion_enabled=True,mass_missing_floor=0,mass_missing_fraction=0)
        path=self.initial()
        path.unlink()
        with self.assertRaisesRegex(ValueError,'mass_source_missing'):
            self.cycle()
        self.assertEqual(self.worker.confirm_missing_batch()['confirmed_sources'],1)
        return path

    def test_mass_threshold_can_be_disabled_without_skipping_deletion_scans(self):
        self.cfg.update(automatic_deletion_enabled=True,mass_missing_floor=0,mass_missing_fraction=0,
                        mass_missing_protection_enabled=False)
        self.initial().unlink()
        self.cycle();self.cycle()
        self.assertEqual(self.api.deleted,[])
        self.cycle()
        self.assertEqual(self.api.deleted,['1'])
        self.assertFalse(self.worker.cycle()['mass_missing_protection_enabled'])

    def test_disabling_mass_threshold_keeps_root_identity_protection(self):
        self.cfg['mass_missing_protection_enabled']=False
        self.worker.state['root_identity']=[0,0]
        with self.assertRaisesRegex(ValueError,'source_root_identity_changed'):
            self.worker.inventory()

    def test_bulk_confirmation_preserves_grace_backups_and_deletion_pipeline(self):
        self.bulk_fixture()
        self.cycle();self.cycle()
        self.assertEqual(self.api.deleted,[])
        self.cycle()
        self.assertEqual(self.api.deleted,['1'])
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'],'deleted')
        self.assertTrue(list((self.home/'history').rglob('*.md')))
        self.assertEqual(self.cfg['mass_missing_fraction'],0)

    def test_bulk_confirmation_does_not_authorize_additional_missing_file(self):
        path=self.initial()
        other=self.source/'other.txt';other.write_text('other')
        record=copy.deepcopy(next(iter(self.worker.state['records'].values())))
        record['path']='other.txt'
        self.worker.state['records']['other-source']=record
        self.worker.persist()
        self.cfg.update(automatic_deletion_enabled=True,mass_missing_floor=0,mass_missing_fraction=0)
        path.unlink();other.unlink()
        manifest=self.home/'operator-manifest.json'
        manifest.write_text(json.dumps({'missing':['x.txt']}))
        self.assertEqual(self.worker.confirm_missing_batch(manifest)['confirmed_sources'],1)
        with self.assertRaisesRegex(ValueError,'mass_source_missing'):
            self.worker.inventory()
        other.write_text('other')
        self.worker.inventory()
        other.unlink()
        with self.assertRaisesRegex(ValueError,'mass_source_missing'):
            self.worker.inventory()

    def test_bulk_confirmation_is_revoked_on_reappearance(self):
        path=self.bulk_fixture()
        path.write_text('V1');self.worker.inventory();path.unlink()
        with self.assertRaisesRegex(ValueError,'mass_source_missing'):
            self.worker.inventory()
        self.assertEqual(json.loads((self.home/'deletion-confirmations.json').read_text()),{})

    def test_bulk_confirmation_expires_and_is_bound_to_version_root_and_kb(self):
        self.bulk_fixture()
        path=self.home/'deletion-confirmations.json'
        proofs=json.loads(path.read_text());identity=next(iter(proofs))
        for field,value in [('source_hash','other-version'),('root_identity',[0,0]),
                            ('knowledge_base_id','other-kb'),('expires_at',self.now-1),
                            ('pending_source_hash','unapproved-pending-version')]:
            with self.subTest(field=field):
                changed=copy.deepcopy(proofs);changed[identity][field]=value
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError,'mass_source_missing'):
                    self.worker.inventory()

    def test_bulk_confirmation_does_not_claim_client_sync_or_cloud_api_verification(self):
        self.bulk_fixture()
        proof=next(iter(json.loads((self.home/'deletion-confirmations.json').read_text()).values()))
        self.assertEqual(proof['evidence'],'operator_confirmed_bulk_cloud_deletion')
        self.assertNotIn('sync_completed_at',proof)

    def test_failure_journal_tracks_real_worker_failure_then_verified_recovery(self):
        self.cfg['failure_notifications']={'enabled':True,'desktop':True}
        self.worker=self.create_worker();sent=[]
        self.worker.failure_alerts.notifier=lambda *args:sent.append(args) or 'submitted'
        (self.source/'x.txt').write_text('document')
        self.worker.renderer=lambda *a: (_ for _ in ()).throw(ParseFailure('UNIT_COVERAGE_INCOMPLETE','quality',False,'repair'))
        self.cycle();self.cycle();self.cycle()
        current=self.home/'failure-alerts/current.json'
        self.assertEqual(json.loads(current.read_text())['open_count'],1)
        self.assertEqual(len(sent),1)
        self.worker.renderer=self.renderer
        identity=next(iter(self.worker.state['records']))
        self.worker.request_parse_retry(identity)
        self.cycle();self.api.complete('1');self.cycle()
        self.assertEqual(json.loads(current.read_text())['open_count'],0)
        self.assertEqual(self.worker.state['records'][identity]['state'],'active')

    def test_failure_logger_error_does_not_block_successful_sync(self):
        with patch.object(self.worker.failure_alerts,'observe',side_effect=OSError('fixture disk error')):
            self.initial()
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'],'active')
        self.assertEqual(self.worker.failure_alert_status['logging_error'],'OSError')

    def test_durable_file_trace_records_full_update_and_delete_chain(self):
        self.cfg['operational_logging']={'enabled':True,'segment_bytes':1024*1024}
        self.worker=self.create_worker();path=self.initial()
        identity,record=next(iter(self.worker.state['records'].items()))
        v1=record['source_hash']
        path.write_text('V2');self.cycle();self.cycle();self.api.complete('2');self.cycle()
        v2=record['source_hash']
        rows=self.worker.logs.query(identity,source_hash=v2)['events'];names=[row['event'] for row in rows]
        for expected in ['processing_started','parse_started','parse_completed','artifact_prepared','upload_started','upload_accepted','index_and_retrieval_verified','version_activated','retirement_verified']:
            self.assertIn(expected,names)
        activation=next(row for row in rows if row['event']=='version_activated')
        self.assertEqual(activation['previous_source_hash'],v1)
        self.assertEqual(activation['knowledge_ids'],['2'])
        self.assertEqual(activation['previous_knowledge_ids'],['1'])
        self.assertTrue(all(row['job_id']==activation['job_id'] for row in rows))
        self.cfg['automatic_deletion_enabled']=True
        path.unlink();self.cycle();self.cycle();self.cycle()
        self.assertEqual(record['state'],'deleted')
        self.assertIn('source_deletion_propagated',[row['event'] for row in self.worker.logs.query(identity)['events']])
        self.assertIn('2',self.api.deleted)

    def test_operational_log_io_failure_does_not_change_sync_result(self):
        self.cfg['operational_logging']={'enabled':True}
        self.worker=self.create_worker()
        with patch.object(self.worker.logs,'locked',side_effect=OSError('fixture log disk error')):
            self.initial()
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'],'active')
        self.assertIn('1',self.api.docs)

    def test_operational_logging_enabled_and_disabled_have_identical_sync_results(self):
        results=[]
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                home=Path(temp).resolve();source=home/'source';source.mkdir();markdown=home/'markdown';markdown.mkdir()
                baseline=home/'baseline.json';baseline.write_text('[]');clock=[0];api=Remote()
                cfg={'source_root':str(source),'markdown_root':str(markdown),'state_dir':str(home/'state'),
                     'knowledge_base_id':'kb','weknora_base_url':'http://fake','folder_root':'项目to咨询',
                     'baseline_mapping':str(baseline),'stable_seconds':0,'delete_grace_seconds':0,
                     'automatic_deletion_enabled':True,'max_jobs_per_cycle':10,'llm_enabled':False,
                     'operational_logging':{'enabled':enabled}}
                worker=Sync(cfg,api=api,renderer=self.renderer,clock=lambda:clock[0])
                def cycle():clock[0]+=1;worker.cycle()
                path=source/'x.txt';path.write_text('V1');cycle();cycle();api.complete('1');cycle()
                path.write_text('V2');cycle();cycle();api.complete('2');cycle()
                path.rename(source/'renamed.txt');cycle();cycle()
                (source/'renamed.txt').unlink();cycle();cycle();cycle()
                results.append({'records':[{k:row.get(k) for k in ('path','state','source_hash','md_sha','remote_ids')}
                                           for row in worker.state['records'].values()],
                                'uploads':api.upload_calls,'deletions':api.deleted,'retire':worker.state['retire']})
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[1]['records'][0]['state'],'deleted')

    def test_logger_callback_failure_cannot_abort_sync_event(self):
        self.cfg['operational_logging']={'enabled':True};self.worker=self.create_worker()
        with patch.object(self.worker.logs,'record',side_effect=RuntimeError('fixture logger callback failure')):
            self.initial()
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'],'active')

    def test_missing_is_not_deletion_even_after_repeated_scans(self):
        path = self.initial()
        path.unlink()
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.assertTrue((self.md / 'x.txt.md').exists())

    def test_deletion_requires_matching_sync_evidence(self):
        path = self.initial()
        path.unlink()
        self.cycle()
        identity, record = next(iter(self.worker.state['records'].items()))
        (self.home / 'deletion-confirmations.json').write_text(json.dumps({identity: {
            'confirmed': True, 'source_hash': record['source_hash'], 'sync_completed_at': self.now + 1}}))
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])
        self.assertFalse((self.md / 'x.txt.md').exists())

    def test_rename_reuses_parse_and_aligns_folder(self):
        path = self.initial()
        target = self.source / 'moved'
        target.mkdir()
        path.rename(target / 'x.txt')
        self.cycle()
        self.cycle()
        self.assertEqual(self.parse_calls, 1)
        self.assertEqual(self.api.upload_calls, 1)
        self.assertEqual(self.api.docs['1']['folder_path'], '项目to咨询/moved')
        self.assertFalse((self.md / 'x.txt.md').exists())
        self.assertTrue((self.md / 'moved/x.txt.md').exists())

    def test_automatic_deletion_waits_for_grace_and_needs_no_receipt(self):
        self.cfg.update(automatic_deletion_enabled=True, delete_grace_seconds=900)
        path = self.initial()
        path.unlink()
        self.cycle()
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.now += 901
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])
        self.assertFalse((self.md / 'x.txt.md').exists())
        self.assertTrue(list((self.home / 'history').rglob('*.md')))
        self.assertTrue((self.home / 'retired-backups/1/original.md').exists())

    def test_automatic_deletion_requires_three_complete_missing_scans(self):
        self.cfg['automatic_deletion_enabled'] = True
        self.initial().unlink()
        self.cycle()
        self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])

    def test_short_grace_still_requires_both_time_and_complete_scans(self):
        self.cfg.update(automatic_deletion_enabled=True, delete_grace_seconds=60)
        self.initial().unlink()
        self.cycle()
        self.cycle()
        record = next(iter(self.worker.state['records'].values()))
        self.now = record['missing_since'] + 58
        self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])

    def test_reappearance_cancels_automatic_deletion_timer(self):
        self.cfg.update(automatic_deletion_enabled=True, delete_grace_seconds=900)
        path = self.initial()
        path.unlink()
        self.cycle()
        path.write_text('V1')
        self.now += 901
        self.cycle()
        self.cycle()
        self.assertEqual(self.api.deleted, [])
        record = next(iter(self.worker.state['records'].values()))
        self.assertNotIn('missing_since', record)
        self.assertEqual(record['state'], 'active')

    def test_placeholder_does_not_trigger_automatic_deletion(self):
        self.cfg['automatic_deletion_enabled'] = True
        path = self.initial()
        path.unlink()
        (self.source / '.x.txt.icloud').write_text('placeholder')
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'], 'placeholder_hold')

    def test_shared_entry_only_deleted_after_last_source_disappears(self):
        self.cfg['automatic_deletion_enabled'] = True
        a, b = self.source / 'a.txt', self.source / 'b.txt'
        a.write_text('shared')
        b.write_text('shared')
        self.cycle()
        self.cycle()
        self.api.complete('1')
        self.cycle()
        a.unlink()
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.assertTrue((self.md / 'b.txt.md').exists())
        b.unlink()
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.api.deleted, ['1'])

    def test_pending_new_version_cleaned_up_when_source_deleted(self):
        self.cfg['automatic_deletion_enabled'] = True
        path = self.initial()
        path.write_text('V2')
        self.cycle()
        self.cycle()
        path.unlink()
        for _ in range(3):
            self.cycle()
        self.assertEqual(set(self.api.deleted), {'1', '2'})
        self.assertNotIn('pending', next(iter(self.worker.state['records'].values())))

    def test_response_loss_recovers_without_duplicate_upload(self):
        (self.source / 'x.txt').write_text('V1')
        self.api.drop_response_once = True
        self.cycle()
        self.cycle()
        self.api.complete('1')
        self.now += 60
        self.worker = self.create_worker()
        self.cycle()
        self.assertEqual(self.api.upload_calls, 1)
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'], 'active')

    def test_shared_entry_survives_one_source_update(self):
        a, b = self.source / 'a.txt', self.source / 'b.txt'
        a.write_text('shared')
        b.write_text('shared')
        self.cycle()
        self.cycle()
        self.api.complete('1')
        self.cycle()
        a.write_text('changed')
        self.cycle()
        self.cycle()
        self.api.complete('2')
        self.cycle()
        self.assertIn('1', self.api.docs)
        self.assertEqual((self.md / 'b.txt.md').read_text(), 'shared')

    def test_probe_failure_keeps_previous_version(self):
        path = self.initial()
        path.write_text('V2')
        self.cycle()
        self.cycle()
        self.api.complete('2')
        self.api.probe_ok = False
        self.cycle()
        self.assertIn('1', self.api.docs)
        self.assertEqual((self.md / 'x.txt.md').read_text(), 'V1')

    def test_root_replacement_is_protective_pause(self):
        self.cfg['automatic_deletion_enabled'] = True
        self.initial()
        self.source.rename(self.source.with_name('old-root'))
        self.source.mkdir()
        with self.assertRaisesRegex(ValueError, 'root_identity'):
            self.cycle()
        self.assertEqual(self.api.deleted, [])

    def test_ambiguous_equal_content_is_not_classified_as_rename(self):
        self.assertEqual(rename_pairs({'1': {'source_hash': 'h'}, '2': {'source_hash': 'h'}},
                                     {'new.txt': {'sha256': 'h'}}), [])

    def test_capacity_failure_holds_changed_version_and_keeps_previous_knowledge(self):
        path = self.initial()
        path.write_text('V2')
        def blocked(*args):
            self.parse_calls += 1
            raise ParseFailure('CAPACITY_EXCEEDED', 'capacity', False, '修复原件后重试')
        self.worker.renderer = blocked
        self.cycle()
        self.cycle()
        calls = self.parse_calls
        for _ in range(4):
            self.now += 3600
            self.cycle()
        record = next(iter(self.worker.state['records'].values()))
        self.assertEqual(record['state'], 'parse_blocked_hold')
        self.assertEqual(record['parse_failure']['code'], 'CAPACITY_EXCEEDED')
        self.assertEqual(self.parse_calls, calls)
        self.assertIn('1', self.api.docs)
        self.assertEqual((self.md / 'x.txt.md').read_text(), 'V1')
        self.assertEqual(self.api.deleted, [])

    def test_changed_source_releases_parse_hold(self):
        path = self.initial()
        path.write_text('V2')
        self.worker.renderer = lambda *a: (_ for _ in ()).throw(
            ParseFailure('INVALID_DOCUMENT', 'document', False, '修复原件后重试'))
        self.cycle()
        self.cycle()
        path.write_text('V3 repaired')
        self.worker.renderer = self.renderer
        self.cycle()
        self.cycle()
        self.api.complete('2')
        self.cycle()
        record = next(iter(self.worker.state['records'].values()))
        self.assertEqual(record['state'], 'active')
        self.assertNotIn('parse_failure', record)
        self.assertEqual((self.md / 'x.txt.md').read_text(), 'V3 repaired')

    def test_transient_parse_failure_retries_with_backoff_and_limit(self):
        (self.source / 'x.txt').write_text('V1')
        def failing(*args):
            self.parse_calls += 1
            raise ParseFailure('NETWORK_ERROR', 'network', True, '等待网络恢复')
        self.worker.renderer = failing
        self.cycle()
        self.cycle()
        self.assertEqual(self.parse_calls, 1)
        self.cycle()
        self.assertEqual(self.parse_calls, 1)
        self.now += 31
        self.cycle()
        self.now += 61
        self.cycle()
        self.assertEqual(self.parse_calls, 3)
        record = next(iter(self.worker.state['records'].values()))
        self.assertEqual(record['state'], 'parse_blocked_hold')
        self.assertTrue(record['parse_failure']['retry_exhausted'])
        self.now += 3600
        self.cycle()
        self.assertEqual(self.parse_calls, 3)

    def test_targeted_retry_is_consumed_by_live_writer_and_uses_new_job(self):
        path = self.initial()
        path.write_text('V2')
        self.worker.renderer = lambda *a: (_ for _ in ()).throw(
            ParseFailure('INVALID_DOCUMENT', 'document', False, '修复原件后重试'))
        self.cycle()
        self.cycle()
        identity, record = next(iter(self.worker.state['records'].items()))
        self.create_worker().request_parse_retry(identity)
        self.assertIn('parse_failure', record)
        self.worker.renderer = self.renderer
        self.cycle()
        self.api.complete('2')
        self.cycle()
        self.assertEqual(record['state'], 'active')
        self.assertEqual(record['parse_retry_generation'], 1)
        self.assertNotIn('parse_failure', record)
        self.assertEqual((self.md / 'x.txt.md').read_text(), 'V2')

    def checkpoint_fixture(self,path):
        from reconcile import hashes
        work=self.home/'jobs/original';work.mkdir(parents=True,exist_ok=True)
        (work/path.name).write_bytes(path.read_bytes())
        report=work/'parse-report.json'
        report.write_text(json.dumps({'source_hash':hashes(path)['sha256'],'status':'incomplete'}))
        return report

    def test_checkpoint_retry_preserves_pending_on_failure_then_retires_only_after_verified(self):
        path=self.source/'x.xlsx';path.write_text('original source')
        self.cycle();self.cycle()
        identity,record=next(iter(self.worker.state['records'].items()))
        before=copy.deepcopy(record['pending'])
        report=self.checkpoint_fixture(path)
        self.create_worker().request_parse_retry(identity,report)
        # Submitting a request cannot rewrite the live writer's pending state.
        self.assertEqual(record['pending'],before)
        self.worker.renderer=lambda *a: (_ for _ in ()).throw(ParseFailure('UNIT_COVERAGE_INCOMPLETE','quality',False,'still incomplete'))
        self.cycle()
        self.assertEqual(record['pending'],before)
        self.assertIn('1',self.api.docs)
        self.assertEqual(self.api.deleted,[])
        self.create_worker().request_parse_retry(identity,report)
        def repaired(cfg,source,relative,digest,work):
            self.assertEqual(cfg['_xlsx_checkpoint_work'],str(report.parent.resolve()))
            artifact,provenance=self.renderer(cfg,source,relative,digest,work)
            artifact.write_text('new verified region content')
            return artifact,provenance
        self.worker.renderer=repaired
        self.cycle()
        self.assertEqual(record['pending']['supersedes_pending'],before)
        self.assertEqual(record['pending']['superseded_ids'],['1'])
        self.assertEqual(self.api.deleted,[])
        self.api.complete('2');self.cycle()
        self.assertEqual(record['state'],'active')
        self.assertEqual(record['remote_ids'],['2'])
        self.assertIn('1',self.api.deleted)

    def test_checkpoint_request_tampering_is_rejected_without_changing_record(self):
        path=self.initial(filename='x.xlsx')
        identity,record=next(iter(self.worker.state['records'].items()))
        report=self.checkpoint_fixture(path)
        self.create_worker().request_parse_retry(identity,report)
        report.write_text(report.read_text()+' ')
        before=copy.deepcopy(record)
        self.worker.apply_parse_retries()
        self.assertEqual(record,before)
        self.assertEqual(self.api.deleted,[])

    def test_active_unchanged_source_can_be_reparsed_without_reusing_cached_markdown(self):
        path=self.initial(filename='x.xlsx')
        identity,record=next(iter(self.worker.state['records'].items()))
        source_hash=record['source_hash'];report=self.checkpoint_fixture(path)
        self.create_worker().request_parse_retry(identity,report)
        def repaired(cfg,source,relative,digest,work):
            result,provenance=self.renderer(cfg,source,relative,digest,work)
            result.write_text('parser repaired content')
            return result,provenance
        self.worker.renderer=repaired
        self.cycle()
        self.assertEqual(record['remote_ids'],['1'])
        self.assertEqual(record['pending']['remote_ids'],['2'])
        self.assertEqual(self.api.deleted,[])
        self.api.complete('2');self.cycle()
        self.assertEqual(record['remote_ids'],['2'])
        self.assertEqual(record['source_hash'],source_hash)
        self.assertIn('1',self.api.deleted)

    def test_supplier_wait_does_not_consume_retry_budget_or_poll_before_deadline(self):
        (self.source / 'x.txt').write_text('V1')
        def waiting(*args):
            self.parse_calls += 1
            raise ParseFailure('RETRY_AFTER', 'transient', True, '等待供应商恢复', retry_at=300)
        self.worker.renderer = waiting
        self.cycle()
        self.cycle()
        record = next(iter(self.worker.state['records'].values()))
        self.assertEqual(record.get('parse_attempts', 0), 0)
        self.assertEqual(record['next_attempt'], 300)
        self.now = 100
        self.cycle()
        self.assertEqual(self.parse_calls, 1)
        self.now = 300
        self.cycle()
        self.assertEqual(record['state'], 'parse_retry_wait')
        self.assertEqual(record.get('parse_attempts', 0), 0)

    def test_parse_hold_survives_service_restart(self):
        (self.source / 'x.txt').write_text('V1')
        self.worker.renderer = lambda *a: (_ for _ in ()).throw(
            ParseFailure('INVALID_DOCUMENT', 'document', False, '修复后重试'))
        self.cycle()
        self.cycle()
        self.worker = self.create_worker()
        self.cycle()
        self.assertEqual(self.parse_calls, 0)
        self.assertEqual(next(iter(self.worker.state['records'].values()))['state'], 'parse_blocked_hold')

    def test_background_entrypoint_imports_project_packages_from_other_directory(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.cfg))
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('worker.py').resolve()),
                                 'status', '--config', str(config)],
                                cwd=self.temp.name, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'not_started')


if __name__ == '__main__':
    unittest.main()
