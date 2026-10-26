import hashlib
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import compression_deploy as deploy
from compression_batch import scope_paths


class CompressionDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.registry = self.project / 'registry'
        self.registry.mkdir(parents=True)
        (self.registry / 'backup.pdf').write_bytes(b'original')
        (self.registry / 'candidate.pdf').write_bytes(b'compressed')
        self.source = self.root / 'new-source'
        (self.source / '测试').mkdir(parents=True)
        (self.source / '测试/a.pdf').write_bytes(b'compressed')
        self.row = {'source_root': '/old/source', 'relative': '测试/a.pdf', 'phase': 'client_verified',
            'backup_relative': 'backup.pdf', 'candidate_relative': 'candidate.pdf',
            'original_sha256': deploy.digest(self.registry / 'backup.pdf'),
            'output_sha256': deploy.digest(self.registry / 'candidate.pdf'), 'cloud_file_id': 'file1',
            'client_sync_evidence': {'version_record_advanced': True}, 'source_signature': [1, 2, 3, 4]}
        deploy.write(self.registry / 'receipts/a.json', self.row)
        deploy.write(self.registry / 'alerts/state.json', {'sent': {'already-sent': '2026-10-16'}})
        self.cfg = {'source_root': '/old/source', 'scope_relative': '测试', 'compression_registry': str(self.registry),
            'room_name': '项目to咨询', 'client_corp_id': 'corp', 'client_space_id': 'space'}
        config = self.project / 'outputs/测试目录定时压缩_20261013'
        deploy.write(config / 'config.json', self.cfg)
        deploy.write(config / 'alerts-config.json', {'daily_hour': 3, 'daily_minute': 0})
        self.home = self.root / 'target'
        deploy.snapshot(self.project, self.cfg, self.home / 'private/compression')

    def bind(self):
        mapping = deploy.restore_archives(self.home, self.source)
        deploy.write(self.home / 'config.json', {'source_root': str(self.source)})
        db = self.root / 'client.sqlite'
        with sqlite3.connect(db) as connection:
            connection.executescript('CREATE TABLE Corp(corpid); CREATE TABLE Room(name,space_id); CREATE TABLE File(roomName,relativePath,space_id,file_id,md5,size TEXT);')
            connection.execute('INSERT INTO Corp VALUES(?)', ('corp',))
            connection.execute('INSERT INTO Room VALUES(?,?)', ('项目to咨询', 'space'))
            connection.execute('INSERT INTO File VALUES(?,?,?,?,?,?)', ('项目to咨询', '测试/a.pdf', 'space', 'file1', hashlib.md5(b'compressed').hexdigest(), 10))
        return mapping, db

    def test_byte_verified_restore_preserves_original_and_alert_dedupe(self):
        mapping, db = self.bind()
        archive = Path(mapping[str(self.registry)])
        self.assertEqual((archive / 'backup.pdf').read_bytes(), b'original')
        row = deploy.read(archive / 'receipts/a.json')
        self.assertEqual(row['source_root'], str(self.source))
        self.assertEqual(row['migration_origin_source_root'], '/old/source')
        self.assertNotIn('source_signature', row)
        self.assertEqual(row['client_sync_evidence'], self.row['client_sync_evidence'])
        self.assertEqual(deploy.read(archive / 'alerts/state.json')['sent'], {'already-sent': '2026-10-16'})
        deploy.bind_client(self.home, db)
        cfg = deploy.read(self.home / 'compression/config.json')
        self.assertEqual(cfg['client_database'], str(db))
        self.assertEqual(cfg['authorized_source_root'], str(self.source))
        self.assertEqual(scope_paths(cfg)[0], self.source)

    def test_corrupt_archive_rejected_before_runtime_copy(self):
        (self.home / 'private/compression/registries/0/backup.pdf').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'checksum_mismatch'):
            deploy.restore_archives(self.home, self.source)
        self.assertFalse((self.home / 'compression').exists())

    def test_wrong_account_or_file_identity_does_not_bind(self):
        _, db = self.bind()
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE File SET file_id='different'")
        with self.assertRaisesRegex(ValueError, 'file_identity_mismatch'):
            deploy.bind_client(self.home, db)
        self.assertFalse(deploy.read(self.home / 'compression/migration.json')['client_binding_verified'])
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE Corp SET corpid='other'")
        with self.assertRaisesRegex(ValueError, 'space_identity_mismatch'):
            deploy.bind_client(self.home, db)

    def test_pending_scheduled_write_blocks_binding(self):
        mapping, db = self.bind()
        deploy.write(Path(mapping[str(self.registry)]) / 'receipts/a.json', {**self.row, 'phase': 'local_replaced'})
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            deploy.bind_client(self.home, db)

    def test_predeployment_never_installs_writing_tasks(self):
        _, db = self.bind()
        deploy.bind_client(self.home, db)
        deploy.write(self.home / 'private/snapshot.json', {'old_services_resumed': True})
        calls = []
        with self.assertRaisesRegex(ValueError, '预部署包'):
            deploy.install(self.home, lambda args: calls.append(args))
        self.assertEqual(calls, [])

    def test_native_compiler_and_all_schedules_use_new_home(self):
        _, db = self.bind()
        deploy.bind_client(self.home, db)
        deploy.write(self.home / 'private/snapshot.json', {'old_services_resumed': False})
        deploy.write(self.home / 'activation.json', {'old_stopped': True})
        calls = []
        with patch.object(Path, 'home', return_value=self.root), patch.object(deploy.subprocess, 'run') as status:
            status.return_value.returncode = 1
            deploy.install(self.home, lambda args: calls.append(args))
        self.assertIn('swiftc', calls[1])
        self.assertIn(str(self.home / 'app/implementation/weknora-sync/CompressionAlert.swift'), calls[1])
        jobs = [plistlib.loads((self.root / 'Library/LaunchAgents' / (label + '.plist')).read_bytes()) for label in deploy.LABELS]
        self.assertEqual(jobs[0]['StartCalendarInterval'], {'Hour': 3, 'Minute': 0})
        self.assertEqual(jobs[1]['StartInterval'], 120)
        self.assertEqual(jobs[2]['StartInterval'], 300)
        self.assertTrue(all(j['ProgramArguments'][0] == str(self.home / '.venv/bin/python3') for j in jobs))
        self.assertNotIn('RunAtLoad', jobs[0])


if __name__ == '__main__':
    unittest.main()
