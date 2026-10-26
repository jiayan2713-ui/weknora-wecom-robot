import json
from pathlib import Path
import unittest
from unittest.mock import patch

from compression_versions import stage, publish, confirm_download, receipt_path, parsing_source
from reconcile import hashes, save
from transform_local import render
from parse_policy import policy_signature
import test_worker


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.env = test_worker.WorkerTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        self.original = self.env.initial()
        self.cfg = dict(self.env.cfg, compression_registry=str(self.env.home / 'compression'))
        self.candidate = self.env.home / 'compressed.txt'
        self.candidate.write_text('compressed representation')
        self.digest = hashes(self.candidate)['sha256']
        self.validation = {'original_sha256_before': hashes(self.original)['sha256'],
                           'original_unchanged': True, 'output_sha256': self.digest,
                           'status': 'passed', 'visual_review_complete': True}
        self.row = stage(self.cfg, 'x.txt', self.candidate, self.validation)
        self.path = receipt_path(self.cfg['compression_registry'], 'x.txt', self.digest)

    def confirm(self):
        self.row.update(phase='upload_intent', cloud_file_id='cloud-1', expected_cloud_version='v1')
        save(self.path, self.row)
        confirm_download(self.cfg['compression_registry'], 'x.txt', self.digest,
                         self.candidate, file_id='cloud-1', version='v2')

    def test_prepared_candidate_has_timestamp_backup_and_no_source_changes(self):
        self.assertEqual(self.original.read_text(), 'V1')
        self.assertIn('x_', self.row['backup_relative'])
        self.assertEqual(stage(self.cfg, 'x.txt', self.candidate, self.validation), self.row)

    def test_cloud_identity_and_download_hash_are_required(self):
        self.assertRaises(ValueError, confirm_download, self.cfg['compression_registry'],
                          'x.txt', self.digest, self.candidate, file_id='other', version='v2')
        self.row.update(phase='upload_intent', cloud_file_id='cloud-1')
        save(self.path, self.row)
        self.assertRaises(ValueError, confirm_download, self.cfg['compression_registry'],
                          'x.txt', self.digest, self.original, file_id='cloud-1', version='v2')

    def test_compression_event_reuses_markdown_and_remote_id(self):
        self.confirm()
        worker = test_worker.Sync(self.cfg, api=self.env.api, renderer=self.env.renderer,
                                  clock=lambda: self.env.now)
        self.env.worker = worker
        self.original.write_bytes(self.candidate.read_bytes())
        self.env.cycle(); self.env.cycle()
        record = next(iter(worker.state['records'].values()))
        self.assertEqual(record['source_hash'], self.digest)
        self.assertEqual(record['remote_ids'], ['1'])
        self.assertEqual(self.env.parse_calls, 1)
        self.assertEqual(self.env.api.upload_calls, 1)
        self.assertEqual((self.env.md / 'x.txt.md').read_text(), 'V1')
        self.assertEqual(self.env.api.deleted, [])
        # A subsequent employee edit must take the normal new-version path.
        self.original.write_text('V3 employee edit')
        self.env.cycle(); self.env.cycle()
        self.assertEqual(self.env.parse_calls, 2)
        self.assertEqual(self.env.api.upload_calls, 2)
        self.env.api.complete('2'); self.env.cycle()
        self.assertEqual((self.env.md / 'x.txt.md').read_text(), 'V3 employee edit')

    def test_unconfirmed_compression_holds_without_model_or_upload(self):
        worker = test_worker.Sync(self.cfg, api=self.env.api, renderer=self.env.renderer,
                                  clock=lambda: self.env.now)
        self.env.worker = worker
        self.original.write_bytes(self.candidate.read_bytes())
        self.env.cycle(); self.env.cycle()
        self.assertEqual(self.env.parse_calls, 1)
        self.assertEqual(self.env.api.upload_calls, 1)
        self.assertIn('compression_cloud_verification_pending',
                      next(iter(worker.state['records'].values()))['error'])
        self.assertEqual(next(iter(worker.state['records'].values()))['state'], 'compression_cloud_hold')
        # A business edit is a different version and must not inherit the cloud hold.
        self.original.write_text('employee edited after pending compression')
        self.env.cycle(); self.env.cycle()
        self.assertEqual(self.env.parse_calls, 2)

    def test_new_compressed_source_parses_backup_with_original_filename(self):
        self.confirm()
        self.original.write_bytes(self.candidate.read_bytes())
        seen = []
        def renderer(cfg, source, relative, digest, work):
            seen.append((source.name, source.read_text(), digest))
            work.mkdir(exist_ok=True, parents=True)
            output = work / 'publication.md'; output.write_text('original parsed body')
            return output, {'source_hash': digest, 'human_review': False}
        with patch('transform_local.render_document', side_effect=renderer):
            _, provenance = render(self.cfg, self.original, 'x.txt', self.digest, self.env.home / 'job')
        self.assertEqual(seen[0][:2], ('x.txt', 'V1'))
        self.assertEqual(provenance['source_hash'], self.digest)
        self.assertEqual(provenance['parsing_original_sha256'], self.validation['original_sha256_before'])

    def test_corrupt_backup_blocks_original_reuse(self):
        self.confirm()
        backup = Path(self.cfg['compression_registry']) / self.row['backup_relative']
        backup.write_text('tampered')
        self.assertRaises(ValueError, parsing_source, self.cfg, self.original, 'x.txt', self.digest)

    def test_staging_again_does_not_hide_corrupt_backup(self):
        backup = Path(self.cfg['compression_registry']) / self.row['backup_relative']
        backup.write_text('tampered')
        self.assertRaises(ValueError, stage, self.cfg, 'x.txt', self.candidate, self.validation)

    def test_successful_conditional_upload_confirms_real_download(self):
        self.row.update(cloud_file_id='cloud-1', expected_cloud_version='v1');save(self.path, self.row)
        class Transport:
            def inspect(inner, identity):
                return {'version': 'v1', 'sha256': self.row['original_sha256']}
            def replace(inner, identity, candidate, *, expected_version):
                self.assertEqual(expected_version, 'v1')
                self.assertEqual(hashes(candidate)['sha256'], self.digest)
            def download(inner, identity):
                return self.candidate, 'v2'
        result = publish(self.cfg, 'x.txt', self.digest, Transport())
        self.assertEqual(result['phase'], 'cloud_verified')
        self.assertEqual(self.original.read_text(), 'V1')

    def test_disabled_configuration_keeps_existing_update_behavior(self):
        self.confirm()
        self.original.write_bytes(self.candidate.read_bytes())
        self.env.cycle(); self.env.cycle()
        self.assertEqual(self.env.parse_calls, 2)
        self.assertEqual(self.env.api.upload_calls, 2)

    def test_scoped_local_write_reuses_active_without_claiming_cloud_confirmation(self):
        self.row['phase'] = 'local_write_intent'; save(self.path, self.row)
        self.cfg['compression_local_test_sources'] = ['x.txt']
        self.env.worker = test_worker.Sync(self.cfg, api=self.env.api,
                            renderer=self.env.renderer, clock=lambda: self.env.now)
        self.original.write_bytes(self.candidate.read_bytes())
        self.env.cycle(); self.env.cycle()
        record = next(iter(self.env.worker.state['records'].values()))
        self.assertEqual(self.env.parse_calls, 1)
        self.assertEqual(record['compression']['phase'], 'local_write_intent')
        self.assertIsNone(record['compression']['cloud_file_id'])
        self.assertEqual(record['remote_ids'], ['1'])
        self.assertRaises(ValueError, parsing_source, self.cfg, self.original, 'x.txt', self.digest)

    def test_cloud_version_conflict_never_calls_replace(self):
        self.row.update(cloud_file_id='cloud-1', expected_cloud_version='v1');save(self.path, self.row)
        class Transport:
            def inspect(self, identity):
                return {'version': 'employee-v2', 'sha256': 'other'}
            def replace(self, *args, **kwargs):
                raise AssertionError('must not overwrite employee update')
        self.assertRaises(ValueError, publish, self.cfg, 'x.txt', self.digest, Transport())
        self.assertEqual(json.loads(self.path.read_text())['phase'], 'prepared')

    def test_compression_preserves_existing_parser_failure_until_business_edit(self):
        self.confirm()
        worker = test_worker.Sync(self.cfg, api=self.env.api, renderer=self.env.renderer,
                                  clock=lambda: self.env.now)
        self.env.worker = worker
        record = next(iter(worker.state['records'].values()))
        record.update(source_hash=None, remote_ids=[], state='parse_blocked_hold',
                      parse_failure={'category': 'document'},
                      blocked_source_hash=self.row['original_sha256'],
                      blocked_parser_signature=policy_signature(self.cfg))
        self.original.write_bytes(self.candidate.read_bytes())
        self.env.cycle(); self.env.cycle()
        self.assertEqual(record['state'], 'parse_blocked_hold')
        self.assertEqual(record['blocked_source_hash'], self.digest)
        self.assertEqual(self.env.parse_calls, 1)
        self.original.write_text('employee updated original content')
        self.env.cycle(); self.env.cycle()
        self.assertEqual(self.env.parse_calls, 2)

    def test_unknown_upload_result_never_retries_replace(self):
        self.row.update(cloud_file_id='cloud-1', expected_cloud_version='v1');save(self.path, self.row)
        class Transport:
            calls = 0
            def inspect(inner, identity):
                return {'version': 'v1', 'sha256': self.row['original_sha256']}
            def replace(inner, *args, **kwargs):
                inner.calls += 1
                raise ConnectionError('response lost')
        transport = Transport()
        self.assertRaises(ConnectionError, publish, self.cfg, 'x.txt', self.digest, transport)
        self.assertRaises(ValueError, publish, self.cfg, 'x.txt', self.digest, transport)
        self.assertEqual(transport.calls, 1)


if __name__ == '__main__':
    unittest.main()
