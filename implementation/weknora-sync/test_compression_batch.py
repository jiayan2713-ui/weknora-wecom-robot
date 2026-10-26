import io
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import subprocess
import sys

from PIL import Image

from compress_documents import compress
from compression_batch import prepare, scope_paths, write_local, run, accept_readback, confirm_client_sync, acquire_worker_lock, process_receipt, recover_receipt
from compression_client import ClientUnavailable
from compression_versions import receipt_path, verified_receipt
from reconcile import hashes


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'drive'
        authorized = patch('compression_batch.APPROVED_SOURCE_ROOT', self.root)
        authorized.start()
        self.addCleanup(authorized.stop)
        running = patch('compression_batch.client_status', return_value={'ready': True, 'reason': 'test_fixture'})
        running.start()
        self.addCleanup(running.stop)
        self.scope = self.root / '测试'
        self.scope.mkdir(parents=True)
        self.registry = self.home / 'backups'
        self.registry.mkdir()
        self.worker = self.home / 'worker.json'
        self.state = self.home / 'state'
        self.state.mkdir()
        self.cfg = {'source_root': str(self.root), 'scope_relative': '测试',
                    'compression_registry': str(self.registry), 'worker_config': str(self.worker)}
        self.worker.write_text(json.dumps({'compression_registry': str(self.registry),
                                          'state_dir': str(self.state)}))
        self.source = self.scope / 'person' / 'document.docx'
        self.source.parent.mkdir()
        self.office(self.source)

    def office(self, path):
        buffer = io.BytesIO()
        Image.effect_noise((256, 256), 50).convert('RGB').save(buffer, format='PNG')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', '<document><text>unchanged content</text></document>')
            archive.writestr('word/media/image1.png', buffer.getvalue())
            archive.writestr('word/_rels/document.xml.rels',
                '<Relationships><Relationship Target="media/image1.png"/></Relationships>')
            archive.writestr('[Content_Types].xml',
                '<Types><Default Extension="png" ContentType="image/png"/></Types>')

    def metadata(self, *args):
        return {'file_id': 'existing-cloud-id', 'md5': hashes(self.source)['md5'],
                'size': str(self.source.stat().st_size), 'history_cnt': 1, 'seq_id': '1'}

    def candidate(self):
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            return prepare(self.cfg, self.source, self.root, self.registry)

    def test_only_test_directory_and_external_backups(self):
        for scope in ('测试/../正式', '/测试', '正式'):
            with self.assertRaises(ValueError):
                scope_paths({**self.cfg, 'scope_relative': scope})
        with self.assertRaises(ValueError):
            scope_paths({**self.cfg, 'compression_registry': str(self.scope / 'backup')})

    def test_sales_scope_backup_and_write_stay_in_selected_directory(self):
        self.scope.rename(self.root / '售前文档')
        self.scope = self.root / '售前文档'
        self.source = self.scope / 'person' / 'document.docx'
        self.cfg['scope_relative'] = '售前文档'
        self.assertEqual(scope_paths(self.cfg)[1], self.scope)
        original = hashes(self.source)['sha256']
        row = self.candidate()
        self.assertTrue(row['relative'].startswith('售前文档/'))
        self.assertEqual(hashes(self.registry / row['backup_relative'])['sha256'], original)
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            write_local(self.cfg, row, self.root, self.registry)
        self.assertEqual(hashes(self.source)['sha256'], row['output_sha256'])
        from compression_batch import receipt_source
        with self.assertRaisesRegex(ValueError, 'receipt_source_path_escaped'):
            receipt_source(self.root, row, '测试')

    def test_disabled_apply_cannot_scan_compress_or_write(self):
        with patch('compression_batch.prepare') as prepared:
            with self.assertRaisesRegex(ValueError, 'compression_apply_disabled'):
                run({**self.cfg, 'apply_enabled': False}, apply=True)
            prepared.assert_not_called()

    def test_lifetime_worker_lock_does_not_block_compression(self):
        row = self.candidate()
        with (self.state / 'worker.lock').open('a') as instance:
            fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('compression_batch.client_metadata', side_effect=self.metadata):
                write_local(self.cfg, row, self.root, self.registry)
        self.assertEqual(hashes(self.source)['sha256'], row['output_sha256'])

    def test_active_cycle_blocks_write_and_preserves_original(self):
        row = self.candidate()
        before = hashes(self.source)['sha256']
        with (self.state / 'source-write.lock').open('a') as cycle:
            fcntl.flock(cycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('compression_batch.client_metadata', side_effect=self.metadata):
                with self.assertRaisesRegex(ValueError, 'worker_busy_timeout'):
                    write_local({**self.cfg, 'worker_lock_timeout_seconds': 0}, row, self.root, self.registry)
        self.assertEqual(hashes(self.source)['sha256'], before)

    def test_old_worker_protocol_is_rejected_before_write(self):
        row = self.candidate()
        with self.assertRaisesRegex(ValueError, 'worker_source_write_protocol_not_ready'):
            write_local({**self.cfg, 'require_source_write_lock_protocol': True}, row, self.root, self.registry)

    def test_download_only_file_is_rejected_without_replacement(self):
        row = self.candidate()
        original = hashes(self.source)['sha256']
        meta = {**self.metadata(), 'role_info_raw_json': json.dumps({'role': {'auth': 1}})}
        with patch('compression_batch.client_metadata', return_value=meta):
            with self.assertRaisesRegex(ValueError, 'client_edit_permission_required'):
                write_local(self.cfg, row, self.root, self.registry)
        self.assertEqual(hashes(self.source)['sha256'], original)

    def test_observe_cache_does_not_block_write_or_prove_sync(self):
        row = self.candidate()
        cfg = {**self.cfg, 'permission_cache_policy': 'observe', 'client_sync_timeout_seconds': 0}
        def cached(*args):
            return {**self.metadata(), 'role_info_raw_json': json.dumps({'role': {'auth': 1}, 'data_version': 'old'})}
        with patch('compression_batch.client_metadata', side_effect=cached):
            write_local(cfg, row, self.root, self.registry)
            self.assertEqual(row['permission_cache_check']['cached_auth'], 1)
            self.assertFalse(row['permission_cache_check']['live_permission_verified'])
            with self.assertRaisesRegex(ValueError, 'client_sync_acknowledgement_timeout'):
                confirm_client_sync(cfg, row, self.root, self.registry)
        self.assertEqual(row['phase'], 'local_replaced')

    def test_invalid_cache_policy_is_rejected(self):
        from compression_batch import require_write_permission
        with self.assertRaisesRegex(ValueError, 'unknown_permission_cache_policy'):
            require_write_permission({'permission_cache_policy': 'typo'}, {})

    def test_worker_cycle_releases_source_lock_but_keeps_singleton(self):
        from worker import Sync, writer_session
        worker = Sync.__new__(Sync)
        worker.home = self.state
        worker._owns_lifetime_lock = True
        with writer_session({'state_dir': str(self.state)}):
            with worker.cycle_guard():
                with (self.state / 'source-write.lock').open('a') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (self.state / 'source-write.lock').open('a') as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                with writer_session({'state_dir': str(self.state)}):
                    self.fail('second worker admitted')

    def test_sales_scope_rejects_traversal_and_sibling_prefix(self):
        from compression_batch import receipt_source
        self.cfg['scope_relative'] = '售前文档'
        for scope in ('售前文档/../测试', '/售前文档', '.', '售前文档/子目录'):
            with self.assertRaises(ValueError):
                scope_paths({**self.cfg, 'scope_relative': scope})
        for relative in ('测试/a.docx', '售前文档副本/a.docx', '售前文档/../测试/a.docx'):
            with self.assertRaises(ValueError):
                receipt_source(self.root, {'source_root': str(self.root), 'relative': relative}, '售前文档')

    def test_symlink_scope_and_source_are_rejected(self):
        self.source.unlink()
        external = self.home / 'outside.docx'
        self.office(external)
        self.source.symlink_to(external)
        with self.assertRaises(ValueError):
            self.candidate()

    def test_backup_first_preserves_content_and_directory_hierarchy(self):
        original = hashes(self.source)['sha256']
        real_compress = compress
        def check_backup(backup, output):
            self.assertEqual(hashes(backup)['sha256'], original)
            self.assertIn('测试/person', str(backup))
            return real_compress(backup, output)
        with patch('compression_batch.compress', side_effect=check_backup):
            row = self.candidate()
        self.assertEqual(hashes(self.source)['sha256'], original)
        self.assertEqual(self.candidate()['created_at'], row['created_at'])

    def test_employee_edit_is_never_overwritten(self):
        row = self.candidate()
        self.source.write_bytes(b'employee changed the file')
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            with self.assertRaisesRegex(ValueError, 'employee_update_detected'):
                write_local(self.cfg, row, self.root, self.registry)
        self.assertEqual(self.source.read_bytes(), b'employee changed the file')

    def test_local_write_keeps_inode_and_requires_cloud_before_adoption(self):
        row = self.candidate()
        inode = self.source.stat().st_ino
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            written = write_local(self.cfg, row, self.root, self.registry)
        self.assertEqual(self.source.stat().st_ino, inode)
        self.assertEqual(hashes(self.source)['sha256'], row['output_sha256'])
        self.assertEqual(written['phase'], 'local_replaced')
        with self.assertRaisesRegex(ValueError, 'cloud_verification_pending'):
            verified_receipt({'compression_registry': str(self.registry)}, row['relative'], row['output_sha256'])
        with self.assertRaises(ValueError):
            write_local(self.cfg, written, self.root, self.registry)

    def test_invalid_candidate_and_backup_block_write(self):
        row = self.candidate()
        (self.registry / row['backup_relative']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'integrity_failed'):
            write_local(self.cfg, row, self.root, self.registry)

    def test_office_text_and_relationships_are_preserved(self):
        output = self.home / 'candidate.docx'
        report = compress(self.source, output)
        with zipfile.ZipFile(self.source) as before, zipfile.ZipFile(output) as after:
            self.assertEqual(before.read('word/document.xml'), after.read('word/document.xml'))
            self.assertIn('media/image1_compressed.jpg', after.read('word/_rels/document.xml.rels').decode())
        self.assertFalse(report['visual_review_complete'])

    def test_batch_activation_requires_actual_cloud_evidence(self):
        result = run(self.cfg, apply=True)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('successful_cloud_round_trip', result['error'])
        result = run({**self.cfg, 'cloud_round_trip_accepted': True}, apply=True)
        self.assertIn('evidence_missing', result['error'])
        self.assertTrue(result['finished_at'])

    def test_readback_cannot_use_candidate_or_sync_source(self):
        row = self.candidate()
        receipt = receipt_path(self.registry, row['relative'], row['output_sha256'])
        with self.assertRaisesRegex(ValueError, 'not_be_local_source_or_candidate'):
            accept_readback(self.cfg, receipt, self.registry / row['candidate_relative'], 'existing-cloud-id', 'v2')
        with self.assertRaisesRegex(ValueError, 'not_be_local_source_or_candidate'):
            accept_readback(self.cfg, receipt, self.source, 'existing-cloud-id', 'v2')

    def test_client_acknowledgement_reuses_backup_without_claiming_cloud_readback(self):
        row = self.candidate()
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            write_local(self.cfg, row, self.root, self.registry)
        meta = {**self.metadata(), 'history_cnt': 2, 'seq_id': '2'}
        with patch('compression_batch.client_metadata', return_value=meta):
            result = confirm_client_sync(self.cfg, row, self.root, self.registry)
        self.assertEqual(result['phase'], 'client_verified')
        self.assertIsNone(result['cloud_version'])
        self.assertFalse(result['client_sync_evidence']['independent_cloud_download'])
        worker = {'source_root': str(self.root), 'compression_registry': str(self.registry),
                  'compression_client_sync_roots': ['测试']}
        self.assertEqual(verified_receipt(worker, row['relative'], row['output_sha256'])['phase'], 'client_verified')
        with self.assertRaises(ValueError):
            verified_receipt({**worker, 'compression_client_sync_roots': ['other']}, row['relative'], row['output_sha256'])

    def test_unchanged_client_version_does_not_confirm_sync(self):
        row = self.candidate()
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            write_local(self.cfg, row, self.root, self.registry)
            times = iter([0, 10])
            with self.assertRaisesRegex(ValueError, 'acknowledgement_timeout'):
                confirm_client_sync({**self.cfg, 'client_sync_timeout_seconds': 5}, row, self.root,
                                    self.registry, clock=lambda: next(times), pause=lambda _: None)

    def test_worker_lock_waits_for_sync_cycle_instead_of_skipping_document(self):
        with (self.state / 'worker.lock').open('a') as lock:
            with patch('compression_batch.fcntl.flock', side_effect=[BlockingIOError(), None]) as attempt:
                acquire_worker_lock(lock, 5, clock=lambda: 0, pause=lambda _: None)
                self.assertEqual(attempt.call_count, 2)

    def test_restored_large_original_gets_new_timestamp_backup_and_archived_receipt(self):
        row = self.candidate()
        with patch('compression_batch.client_metadata', side_effect=self.metadata):
            write_local(self.cfg, row, self.root, self.registry)
        meta = {**self.metadata(), 'history_cnt': 2, 'seq_id': '2'}
        with patch('compression_batch.client_metadata', return_value=meta):
            confirm_client_sync(self.cfg, row, self.root, self.registry)
        self.source.write_bytes((self.registry / row['backup_relative']).read_bytes())
        new = self.candidate()
        self.assertNotEqual(new['backup_relative'], row['backup_relative'])
        self.assertEqual(new['phase'], 'prepared')
        self.assertEqual(len(list((self.registry / 'receipt-history').glob('*.json'))), 1)

    def legacy_pending(self):
        row = self.candidate()
        before = self.metadata()
        self.source.write_bytes((self.registry / row['candidate_relative']).read_bytes())
        row['phase'] = 'local_replaced'
        receipt_path(self.registry, row['relative'], row['output_sha256']).write_text(json.dumps(row))
        return row, before

    def test_stopped_client_never_overwrites_prepared_source(self):
        row = self.candidate()
        original = self.source.read_bytes()
        with patch('compression_batch.client_status', return_value={'ready': False, 'reason': 'client_not_running'}):
            with self.assertRaises(ClientUnavailable):
                write_local(self.cfg, row, self.root, self.registry)
            report = run({**self.cfg, 'acceptance_mode': 'client_sync'}, apply=True, resume_only=True)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(report['counts'], {'waiting_client': 1})
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(json.loads(Path(report['files'][0]['receipt']).read_text())['phase'], 'prepared')

    def test_reopened_client_resumes_prepared_without_manual_file_event(self):
        row = self.candidate()
        def metadata(*_):
            result = self.metadata()
            if hashes(self.source)['sha256'] == row['output_sha256']:
                result.update(history_cnt=2, seq_id='2')
            return result
        with patch('compression_batch.client_metadata', side_effect=metadata):
            report = run({**self.cfg, 'acceptance_mode': 'client_sync'}, apply=True, resume_only=True)
        self.assertEqual(report['counts'], {'client_verified': 1})
        self.assertEqual(hashes(self.source)['sha256'], row['output_sha256'])

    def test_legacy_pending_emits_one_mtime_event_and_preserves_bytes(self):
        row, before = self.legacy_pending()
        after = {**self.metadata(), 'history_cnt': 2, 'seq_id': '2'}
        contents = self.source.read_bytes()
        with patch('compression_batch.os.utime', wraps=__import__('os').utime) as event:
            with patch('compression_batch.client_metadata', side_effect=lambda *_: after if event.call_count else before):
                result = recover_receipt(self.cfg, row, self.root, self.registry)
        self.assertEqual(event.call_count, 1)
        self.assertEqual(result['phase'], 'client_verified')
        self.assertEqual(result['retrigger_count'], 1)
        self.assertEqual(self.source.read_bytes(), contents)

    def test_acknowledged_version_is_never_retriggered(self):
        row, _ = self.legacy_pending()
        after = {**self.metadata(), 'history_cnt': 2, 'seq_id': '2'}
        with patch('compression_batch.os.utime') as event, patch('compression_batch.client_metadata', return_value=after):
            recover_receipt(self.cfg, row, self.root, self.registry)
        event.assert_not_called()

    def test_retry_limit_stops_events_and_accepts_late_ack(self):
        row, before = self.legacy_pending()
        cfg = {**self.cfg, 'client_sync_timeout_seconds': 0, 'recovery_max_attempts': 2}
        with patch('compression_batch.os.utime', wraps=__import__('os').utime) as event:
            with patch('compression_batch.client_metadata', return_value=before):
                first = process_receipt(cfg, row, self.root, self.registry, recovery=True)
                second = process_receipt(cfg, row, self.root, self.registry, recovery=True)
                third = process_receipt(cfg, row, self.root, self.registry, recovery=True)
            self.assertEqual(first['phase'], 'waiting_sync')
            self.assertEqual(second['phase'], 'waiting_sync')
            self.assertEqual(third['phase'], 'attention_required')
            self.assertEqual(event.call_count, 1)
            after = {**self.metadata(), 'history_cnt': 2, 'seq_id': '2'}
            with patch('compression_batch.client_metadata', return_value=after):
                recovered = process_receipt(cfg, row, self.root, self.registry, recovery=True)
            self.assertEqual(recovered['phase'], 'client_verified')
            self.assertEqual(event.call_count, 1)

    def test_employee_change_is_isolated_and_other_receipt_completes(self):
        bad = self.candidate()
        second = self.source.with_name('second.docx')
        self.office(second)
        relative = str(second.relative_to(self.root))
        def metadata(_cfg, path):
            source = self.root / path
            digest = hashes(source)
            old = bad['original_sha256'] if path == bad['relative'] else original_second
            return {'file_id': 'existing-cloud-id' if path == bad['relative'] else 'second-id',
                    'md5': digest['md5'], 'size': str(digest['size']),
                    'history_cnt': 1 if digest['sha256'] == old else 2,
                    'seq_id': '1' if digest['sha256'] == old else '2'}
        original_second = hashes(second)['sha256']
        with patch('compression_batch.client_metadata', side_effect=metadata):
            good = prepare(self.cfg, second, self.root, self.registry)
            self.source.write_bytes(b'employee changed content while waiting')
            report = run({**self.cfg, 'acceptance_mode': 'client_sync'}, apply=True, resume_only=True)
        self.assertEqual(report['status'], 'needs_attention')
        self.assertEqual(report['counts'], {'client_verified': 1, 'conflict': 1})
        self.assertEqual(self.source.read_bytes(), b'employee changed content while waiting')
        self.assertEqual(hashes(second)['sha256'], good['output_sha256'])

    def test_run_results_preserve_history_even_when_scope_missing(self):
        self.source.unlink(); self.source.parent.rmdir(); self.scope.rmdir()
        report = run({**self.cfg, 'acceptance_mode': 'client_sync'}, apply=True, resume_only=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error'], 'scope_missing')
        self.assertTrue(report['finished_at'])
        self.assertEqual(json.loads((self.registry / 'last-recovery.json').read_text())['run_id'], report['run_id'])
        self.assertTrue((self.registry / 'runs' / (report['run_id'] + '.json')).exists())

    def test_daily_start_is_visible_before_work_and_ends_with_same_run_id(self):
        def observe_start(_):
            marker = json.loads((self.registry / 'active-daily.json').read_text())
            self.assertEqual(marker['status'], 'running')
            self.assertTrue(marker['apply'])
            return {'ready': True}
        with patch('compression_batch.client_status', side_effect=observe_start):
            report = run({**self.cfg, 'acceptance_mode': 'client_sync'}, apply=True)
        marker = json.loads((self.registry / 'active-daily.json').read_text())
        self.assertEqual(marker['run_id'], report['run_id'])
        self.assertEqual(marker['status'], 'success')
        self.assertTrue(marker['finished_at'])

    def test_invalid_startup_config_still_emits_timestamped_failure(self):
        cfg = self.home / 'invalid.json'
        cfg.write_text('{invalid')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('compression_batch.py')),
                                 '--config', str(cfg), '--resume-only', '--apply'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error_type'], 'JSONDecodeError')
        self.assertTrue(report['started_at'])
        self.assertTrue(report['finished_at'])

    def test_recovery_budget_reports_waiting_instead_of_all_done(self):
        first = self.candidate()
        second = self.source.with_name('budget-second.docx')
        self.office(second)
        originals = {first['relative']: first['original_sha256'], str(second.relative_to(self.root)): hashes(second)['sha256']}
        def metadata(_cfg, relative):
            digest = hashes(self.root / relative)
            version = 1 if digest['sha256'] == originals[relative] else 2
            return {'file_id': 'existing-cloud-id' if relative == first['relative'] else 'second-id',
                    'size': str(digest['size']), 'md5': digest['md5'], 'seq_id': str(version), 'history_cnt': version}
        with patch('compression_batch.client_metadata', side_effect=metadata):
            prepare(self.cfg, second, self.root, self.registry)
            report = run({**self.cfg, 'acceptance_mode': 'client_sync', 'recovery_max_files_per_run': 1},
                         apply=True, resume_only=True)
        self.assertEqual(report['status'], 'waiting')
        self.assertEqual(report['remaining_receipts'], 1)
        self.assertEqual(report['counts'], {'client_verified': 1})


if __name__ == '__main__':
    unittest.main()
