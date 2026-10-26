import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import unicodedata

from collect_replica import Collector
from receive_replica import Receiver
from replica_transport import LocalTransport, SFTPTransport
from replica_protocol import ProtocolError, batch_name, digest, locked, read, write
from migrate_replica import migrate
import migrate_replica
from reconcile import API
from worker import Sync
from test_worker import Remote


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'client-source'
        self.source.mkdir()
        self.now = 0
        self.collect_cfg = {'source_root': str(self.source), 'collector_state_dir': str(self.base / 'collector'),
                            'scope_id': 'consultation', 'generation': 'migration-1', 'stable_seconds': 0}
        self.receive_cfg = {'source_root': str(self.base / 'server-source'),
                            'replica_state_dir': str(self.base / 'replica'), 'incoming_dir': str(self.base / 'incoming'),
                            'worker_state_dir': str(self.base / 'server-worker'),
                            'scope_id': 'consultation', 'generation': 'migration-1'}
        self.collector = Collector(self.collect_cfg, clock=lambda: self.now)
        self.receiver = Receiver(self.receive_cfg)
        self.transport = LocalTransport(self.receive_cfg['incoming_dir'])
        self.baseline = self.base / 'baseline.json'
        write(self.baseline, [])
        self.api = Remote()
        self.parse_calls = 0
        self.worker_cfg = {**self.receive_cfg, 'state_dir': self.receive_cfg['worker_state_dir'],
                           'source_mode': 'replica', 'knowledge_base_id': 'kb', 'weknora_base_url': 'http://fake',
                           'folder_root': '项目to咨询', 'markdown_root': str(self.base / 'server-markdown'),
                           'baseline_mapping': str(self.baseline), 'stable_seconds': 0,
                           'automatic_deletion_enabled': True, 'delete_grace_seconds': 999999,
                           'llm_enabled': False, 'max_jobs_per_cycle': 10}
        self.worker = Sync(self.worker_cfg, api=self.api, renderer=self.render, clock=lambda: self.now)

    def render(self, cfg, source, relative, checksum, work):
        self.parse_calls += 1
        work.mkdir(parents=True, exist_ok=True)
        path = work / 'publication.md'
        path.write_text(source.read_text())
        return path, {'method': 'isolated_fixture', 'source_hash': checksum, 'human_review': False}

    def cycle(self):
        self.now += 1
        return self.worker.cycle()

    def collect(self):
        self.collector.prepare()
        return self.collector.prepare()

    def transmit(self, batch):
        self.transport.deliver(batch)
        folder = Path(self.receive_cfg['incoming_dir']) / batch.name
        ack = self.receiver.receive(folder)
        self.collector.acknowledge(ack)
        return folder

    def initial(self, name='x.txt'):
        path = self.source / name
        path.write_text('V1')
        self.transmit(self.collect())
        self.cycle(); self.cycle()
        self.api.complete('1')
        self.cycle()
        return path

    def delete_batch(self, path):
        path.unlink()
        self.now += 1
        self.assertIsNone(self.collector.prepare())
        self.now += 1
        self.assertIsNone(self.collector.prepare())
        self.now += 60
        return self.collector.prepare()

    def publish_manifest(self, manifest):
        folder = Path(self.receive_cfg['incoming_dir']) / batch_name(manifest)
        write(folder / 'manifest.json', manifest)
        write(folder / 'READY.json', {'manifest_sha256': digest(manifest)})
        return folder

    def test_bootstrap_does_not_allow_worker_before_complete_baseline(self):
        (self.source / 'x.txt').write_text('V1')
        self.assertIsNone(self.collector.prepare())
        with self.assertRaisesRegex(ProtocolError, 'baseline_not_ready'):
            self.cycle()
        self.assertEqual(self.api.upload_calls, 0)

    def test_add_update_and_delete_reuse_existing_worker(self):
        path = self.initial()
        path.write_text('V2')
        self.transmit(self.collect())
        self.cycle(); self.cycle()
        self.assertIn('1', self.api.docs)
        self.api.complete('2')
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])
        self.assertEqual((Path(self.worker_cfg['markdown_root']) / 'x.txt.md').read_text(), 'V2')
        self.transmit(self.delete_batch(path))
        self.cycle()
        self.assertEqual(self.api.deleted, ['1', '2'])
        self.assertTrue((self.worker.home / 'retired-backups/2/original.md').exists())
        self.assertTrue((self.receiver.home / 'source-history/3/x.txt').exists())

    def test_update_pending_then_delete_uses_latest_received_hash(self):
        path = self.initial()
        path.write_text('V2')
        self.transmit(self.collect())
        self.cycle(); self.cycle()
        record = next(iter(self.worker.state['records'].values()))
        self.assertNotEqual(record['source_hash'], self.receiver.load()['files']['x.txt']['sha256'])
        self.transmit(self.delete_batch(path))
        self.cycle()
        self.assertEqual(set(self.api.deleted), {'1', '2'})
        self.assertEqual(record['state'], 'deleted')

    def test_missing_server_file_without_receipt_never_deletes_knowledge(self):
        self.initial()
        (self.receiver.root / 'x.txt').unlink()
        for _ in range(5):
            self.now += 1000000
            self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.assertIn('1', self.api.docs)

    def test_receiver_checks_hash_before_exposing_new_source(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        self.transport.deliver(batch)
        folder = Path(self.receive_cfg['incoming_dir']) / batch.name
        manifest = read(folder / 'manifest.json')
        (folder / 'objects' / manifest['upserts'][0]['sha256']).write_text('bad')
        with self.assertRaisesRegex(ProtocolError, 'object_invalid'):
            self.receiver.receive(folder)
        self.assertFalse((self.receiver.root / 'x.txt').exists())
        self.assertEqual(self.receiver.load()['last_seq'], 0)

    def test_interrupted_upload_keeps_existing_knowledge_and_retries_same_batch(self):
        path = self.initial()
        path.write_text('V2')
        batch = self.collect()
        def fail(stage, name):
            raise ConnectionError('isolated_drop')
        with self.assertRaises(ConnectionError):
            LocalTransport(self.receiver.incoming, fail).deliver(batch)
        self.receiver.run_once()
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V1')
        self.collector = Collector(self.collect_cfg, clock=lambda: self.now)
        self.assertEqual(self.collector.prepare(), batch)
        self.transmit(batch)
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V2')
        self.assertEqual(self.api.deleted, [])

    def test_duplicate_batch_and_lost_ack_do_not_repeat_commit(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        self.transport.deliver(batch)
        folder = self.receiver.incoming / batch.name
        first = self.receiver.receive(folder)
        (folder / 'ACK.json').unlink()
        self.receiver = Receiver(self.receive_cfg)
        self.assertEqual(first, self.receiver.receive(folder))
        self.assertEqual(self.receiver.load()['last_seq'], 1)
        self.collector.acknowledge(first)
        self.assertEqual(self.collector.load()['seq'], 1)

    def test_rename_reuses_parser_and_knowledge_id(self):
        path = self.initial()
        (self.source / 'moved').mkdir()
        path.rename(self.source / 'moved/x.txt')
        # The upsert is stable before the deletion evidence is ready. Keep both
        # changes in one batch by completing the grace period on observation 2.
        self.collector.prepare()
        self.now += 61
        self.collector.prepare()
        batch = self.collector.prepare()
        self.transmit(batch)
        self.cycle(); self.cycle()
        self.assertEqual(self.parse_calls, 1)
        self.assertEqual(self.api.upload_calls, 1)
        self.assertEqual(self.api.docs['1']['folder_path'], '项目to咨询/moved')
        self.assertEqual(self.api.deleted, [])

    def test_commit_crash_blocks_worker_until_recovery(self):
        (self.source / 'a.txt').write_text('A')
        (self.source / 'b.txt').write_text('B')
        batch = self.collect()
        self.transport.deliver(batch)
        def fail(stage, name):
            if stage == 'after_upsert':
                raise RuntimeError('isolated_receiver_crash')
        self.receiver.fault = fail
        with self.assertRaises(RuntimeError):
            self.receiver.receive(self.receiver.incoming / batch.name)
        with self.assertRaisesRegex(ProtocolError, 'commit_incomplete'):
            self.cycle()
        self.assertEqual(self.api.upload_calls, 0)
        self.receiver = Receiver(self.receive_cfg)
        ack = self.receiver.recover()
        self.collector.acknowledge(ack)
        self.assertEqual((self.receiver.root / 'a.txt').read_text(), 'A')
        self.assertEqual((self.receiver.root / 'b.txt').read_text(), 'B')
        self.assertFalse(self.receiver.journal.exists())
        self.cycle(); self.cycle()
        self.assertEqual(self.api.upload_calls, 2)

    def test_wrong_sequence_generation_or_delete_hash_does_not_commit(self):
        self.initial()
        template = {'version': 1, 'scope_id': 'consultation', 'generation': 'migration-1',
                    'seq': 2, 'kind': 'delta', 'upserts': [], 'deletes': []}
        for change, reason in [({'seq': 3}, 'sequence_gap'), ({'generation': 'other'}, 'binding_mismatch'),
                ({'deletes': [{'path': 'x.txt', 'expected_hash': '0'*64,
                              'missing_scans': 3, 'missing_seconds': 60}]}, 'expected_hash_mismatch')]:
            manifest = {**template, **change}
            with self.assertRaisesRegex(ProtocolError, reason):
                self.receiver.receive(self.publish_manifest(manifest))
        self.assertEqual(self.receiver.load()['last_seq'], 1)
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V1')

    def test_bulk_client_disappearance_protects_server_and_knowledge(self):
        self.collect_cfg['mass_missing_floor'] = 1
        for name in ('a.txt', 'b.txt', 'c.txt'):
            (self.source / name).write_text('V1')
        self.transmit(self.collect())
        self.cycle(); self.cycle(); self.api.complete('1'); self.cycle()
        for path in self.source.iterdir():
            path.unlink()
        with self.assertRaisesRegex(ProtocolError, 'mass_collector_missing'):
            self.collector.prepare()
        self.assertEqual(len(self.receiver.load()['files']), 3)
        self.assertEqual(self.api.deleted, [])

    def test_placeholder_does_not_generate_delete(self):
        path = self.initial()
        path.unlink()
        (self.source / '.x.txt.icloud').write_text('placeholder')
        for _ in range(5):
            self.now += 100
            self.assertIsNone(self.collector.prepare())
        self.assertEqual(self.collector.load()['missing'], {})

    def test_path_traversal_and_symlink_cannot_escape_source_scope(self):
        self.initial()
        manifest = {'version': 1, 'scope_id': 'consultation', 'generation': 'migration-1',
                    'seq': 2, 'kind': 'delta', 'upserts': [],
                    'deletes': [{'path': '../outside', 'expected_hash': '0'*64,
                                 'missing_scans': 3, 'missing_seconds': 60}]}
        with self.assertRaisesRegex(ProtocolError, 'unsafe_relative'):
            self.receiver.receive(self.publish_manifest(manifest))
        self.assertFalse((self.base / 'outside').exists())

    def test_normalization_collision_pauses_collection(self):
        first = self.source / 'é.txt'
        first.write_text('V1')
        second = self.source / unicodedata.normalize('NFD', 'é.txt')
        if not second.exists():
            second.write_text('V1')
        with patch.object(self.collector, 'paths', return_value=iter([first, second])):
            with self.assertRaisesRegex(ProtocolError, 'normalized_source_name_collision'):
                self.collector.prepare()

    def test_incomplete_source_scan_never_generates_deletion(self):
        self.initial()
        with patch.object(self.collector, 'paths', side_effect=PermissionError('isolated unreadable directory')):
            with self.assertRaises(PermissionError):
                self.collector.prepare()
        self.assertIsNone(self.collector.load()['outbox'])
        self.assertEqual(self.collector.load()['missing'], {})

    def test_receiver_and_worker_share_one_commit_lock(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        self.transport.deliver(batch)
        with locked(self.worker.home / 'worker.lock'):
            with self.assertRaises(BlockingIOError):
                self.receiver.receive(self.receiver.incoming / batch.name)
        self.assertEqual(self.receiver.load()['last_seq'], 0)

    def test_shared_knowledge_survives_one_source_deletion(self):
        for name in ('a.txt', 'b.txt'):
            (self.source / name).write_text('shared')
        self.transmit(self.collect())
        self.cycle(); self.cycle(); self.api.complete('1'); self.cycle()
        self.transmit(self.delete_batch(self.source / 'a.txt'))
        self.cycle()
        self.assertEqual(self.api.deleted, [])
        self.assertTrue((Path(self.worker_cfg['markdown_root']) / 'b.txt.md').exists())

    def test_receiver_modified_file_cannot_be_published(self):
        self.initial()
        (self.receiver.root / 'x.txt').write_text('uncommitted V2')
        self.cycle(); self.cycle()
        self.assertEqual(self.api.upload_calls, 1)
        self.assertEqual(self.api.deleted, [])

    def test_migration_preserves_source_and_remote_ids_and_reuses_markdown(self):
        self.initial()
        old_cfg = copy.deepcopy(self.worker_cfg)
        old_cfg['excluded_sources'] = {'excluded.txt': 'preserved user exclusion'}
        target_source = self.base / 'migrated-source'
        target_source.mkdir()
        receiver_cfg = {**self.receive_cfg, 'source_root': str(target_source),
                        'replica_state_dir': str(self.base / 'migrated-replica'),
                        'worker_state_dir': str(self.base / 'migrated-worker')}
        target_receiver = Receiver(receiver_cfg)
        baseline = copy.deepcopy(read(next(self.receiver.incoming.glob('*/manifest.json'))))
        folder = self.receiver.incoming / batch_name(baseline)
        target_receiver.receive(folder)
        target_cfg = {**self.worker_cfg, **receiver_cfg, 'state_dir': receiver_cfg['worker_state_dir'],
                      'markdown_root': str(self.base / 'migrated-markdown'), 'weknora_base_url': 'http://new-server'}
        old_ids = set(self.worker.state['records'])
        with self.assertRaisesRegex(ProtocolError, 'stop_source_worker'):
            migrate(old_cfg, target_cfg, self.api.inventory())
        result = migrate(old_cfg, target_cfg, self.api.inventory(), source_stopped=True)
        migrated = Sync(target_cfg, api=self.api, renderer=self.render, clock=lambda: self.now)
        self.assertEqual(set(migrated.state['records']), old_ids)
        self.assertTrue(result['knowledge_ids_preserved'])
        self.assertEqual(read(result['effective_target_config'])['excluded_sources'], old_cfg['excluded_sources'])
        before = self.parse_calls
        self.now += 1; migrated.cycle()
        self.now += 1; migrated.cycle()
        self.assertEqual(self.parse_calls, before)
        self.assertEqual(self.api.upload_calls, 1)
        self.assertFalse((migrated.home / 'migration.incomplete.json').exists())

    def test_restore_invalidates_old_tombstone_and_old_batch_cannot_delete_new_file(self):
        path = self.initial()
        old_batch = self.transmit(self.delete_batch(path))
        self.cycle()
        path.write_text('restored V2')
        self.transmit(self.collect())
        self.receiver.receive(old_batch)
        self.assertNotIn('x.txt', self.receiver.load()['deletions'])
        self.cycle(); self.api.complete('2'); self.cycle()
        self.assertIn('2', self.api.docs)
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'restored V2')

    def test_new_modification_during_unacknowledged_transfer_is_not_lost(self):
        path = self.initial()
        path.write_text('V2')
        batch = self.collect()
        path.write_text('V3')
        self.transmit(batch)
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V2')
        self.transmit(self.collect())
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V3')

    def test_same_sequence_with_different_manifest_is_rejected(self):
        self.initial()
        manifest = read(next(self.receiver.incoming.glob('*/manifest.json')))
        manifest['upserts'] = []
        with self.assertRaisesRegex(ProtocolError, 'same_sequence_different_manifest'):
            self.receiver.receive(self.publish_manifest(manifest))
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V1')

    def test_crash_after_delete_and_after_state_commit_recovers_without_repeating_delete(self):
        path = self.initial()
        batch = self.delete_batch(path)
        self.transport.deliver(batch)
        folder = self.receiver.incoming / batch.name
        for stage in ('after_delete', 'after_state_commit'):
            def fail(current, name):
                if current == stage:
                    raise RuntimeError('isolated crash')
            self.receiver.fault = fail
            with self.assertRaises(RuntimeError):
                if self.receiver.journal.exists():
                    self.receiver.recover()
                else:
                    self.receiver.receive(folder)
            with self.assertRaisesRegex(ProtocolError, 'commit_incomplete'):
                self.cycle()
        self.receiver.fault = lambda *args: None
        self.collector.acknowledge(self.receiver.recover())
        self.cycle()
        self.assertEqual(self.api.deleted, ['1'])
        self.assertTrue((self.receiver.home / 'source-history/2/x.txt').exists())

    def test_replica_deletion_disabled_keeps_knowledge_after_valid_receipt(self):
        path = self.initial()
        self.transmit(self.delete_batch(path))
        self.worker.cfg['automatic_deletion_enabled'] = False
        self.cycle()
        self.assertEqual(self.api.deleted, [])

    def fake_sftp(self):
        executable = self.base / 'fake-sftp'
        executable.write_text('#!' + sys.executable + '\n' + '''
import os, pathlib, shlex, shutil, sys
with open(os.environ['FAKE_SFTP_LOG'], 'a') as log:
    log.write(' '.join(sys.argv[1:])+'\\n')
for line in sys.stdin:
    optional = line.startswith('-')
    args = shlex.split(line[1:] if optional else line)
    try:
        op=args[0]
        if op=='mkdir': pathlib.Path(args[1]).mkdir()
        elif op=='rm': pathlib.Path(args[1]).unlink()
        elif op in ('get','put'):
            if args[1]=='-f': args.pop(1)
            shutil.copyfile(args[1],args[2])
        elif op=='rename':
            if args[1]=='-f': args.pop(1)
            trigger=os.environ.get('FAKE_SFTP_DROP')
            if trigger and args[2].endswith('/READY.json') and not pathlib.Path(trigger).exists():
                pathlib.Path(trigger).touch(); sys.exit(73)
            os.replace(args[1],args[2])
        else: raise RuntimeError('unknown command')
    except OSError:
        if not optional: sys.exit(1)
''')
        executable.chmod(0o700)
        key = self.base / 'isolated-key'
        key.write_text('isolated credential fixture')
        key.chmod(0o600)
        known = self.base / 'known_hosts'
        known.write_text('isolated host fixture')
        return SFTPTransport({'host': 'isolated.invalid', 'user': 'sync', 'identity_file': str(key),
                             'known_hosts_file': str(known), 'remote_incoming': str(self.receiver.incoming),
                             'sftp_executable': str(executable)})

    def test_sftp_adapter_uses_pinned_host_and_same_commit_protocol(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        log = self.base / 'sftp.log'
        with patch.dict(os.environ, {'FAKE_SFTP_LOG': str(log)}):
            transport = self.fake_sftp()
            self.assertIsNone(transport.deliver(batch))
            self.receiver.receive(self.receiver.incoming / batch.name)
            self.collector.acknowledge(transport.deliver(batch))
        self.assertIn('StrictHostKeyChecking=yes', log.read_text())
        self.assertIn('BatchMode=yes', log.read_text())
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V1')

    def test_sftp_adapter_retries_interruption_before_ready(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        with patch.dict(os.environ, {'FAKE_SFTP_LOG': str(self.base / 'sftp.log'),
                                     'FAKE_SFTP_DROP': str(self.base / 'drop-once')}):
            transport = self.fake_sftp()
            with self.assertRaisesRegex(ProtocolError, 'transfer_failed'):
                transport.deliver(batch)
            self.receiver.run_once()
            self.assertEqual(self.receiver.load()['last_seq'], 0)
            self.assertFalse((self.receiver.root / 'x.txt').exists())
            transport.deliver(batch)
            self.receiver.receive(self.receiver.incoming / batch.name)
            self.collector.acknowledge(transport.deliver(batch))
        self.assertEqual(self.collector.load()['seq'], 1)

    @unittest.skipUnless(Path('/usr/bin/sftp').is_file() and Path('/usr/libexec/sftp-server').is_file(),
                         'real local OpenSSH SFTP binaries unavailable')
    def test_real_openssh_sftp_protocol_and_interrupted_batch_retry(self):
        (self.source / 'x.txt').write_text('V1')
        batch = self.collect()
        transport = self.fake_sftp()
        transport.command = ['/usr/bin/sftp', '-q', '-b', '-', '-D', '/usr/libexec/sftp-server']
        remote = self.receiver.incoming / batch.name
        manifest = read(batch / 'manifest.json')
        checksum = manifest['upserts'][0]['sha256']
        # A real SFTP process transfers only part of the batch, then exits.
        transport.run(['mkdir ' + transport.quote(remote), 'mkdir ' + transport.quote(remote / 'objects'),
                       'put -f ' + transport.quote(batch / 'objects' / checksum) + ' ' + transport.quote(remote / 'objects' / checksum)])
        self.receiver.run_once()
        self.assertEqual(self.receiver.load()['last_seq'], 0)
        self.assertIsNone(transport.deliver(batch))
        self.receiver.receive(remote)
        self.collector.acknowledge(transport.deliver(batch))
        self.assertEqual((self.receiver.root / 'x.txt').read_text(), 'V1')
        self.assertEqual(self.collector.load()['seq'], 1)

    def test_migration_recovery_after_state_commit_preserves_ids(self):
        self.initial()
        receiver_cfg = {**self.receive_cfg, 'source_root': str(self.base / 'migrate-source'),
                        'replica_state_dir': str(self.base / 'migrate-replica'),
                        'worker_state_dir': str(self.base / 'migrate-worker')}
        receiver = Receiver(receiver_cfg)
        receiver.receive(next(self.receiver.incoming.glob('*')))
        target_cfg = {**self.worker_cfg, **receiver_cfg, 'state_dir': receiver_cfg['worker_state_dir'],
                      'markdown_root': str(self.base / 'migrate-md'), 'weknora_base_url': 'http://new'}
        original_write = migrate_replica.write
        def crashing(path, value):
            original_write(path, value)
            if Path(path) == Path(target_cfg['state_dir']) / 'state.json':
                raise OSError('isolated crash after state commit')
        with patch.object(migrate_replica, 'write', side_effect=crashing):
            with self.assertRaises(OSError):
                migrate(self.worker_cfg, target_cfg, self.api.inventory(), source_stopped=True)
        marker = Path(target_cfg['state_dir']) / 'migration.incomplete.json'
        self.assertTrue(marker.exists())
        target = Sync(target_cfg, api=self.api, renderer=self.render, clock=lambda: self.now)
        with self.assertRaisesRegex(ProtocolError, 'migration_incomplete'):
            target.cycle()
        report = migrate(self.worker_cfg, target_cfg, self.api.inventory(), source_stopped=True)
        self.assertTrue(report['source_ids_preserved'])
        self.assertFalse(marker.exists())

    def test_migration_rejects_pending_upload_and_wrong_remote_identity(self):
        self.initial()
        receiver_cfg = {**self.receive_cfg, 'source_root': str(self.base / 'migration-source'),
                        'replica_state_dir': str(self.base / 'migration-replica'),
                        'worker_state_dir': str(self.base / 'migration-worker')}
        receiver = Receiver(receiver_cfg)
        receiver.receive(next(self.receiver.incoming.glob('*')))
        target_cfg = {**self.worker_cfg, **receiver_cfg, 'state_dir': receiver_cfg['worker_state_dir'],
                      'markdown_root': str(self.base / 'migration-md')}
        inventory = self.api.inventory()
        with self.assertRaisesRegex(ProtocolError, 'remote_knowledge'):
            migrate(self.worker_cfg, target_cfg, [], source_stopped=True)
        disabled = copy.deepcopy(inventory)
        disabled[0]['enable_status'] = 'disabled'
        with self.assertRaisesRegex(ProtocolError, 'remote_knowledge_not_ready'):
            migrate(self.worker_cfg, target_cfg, disabled, source_stopped=True)
        state_file = self.worker.home / 'state.json'
        state = read(state_file)
        next(iter(state['records'].values()))['pending'] = {'phase': 'uncertain'}
        write(state_file, state)
        with self.assertRaisesRegex(ProtocolError, 'resolve_pending_uploads'):
            migrate(self.worker_cfg, target_cfg, inventory, source_stopped=True)
        self.assertFalse((Path(target_cfg['state_dir']) / 'state.json').exists())

    def test_api_inventory_uses_configured_knowledge_base(self):
        key = self.base / 'key'
        key.write_text('isolated-key')
        api = API(key, 'http://isolated.invalid', kb_id='preserved-kb')
        routes = []
        def request(route):
            routes.append(route)
            return 200, {'success': True, 'total': 0, 'data': []}
        api.request = request
        self.assertEqual(api.inventory(), [])
        self.assertIn('/knowledge-bases/preserved-kb/', routes[0])

    def test_receiver_lock_binding_mismatch_blocks_worker(self):
        self.initial()
        self.worker.cfg['state_dir'] = str(self.base / 'wrong-worker')
        with self.assertRaisesRegex(ProtocolError, 'lock_directory_mismatch'):
            self.worker.cycle()

    def test_malformed_ready_package_fails_before_commit(self):
        self.initial()
        manifest = {'version': 1, 'scope_id': 'consultation', 'generation': 'migration-1',
                    'seq': 2, 'kind': 'delta', 'upserts': [], 'deletes': []}
        folder = self.publish_manifest(manifest)
        (folder / 'manifest.json').write_text('{broken')
        with self.assertRaisesRegex(ProtocolError, 'invalid_protocol_json'):
            self.receiver.receive(folder)
        self.assertEqual(self.receiver.load()['last_seq'], 1)
        self.assertIn('1', self.api.docs)


if __name__ == '__main__':
    unittest.main()
