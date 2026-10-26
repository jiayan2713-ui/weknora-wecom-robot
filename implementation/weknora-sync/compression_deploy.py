"""Portable compression archives and opt-in target-machine scheduling."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess

LABELS = ('com.robot5.mac-compression', 'com.robot5.mac-compression-recovery',
          'com.robot5.mac-compression-alerts')


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_archive(source, target):
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('compression_archive_symlink')
        if path.is_file() and not path.name.endswith('.lock'):
            out = target / path.relative_to(source)
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, out)
            out.chmod(0o600)


def snapshot(project, worker, destination):
    project, destination = Path(project), Path(destination)
    cfg = read(project / 'outputs/测试目录定时压缩_20261013/config.json')
    alerts = read(project / 'outputs/测试目录定时压缩_20261013/alerts-config.json')
    directories = list(dict.fromkeys([worker['compression_registry'],
        *worker.get('compression_additional_registries', []), cfg['compression_registry']]))
    mapping = {}
    for index, directory in enumerate(directories):
        if not Path(directory).is_dir():
            raise ValueError('compression_registry_missing')
        relative = 'registries/' + str(index)
        copy_archive(directory, destination / relative)
        mapping[directory] = relative
    write(destination / 'source-config.json', cfg)
    write(destination / 'alerts-config.json', alerts)
    write(destination / 'snapshot.json', {'registries': mapping, 'source_root': cfg['source_root'],
        'daily_hour': alerts['daily_hour'], 'daily_minute': alerts['daily_minute']})
    checksums = {str(p.relative_to(destination)): digest(p) for p in destination.rglob('*') if p.is_file()}
    write(destination / 'checksums.json', checksums)
    return {'files': len(checksums), 'bytes': sum((destination / p).stat().st_size for p in checksums),
            'registry_count': len(mapping), 'schedule': '03:00 Asia/Shanghai'}


def verify_snapshot(source):
    source = Path(source).resolve()
    for name, checksum in read(source / 'checksums.json').items():
        path = source / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('compression_snapshot_path_escaped')
        if digest(path) != checksum:
            raise ValueError('compression_snapshot_checksum_mismatch')


def restore_archives(home, source_root):
    home, source_root = Path(home).resolve(), Path(source_root).resolve()
    source = home / 'private/compression'
    if not source.exists():
        return {}
    verify_snapshot(source)
    info = read(source / 'snapshot.json')
    destination = home / 'compression'
    if destination.exists():
        raise ValueError('compression_destination_exists')
    copy_archive(source, destination)
    mapping = {old: str(destination / relative) for old, relative in info['registries'].items()}
    for old, new in mapping.items():
        for receipt in (Path(new) / 'receipts').glob('*.json'):
            row = read(receipt)
            name = Path(row['relative'])
            if name.is_absolute() or '..' in name.parts or not name.is_relative_to('测试'):
                raise ValueError('compression_receipt_scope_invalid')
            # Existing evidence remains explicitly attributed to the old account.
            row['migration_origin_source_root'] = row['source_root']
            row['source_root'] = str(source_root)
            row.pop('source_signature', None)
            write(receipt, row)
    write(destination / 'migration.json', {'registry_paths': mapping,
        'source_root': str(source_root), 'client_binding_verified': False})
    return mapping


def bind_client(home, database):
    home, database = Path(home).resolve(), Path(database).resolve()
    migration = read(home / 'compression/migration.json')
    worker = read(home / 'config.json')
    original = read(home / 'private/compression/source-config.json')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        corps = connection.execute('SELECT corpid FROM Corp').fetchall()
        rooms = connection.execute('SELECT space_id FROM Room WHERE name=?', (original['room_name'],)).fetchall()
        if len(corps) != 1 or len(rooms) != 1 or str(corps[0][0]) != original['client_corp_id'] or rooms[0][0] != original['client_space_id']:
            raise ValueError('new_client_space_identity_mismatch')
        for directory in migration['registry_paths'].values():
            for path in (Path(directory) / 'receipts').glob('*.json'):
                row = read(path)
                if row['phase'] not in ('client_verified', 'cloud_verified', 'cancelled_test', 'superseded_prepared'):
                    if directory == migration['registry_paths'][original['compression_registry']]:
                        raise ValueError('compression_pending_receipt_requires_reconciliation')
                    # Legacy experiment stays archived; scheduled recovery never visits it.
                    continue
                if row['phase'] not in ('client_verified', 'cloud_verified'):
                    continue
                for field, hash_field in (('backup_relative', 'original_sha256'), ('candidate_relative', 'output_sha256')):
                    artifact = Path(directory) / row[field]
                    if not artifact.resolve().is_relative_to(Path(directory)) or digest(artifact) != row[hash_field]:
                        raise ValueError('compression_artifact_integrity_failed')
                source = Path(worker['source_root']) / row['relative']
                if source.exists() and digest(source) == row['output_sha256']:
                    matches = connection.execute('SELECT file_id,md5,size FROM File WHERE roomName=? AND relativePath=? AND space_id=?',
                        (original['room_name'], row['relative'], original['client_space_id'])).fetchall()
                    with source.open('rb') as stream:
                        md5 = hashlib.file_digest(stream, 'md5').hexdigest()
                    if (len(matches) != 1 or matches[0][0] != row['cloud_file_id']
                            or matches[0][1] != md5 or int(matches[0][2]) != source.stat().st_size):
                        raise ValueError('new_client_file_identity_mismatch')
    cfg = {**original, 'source_root': worker['source_root'], 'authorized_source_root': worker['source_root'],
        'compression_registry': migration['registry_paths'][original['compression_registry']],
        'worker_config': str(home / 'config.json'), 'client_database': str(database)}
    alerts = read(home / 'private/compression/alerts-config.json')
    alerts.update(registry=cfg['compression_registry'], state_dir=str(Path(cfg['compression_registry']) / 'alerts'),
                  notifier_app=str(home / 'CompressionAlerts.app'))
    write(home / 'compression/config.json', cfg)
    write(home / 'compression/alerts-config.json', alerts)
    migration['client_binding_verified'] = True
    write(home / 'compression/migration.json', migration)
    print('压缩档案与新账号缓存身份核对完成；定时压缩尚未启动。')


def install(home, execute):
    home = Path(home).resolve()
    if not read(home / 'compression/migration.json')['client_binding_verified']:
        raise ValueError('compression_client_not_bound')
    if read(home / 'private/snapshot.json')['old_services_resumed']:
        raise ValueError('预部署包不启动自动回写；正式切换使用最终冻结包')
    # Activation is the single-writer gate; predeployment never writes to WeDrive.
    if not (home / 'activation.json').exists():
        raise ValueError('先完成正式接管，才能启用压缩任务')
    execute([str(home / '.venv/bin/python3'), '-c', 'import fitz, PIL; print(fitz.VersionBind)'])
    app = home / 'CompressionAlerts.app/Contents'
    (app / 'MacOS').mkdir(parents=True, exist_ok=True)
    execute(['/usr/bin/xcrun', 'swiftc', str(home / 'app/implementation/weknora-sync/CompressionAlert.swift'),
        '-o', str(app / 'MacOS/CompressionAlert'), '-framework', 'AppKit'])
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'CompressionAlert',
        'CFBundleIdentifier': 'local.robot5.CompressionAlerts', 'CFBundleName': '机器人5压缩提醒',
        'CFBundlePackageType': 'APPL', 'LSUIElement': True}))
    execute(['/usr/bin/codesign', '--force', '--sign', '-', str(app.parent)])
    scripts = home / 'app/implementation/weknora-sync'
    alerts = read(home / 'compression/alerts-config.json')
    jobs = [([str(scripts / 'compression_batch.py'), '--config', str(home / 'compression/config.json'), '--apply'],
             {'StartCalendarInterval': {'Hour': alerts['daily_hour'], 'Minute': alerts['daily_minute']}}),
            ([str(scripts / 'compression_batch.py'), '--config', str(home / 'compression/config.json'), '--apply', '--resume-only'], {'StartInterval': 120}),
            ([str(scripts / 'compression_alerts.py'), '--config', str(home / 'compression/alerts-config.json')], {'StartInterval': 300})]
    plists = []
    for label, (args, schedule) in zip(LABELS, jobs):
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        data = {'Label': label, 'ProgramArguments': [str(home / '.venv/bin/python3'), *args], **schedule,
            'WorkingDirectory': str(home / 'app'), 'Umask': 63,
            'EnvironmentVariables': {'TZ': 'Asia/Shanghai', 'PATH': '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin'},
            'StandardOutPath': str(home / 'compression' / (label + '.stdout.log')),
            'StandardErrorPath': str(home / 'compression' / (label + '.stderr.log'))}
        content = plistlib.dumps(data)
        if path.exists() and path.read_bytes() != content:
            raise ValueError('compression_launchagent_owned_by_other_install')
        plists.append((path, content))
    for path, content in plists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
        label = plistlib.loads(content)['Label']
        if subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True).returncode:
            execute(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(path)])
    print('压缩任务已安装：每天03:00；恢复每120秒；异常检查每300秒。')
