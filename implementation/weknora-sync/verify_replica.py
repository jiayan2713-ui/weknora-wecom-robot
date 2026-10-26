"""Persistent synthetic acceptance: no production files, APIs or model calls."""
import argparse
from datetime import datetime
from pathlib import Path
import uuid

from collect_replica import Collector
from receive_replica import Receiver
from replica_transport import LocalTransport
from replica_protocol import ProtocolError, copy_verified, read, write
from migrate_replica import migrate
from worker import Sync
from test_worker import Remote


def verify(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('acceptance_output_must_be_new')
    output.mkdir(parents=True)
    now = [0]
    def clock():
        return now[0]
    def cycle(worker):
        now[0] += 1
        return worker.cycle()
    source = output / 'client-source'
    source.mkdir()
    source_file = source / 'fixture.txt'
    source_file.write_text('隔离验收版本一：费用100。')
    baseline = output / 'baseline.json'
    write(baseline, [])
    common = {'knowledge_base_id': 'kb', 'weknora_base_url': 'http://isolated.invalid',
              'folder_root': '项目to咨询', 'baseline_mapping': str(baseline), 'stable_seconds': 0,
              'max_jobs_per_cycle': 10, 'llm_enabled': False, 'allow_model_calls': False,
              'reuse_robot1_artifacts': False, 'automatic_deletion_enabled': True,
              'delete_grace_seconds': 60}
    old_cfg = {**common, 'source_root': str(source), 'state_dir': str(output / 'old-worker'),
               'markdown_root': str(output / 'old-markdown')}
    api = Remote()
    old_worker = Sync(old_cfg, api=api, clock=clock)
    cycle(old_worker); cycle(old_worker); api.complete('1'); cycle(old_worker)
    generation = 'acceptance-' + uuid.uuid4().hex
    collector_cfg = {'source_root': str(source), 'collector_state_dir': str(output / 'collector'),
                     'scope_id': 'isolated-consultation', 'generation': generation, 'stable_seconds': 0}
    receiver_cfg = {'source_root': str(output / 'server-source'), 'incoming_dir': str(output / 'incoming'),
                    'replica_state_dir': str(output / 'replica'), 'worker_state_dir': str(output / 'new-worker'),
                    'scope_id': collector_cfg['scope_id'], 'generation': generation}
    target_cfg = {**common, **receiver_cfg, 'source_mode': 'replica',
                  'state_dir': receiver_cfg['worker_state_dir'], 'markdown_root': str(output / 'new-markdown')}
    for name, cfg in [('collector', collector_cfg), ('receiver', receiver_cfg), ('target-worker', target_cfg)]:
        write(output / (name + '-config.json'), cfg)
    collector = Collector(collector_cfg, clock=clock)
    receiver = Receiver(receiver_cfg)
    transport = LocalTransport(receiver.incoming)
    collector.prepare()
    batch = collector.prepare()
    def dropped(stage, name):
        raise ConnectionError('isolated interrupted transfer')
    try:
        LocalTransport(receiver.incoming, dropped).deliver(batch)
        raise AssertionError('fault did not run')
    except ConnectionError:
        pass
    receiver.run_once()
    assert receiver.load()['last_seq'] == 0 and not list(receiver.root.iterdir())
    transport.deliver(batch)
    first_folder = receiver.incoming / batch.name
    ack = receiver.receive(first_folder)
    assert receiver.receive(first_folder) == ack
    collector.acknowledge(ack)
    migration = migrate(old_cfg, target_cfg, api.inventory(), source_stopped=True)
    worker = Sync(target_cfg, api=api, clock=clock)
    cycle(worker)
    assert api.upload_calls == 1 and migration['knowledge_ids_preserved']
    cases = {'bootstrap_and_interrupted_transfer': True, 'duplicate_batch': True,
             'migration_preserves_source_and_knowledge_ids': True,
             'migration_does_not_reparse_or_upload_existing_document': True}

    def commit(batch):
        transport.deliver(batch)
        folder = receiver.incoming / batch.name
        collector.acknowledge(receiver.receive(folder))
        return folder
    def updated_batch():
        collector.prepare()
        return collector.prepare()

    source_file.write_text('隔离验收版本二：费用200。')
    commit(updated_batch())
    cycle(worker)
    assert '1' in api.docs and '2' in api.docs
    api.complete('2'); cycle(worker)
    assert '1' not in api.docs and '2' in api.docs
    cases['new_version_waits_for_index_before_retiring_old'] = True
    (source / '移动').mkdir()
    moved = source / '移动/fixture.txt'
    source_file.rename(moved)
    collector.prepare(); now[0] += 61; collector.prepare()
    commit(collector.prepare())
    cycle(worker)
    assert api.upload_calls == 2 and api.docs['2']['folder_path'] == '项目to咨询/移动'
    cases['same_content_rename_reuses_knowledge_id'] = True
    replica_path = receiver.root / '移动/fixture.txt'
    replica_path.unlink()
    cycle(worker)
    assert '2' in api.docs
    entry = receiver.load()['files']['移动/fixture.txt']
    copy_verified(receiver.incoming / entry['object_batch'] / 'objects' / entry['sha256'],
                  replica_path, entry['sha256'], entry['size'])
    cycle(worker)
    cases['server_missing_without_receipt_preserves_knowledge'] = True
    moved.unlink()
    collector.prepare(); now[0] += 61; collector.prepare()
    batch = collector.prepare()
    transport.deliver(batch)
    delete_folder = receiver.incoming / batch.name
    def crashed(stage, name):
        if stage == 'after_delete':
            raise RuntimeError('isolated receiver crash')
    receiver.fault = crashed
    try:
        receiver.receive(delete_folder)
        raise AssertionError('fault did not run')
    except RuntimeError:
        pass
    try:
        cycle(worker)
        raise AssertionError('worker saw an incomplete commit')
    except ProtocolError:
        pass
    assert '2' in api.docs
    receiver = Receiver(receiver_cfg)
    collector.acknowledge(receiver.recover())
    cycle(worker)
    assert not api.docs and (worker.home / 'retired-backups/2/original.md').exists()
    cases['receiver_crash_blocks_worker_until_recovered'] = True
    cases['collector_receipt_propagates_delete_with_backup'] = True
    moved.write_text('隔离验收恢复版本：费用300。')
    commit(updated_batch())
    receiver.receive(delete_folder)
    cycle(worker); api.complete('3'); cycle(worker)
    assert '3' in api.docs and '移动/fixture.txt' not in receiver.load()['deletions']
    cases['restoration_invalidates_old_delete_receipt'] = True
    assert all(cases.values())
    result = {'status': 'passed', 'checked_at': datetime.now().isoformat(),
              'scope': 'synthetic_local_only', 'external_model_calls': 0,
              'production_api_calls': 0, 'production_state_writes': 0,
              'cases': cases, 'migration': migration, 'final_replica_seq': receiver.load()['last_seq'],
              'final_active_knowledge_ids': list(api.docs),
              'limitations': ['真实服务器SSH认证、网络与Linux解析环境仍需部署联调',
                              '采集删除证据来自本地目录，不证明微盘云端删除或所有取消同步情况']}
    write(output / '验收结果.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.output)
    print('隔离验收通过：', len(result['cases']), '条贯通检查；无生产写入、无外部模型调用。')


if __name__ == '__main__':
    main()
