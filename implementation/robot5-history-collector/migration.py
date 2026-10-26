"""Locked archive snapshot and scoped restore; never copy live SQLite WAL files."""
import fcntl
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

import collector


def snapshot(root, binding_file, destination, refresh=False):
    root, destination = Path(root), Path(destination)
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('备份目标不能放在活动档案目录内部')
    if destination.exists():
        raise ValueError('历史档案目标已存在，拒绝覆盖')
    binding = json.loads(Path(binding_file).read_text())
    with (root / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source = collector.connect(root)
        excel_lock=None
        try:
            if refresh:
                collector.sync(source, root, binding, full=True)
            # A background export deliberately does not hold collector.lock.
            # Wait for it before copying a coherent database/workbook pair.
            excel_lock=(root/'excel.lock').open('a')
            fcntl.flock(excel_lock,fcntl.LOCK_EX)
            scope = json.loads(source.execute("SELECT value FROM state WHERE key='scope'").fetchone()[0])
            if any(scope[key] != binding[key] for key in ('channel_id', 'agent_id')):
                raise ValueError('采集档案与渠道绑定不符')
            destination.mkdir(parents=True, mode=0o700)
            with sqlite3.connect(destination / 'history.sqlite3') as target:
                source.backup(target)
            for path in root.rglob('*'):
                if path.is_symlink() or not path.is_file() or path.name in (
                        'collector.lock', 'excel.lock', 'history.sqlite3', 'history.sqlite3-wal', 'history.sqlite3-shm'):
                    continue
                target = destination / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(path, target)
                target.chmod(0o600)
            collector.atomic_json(destination / 'binding.json', {key: binding[key] for key in ('channel_id', 'agent_id')})
            collector.atomic_json(destination / 'snapshot.json', {
                'captured_at': collector.now(), 'scope': scope,
                'messages': source.execute('SELECT count(*) FROM messages').fetchone()[0],
                'qa_records': len(collector.qa_records(source)), 'refreshed_after_platform_freeze': refresh})
            (destination / 'history.sqlite3').chmod(0o600)
        finally:
            if excel_lock:excel_lock.close()
            source.close()


def restore(source, root, compose_file):
    source, root = Path(source), Path(root)
    if root.exists() and any(root.iterdir()):
        raise ValueError('已有历史档案，拒绝覆盖')
    # Binding comes from the authenticated payload, never from a machine-specific source path.
    binding = json.loads((source / 'binding.json').read_text())
    snapshot_info = json.loads((source / 'snapshot.json').read_text())
    with sqlite3.connect(f'file:{source / "history.sqlite3"}?mode=ro', uri=True) as archived:
        if archived.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('历史档案完整性检查失败')
        scope = json.loads(archived.execute("SELECT value FROM state WHERE key='scope'").fetchone()[0])
        if scope != snapshot_info['scope'] or any(scope[key] != binding[key] for key in ('channel_id', 'agent_id')):
            raise ValueError('历史档案归属不匹配')
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=root.name+'.restoring-', dir=root.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        staging.chmod(0o700)
        db = collector.connect(staging)
        try:
            # A restored platform may predate the archive: force a full, deduplicated reconciliation.
            with db:
                db.execute("DELETE FROM state WHERE key IN ('cursor','last_full','export_version')")
            collector.export(db, staging, backend='openpyxl')
        finally:
            db.close()
        binding.update(compose_file=str(Path(compose_file).resolve()), export_backend='openpyxl', export_async=True)
        collector.atomic_json(staging / 'binding.json', binding)
        os.replace(staging, root)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return snapshot_info


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--compose-file', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = restore(args.source, args.root, args.compose_file)
    print(json.dumps({'status': 'restored', 'qa_records': result['qa_records'], 'messages': result['messages']}))
