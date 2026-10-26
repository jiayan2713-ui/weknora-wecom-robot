"""Extend a predeployment snapshot without stopping or replacing the live platform."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import zipfile

from mac_bundle_crypto import encrypt, unpack
from replica_protocol import file_hash, read, write

PROJECT = Path(__file__).resolve().parents[2]


def augment(base, base_key, output):
    if output.exists():
        raise ValueError('新包目录已存在，未覆盖')
    private = PROJECT/'.local/mac-deploy'/output.name
    if private.exists():
        raise ValueError('新包私有目录已存在，未覆盖')
    private.mkdir(parents=True, mode=0o700)
    stage = private/'payload'
    unpack(base/'payload.enc', base_key, read(base/'manifest.json'), stage)
    shutil.copytree(base, output, ignore=shutil.ignore_patterns('payload.enc','manifest.json','部署包校验.json'))
    output.chmod(0o700)
    sys.path.insert(0,str(PROJECT/'implementation/robot5-history-collector'))
    from migration import snapshot, restore
    snapshot(PROJECT/'outputs/机器人5历史问答采集',
             Path('/Users/xinchengyuan/Documents/ChatGPT/调研/weknora-local/wecom-connection.json'),
             stage/'history-collector')
    history = read(stage/'history-collector/snapshot.json')
    # The platform snapshot stays explicitly dated; this is still a predeployment package.
    metadata = read(stage/'snapshot.json')
    if not metadata['old_services_resumed']:
        raise ValueError('此增补工具仅用于预部署包；正式切换请重新全量冻结导出')
    metadata['history_archive'] = history
    metadata['supplemented_at'] = history['captured_at']
    write(stage/'snapshot.json',metadata)
    for module in ('mac_deploy.py','mac_bundle_crypto.py','build_mac_bundle.py','augment_mac_history.py'):
        shutil.copy2(PROJECT/'implementation/weknora-sync'/module,output/'app/implementation/weknora-sync'/module)
    for module in ('mac_deploy.py','mac_bundle_crypto.py'):
        shutil.copy2(PROJECT/'implementation/weknora-sync'/module,output/module)
    collector_target=output/'app/implementation/robot5-history-collector'
    collector_target.mkdir(parents=True,exist_ok=True)
    for path in (PROJECT/'implementation/robot5-history-collector').iterdir():
        if path.is_file() and path.suffix in ('.py','.md','.mjs'):
            shutil.copy2(path,collector_target/path.name)
    shutil.copy2(PROJECT/'implementation/weknora-sync/README_mac_deploy.md',output/'开始部署.md')
    shutil.copy2(PROJECT/'implementation/weknora-sync/README_mac_deploy.md',output/'app/implementation/weknora-sync/README_mac_deploy.md')
    key=private/'unlock.key'
    key.write_text(secrets.token_hex(32)+'\n')
    key.chmod(0o600)
    manifest=encrypt(stage,output/'payload.enc',key,metadata)
    write(output/'manifest.json',manifest)
    verified=private/'verified-payload'
    unpack(output/'payload.enc',key,manifest,verified)
    files=['platform/postgres.dump','sync/state.json','sync/private/api-key.txt',
           'sync/private/model-env.json','history-collector/history.sqlite3','history-collector/binding.json']
    if not all(file_hash(stage/path)==file_hash(verified/path) for path in files):
        raise ValueError('加密包回读字节校验失败')
    restored=private/'isolated-history-restore'
    restore(verified/'history-collector',restored,private/'isolated-compose.json')
    with sqlite3.connect(stage/'history-collector/history.sqlite3') as original, sqlite3.connect(restored/'history.sqlite3') as target:
        for table in ('messages','versions'):
            if original.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()!=target.execute(f'SELECT * FROM {table} ORDER BY id').fetchall():
                raise ValueError('历史消息或版本迁移不一致')
    validation={'status':'passed','history_messages':history['messages'],'history_qa_records':history['qa_records'],
                'history_original_bytes_preserved':True,'history_versions_preserved':True,
                'encrypted_round_trip':True,'sqlite_backup_not_wal_copy':True,
                'excel_validation':read(restored/'excel-validation.json'),
                'new_mac_execution':'not_run','package_stage':'predeployment',
                'platform_snapshot_at':metadata['created_at'],'history_snapshot_at':history['captured_at']}
    write(output/'问答采集迁移验收.json',validation)
    return output,key


def finalize(output):
    zip_path=output.with_suffix('.zip')
    with zipfile.ZipFile(zip_path,'w') as archive:
        for path in output.rglob('*'):
            if path.is_file():
                archive.write(path,str(path.relative_to(output.parent)),compress_type=(
                    zipfile.ZIP_STORED if path.name=='payload.enc' else zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None:
            raise ValueError('部署ZIP校验失败')
    checksum=file_hash(zip_path)
    zip_path.with_suffix('.sha256').write_text(checksum+'  '+zip_path.name+'\n')
    print(json.dumps({'package':str(zip_path),'bytes':zip_path.stat().st_size,'sha256':checksum},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--base-key',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077)
    augment(args.base.resolve(),args.base_key.resolve(),args.output.resolve())
    finalize(args.output.resolve())
