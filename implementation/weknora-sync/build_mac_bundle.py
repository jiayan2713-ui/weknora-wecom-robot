"""Build an encrypted full Mac migration snapshot and restore old services afterwards."""
import argparse
import fcntl
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import zipfile

from mac_bundle_crypto import encrypt
from replica_protocol import file_hash, inside, read, write

PROJECT = Path(__file__).resolve().parents[2]
LABEL = 'com.xinchengyuan.weknora-project-consultation-sync'
HISTORY_LABEL = 'com.xinchengyuan.robot5-history-collector'
COMPRESSION_LABELS = ['com.xinchengyuan.wedrive-test-compression',
    'com.xinchengyuan.wedrive-test-compression-recovery', 'com.xinchengyuan.wedrive-test-compression-alerts']
REPO = Path('/Users/xinchengyuan/Documents/ChatGPT/调研/weknora-local')
NAMES = ['WeKnora-frontend','WeKnora-app','WeKnora-docreader','WeKnora-postgres',
         'WeKnora-neo4j','WeKnora-redis','WeKnora-bailian-asr']


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError('部署备份子步骤失败：' + args[0] + '（未打印含凭据的诊断）')
    return result.stdout


def machine_id():
    data = run(['ioreg','-rd1','-c','IOPlatformExpertDevice'])
    import re
    match = re.search(rb'"IOPlatformUUID"\s*=\s*"([^"]+)"', data)
    if not match:
        raise RuntimeError('无法识别Mac机器身份')
    return hashlib.sha256(match[1]).hexdigest()


def resume_old(journal):
    data=read(journal)
    if data['source_machine']!=machine_id():
        raise ValueError('恢复旧服务操作只能在来源Mac执行')
    if data.get('stopped'):
        run(['docker','start',*data['stopped']])
    if data.get('worker_was_loaded'):
        status=subprocess.run(['launchctl','print','gui/'+str(os.getuid())+'/'+LABEL],capture_output=True)
        if status.returncode:
            run(['launchctl','bootstrap','gui/'+str(os.getuid()),data['plist']])
    if data.get('history_was_loaded'):
        if subprocess.run(['launchctl','print','gui/'+str(os.getuid())+'/'+HISTORY_LABEL],capture_output=True).returncode:
            run(['launchctl','bootstrap','gui/'+str(os.getuid()),data['history_plist']])
    for label,plist in data.get('compression_loaded',{}).items():
        if subprocess.run(['launchctl','print','gui/'+str(os.getuid())+'/'+label],capture_output=True).returncode:
            run(['launchctl','bootstrap','gui/'+str(os.getuid()),plist])
    data['status']='resumed'
    data['resumed_at']=datetime.now().isoformat()
    write(journal,data)


def copy_tree(source, destination):
    source, destination = Path(source), Path(destination)
    for path in source.rglob('*'):
        if path.is_symlink():
            continue
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            target.chmod(0o600)


def database_output(command, destination):
    with Path(destination).open('wb') as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('数据库或存储卷导出失败，旧服务将恢复')


def build(output, *, leave_old_stopped=False):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('部署包输出目录必须为全新目录')
    output.mkdir(parents=True)
    private = PROJECT / '.local/mac-deploy' / output.name
    private.mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    stage = private / 'payload'
    stage.mkdir(mode=0o700)
    key = private / 'unlock.key'
    key.write_text(secrets.token_hex(32) + '\n')
    key.chmod(0o600)
    cfg = read(PROJECT / '.local/weknora-sync/config.json')
    containers = json.loads(run(['docker','inspect',*NAMES]))
    running = [c['Name'].lstrip('/') for c in containers if c['State']['Running']]
    compose = json.loads(run(['docker','compose','--project-directory',str(REPO),
                             '--profile','*','config','--format','json']))
    services, volume_sources = {}, {}
    platform = stage / 'platform'
    platform.mkdir(mode=0o700)
    helper = next(c for c in containers if c['Name']=='/WeKnora-neo4j')['Config']['Image']
    for container in containers:
        name = container['Config']['Labels']['com.docker.compose.service']
        service = dict(compose['services'][name])
        image = json.loads(run(['docker','image','inspect',container['Image']]))[0]
        digests = image.get('RepoDigests') or []
        if not digests:
            raise RuntimeError('镜像没有可校验的仓库摘要，暂停迁移')
        service['image'] = digests[0]
        service['environment'] = dict(item.split('=',1) for item in container['Config']['Env'])
        service['networks'] = ['robot5']
        service['restart'] = 'unless-stopped'
        for field in ('build','profiles','container_name','labels'):
            service.pop(field,None)
        service['depends_on'] = {k:v for k,v in service.get('depends_on',{}).items()
                                 if any(c['Config']['Labels']['com.docker.compose.service']==k for c in containers)}
        mounts = []
        for index, mount in enumerate(container['Mounts']):
            if mount['Type']=='bind':
                relative = 'binds/' + name + '-' + str(index)
                source = Path(mount['Source'].removeprefix('/host_mnt'))
                target = platform / relative
                if source.is_dir():
                    copy_tree(source,target)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,target)
                mounts.append({'type':'bind','source':'./'+relative,'target':mount['Destination'],
                               'read_only':not mount['RW']})
            elif mount['Type']=='volume':
                original = next((m.get('source') for m in service.get('volumes',[])
                                 if m.get('target')==mount['Destination']), None)
                if not original: # Anonymous logs are not runtime knowledge state.
                    continue
                volume_sources[original] = mount['Name']
                mounts.append({'type':'volume','source':original,'target':mount['Destination'],
                               'read_only':not mount['RW']})
        service['volumes'] = mounts
        services[name] = service
    blueprint = {'name':'robot5-mac-'+datetime.now().strftime('%Y%m%d%H%M%S'),
                 'services':services, 'volumes':{key:{} for key in volume_sources},
                 'networks':{'robot5':{'driver':'bridge'}}}
    write(platform/'compose.json',blueprint)
    plist = Path.home()/'Library/LaunchAgents'/ (LABEL+'.plist')
    domain='gui/'+str(os.getuid())
    worker_was_loaded = subprocess.run(['launchctl','print',domain+'/'+LABEL],capture_output=True).returncode==0
    history_plist=Path.home()/'Library/LaunchAgents'/(HISTORY_LABEL+'.plist')
    history_was_loaded=subprocess.run(['launchctl','print',domain+'/'+HISTORY_LABEL],capture_output=True).returncode==0
    compression_loaded={label:str(Path.home()/'Library/LaunchAgents'/(label+'.plist'))
        for label in COMPRESSION_LABELS if subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode==0}
    stopped = []
    journal=private/'resume-old.json'
    write(journal,{'source_machine':machine_id(),'worker_was_loaded':worker_was_loaded,
                  'plist':str(plist),'history_was_loaded':history_was_loaded,
                  'history_plist':str(history_plist),'compression_loaded':compression_loaded,'stopped':[],'status':'preparing'})
    snapshot_complete=False
    lock=(Path(cfg['state_dir'])/'worker.lock').open('a')
    compression_lock=None
    try:
        # Stop compressor writers before taking worker lock: compressor can acquire it.
        for label,path in compression_loaded.items():
            run(['launchctl','bootout',domain,path])
        batch_cfg=read(PROJECT/'outputs/测试目录定时压缩_20261013/config.json')
        compression_lock=(Path(batch_cfg['compression_registry'])/'batch.lock').open('a')
        from compression_batch import acquire_worker_lock
        acquire_worker_lock(compression_lock,120)
        if worker_was_loaded:
            run(['launchctl','bootout','gui/'+str(os.getuid()),str(plist)])
        if history_was_loaded:
            run(['launchctl','bootout','gui/'+str(os.getuid()),str(history_plist)])
        deadline=time.time()+120
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time()>deadline:raise RuntimeError('旧同步循环未停止，取消备份并恢复服务')
                time.sleep(.5)
        # Stop writers; keep Postgres running only for a consistent logical dump.
        stopped = [name for name in running if name!='WeKnora-postgres']
        write(journal,{'source_machine':machine_id(),'worker_was_loaded':worker_was_loaded,
                      'plist':str(plist),'history_was_loaded':history_was_loaded,
                      'history_plist':str(history_plist),'compression_loaded':compression_loaded,'stopped':stopped,'status':'paused',
                      'paused_at':datetime.now().isoformat()})
        run(['docker','stop','--time','60',*stopped])
        write(platform/'enabled-channel-ids.json',json.loads(run(['docker','exec','WeKnora-postgres','sh','-c',
          'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT coalesce(json_agg(id), \'[]\'::json) FROM im_channels WHERE enabled=true AND deleted_at IS NULL"'])))
        counts=run(['docker','exec','WeKnora-postgres','sh','-c',
                   'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT count(*) FROM knowledges WHERE deleted_at IS NULL"']).decode().strip()
        database_output(['docker','exec','WeKnora-postgres','sh','-c',
                         'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z1 --no-owner --no-privileges'],
                         platform/'postgres.dump')
        (platform/'volumes').mkdir()
        for logical, actual in volume_sources.items():
            if logical=='postgres-data':
                continue
            database_output(['docker','run','--rm','--network','none','--user','0',
                             '--mount','type=volume,src='+actual+',dst=/snapshot,readonly',
                             '--entrypoint','tar',helper,'-czf','-','-C','/snapshot','.'],
                             platform/'volumes'/(logical+'.tgz'))
        sync = stage/'sync'
        sync.mkdir()
        write(sync/'source-config.json',cfg)
        state = read(Path(cfg['state_dir'])/'state.json')
        write(sync/'state.json',state)
        for directory in ('artifacts','history','retired-backups','jobs','failure-alerts','logs','pdf-compatibility'):
            copy_tree(Path(cfg['state_dir'])/directory,sync/directory)
        copy_tree(cfg['markdown_root'],sync/'markdown')
        (sync/'private').mkdir(mode=0o700)
        shutil.copy2(cfg['api_key_file'],sync/'private/api-key.txt')
        shutil.copy2(cfg['model_secrets_file'],sync/'private/model-env.json')
        source_fingerprints = {}
        for record in state['records'].values():
            if record['state']=='deleted':
                continue
            name = record.get('desired_path') or record['path']
            source = inside(cfg['source_root'],name)
            source_fingerprints[name] = file_hash(source) if source.is_file() else None
        write(sync/'source-fingerprints.json',source_fingerprints)
        from compression_deploy import snapshot as compression_snapshot
        compression_info=compression_snapshot(PROJECT,cfg,stage/'compression')
        metadata = {'created_at':datetime.now().isoformat(),'source_machine':machine_id(),
                    'source_docker_engine':hashlib.sha256(run(['docker','info','--format','{{.ID}}']).strip()).hexdigest(),
                    'knowledge_base_id':cfg['knowledge_base_id'], 'mapped_sources':len(source_fingerprints),
                    'platform_knowledge_count':int(counts), 'source_mode':'local',
                    'snapshot':'cold-volume-and-logical-postgres','old_services_resumed':not leave_old_stopped,
                    'compression':compression_info,'included_features':['sync','parser','history_collector','compression','operational_logs','desktop_alerts']}
        write(stage/'snapshot.json',metadata)
        sys.path.insert(0,str(PROJECT/'implementation/robot5-history-collector'))
        from migration import snapshot as history_snapshot
        history_snapshot(PROJECT/'outputs/机器人5历史问答采集',REPO/'wecom-connection.json',
                         stage/'history-collector',refresh=True)
        snapshot_complete=True
    finally:
        lock.close()
        if compression_lock:compression_lock.close()
        if not leave_old_stopped or not snapshot_complete:
            resume_old(journal)
    app = output/'app'
    for directory in ('docflow','提示词','implementation/weknora-sync','implementation/robot5-history-collector'):
        source = PROJECT/directory
        for path in source.rglob('*'):
            if path.is_file() and (path.suffix in ('.py','.md','.json','.plist','.mjs','.swift') or path.name=='requirements-mac.txt') and '__pycache__' not in path.parts:
                target=app/directory/path.relative_to(source)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target)
    shutil.copy2(PROJECT/'implementation/weknora-sync/requirements-mac.txt',app/'requirements.txt')
    for name in ('mac_deploy.py','mac_bundle_crypto.py','compression_deploy.py'):
        shutil.copy2(PROJECT/'implementation/weknora-sync'/name,output/name)
    shutil.copy2(PROJECT/'implementation/weknora-sync/README_mac_deploy.md',output/'开始部署.md')
    write(output/'程序校验.json',{str(p.relative_to(output)):file_hash(p) for p in output.rglob('*')
        if p.is_file() and p.name not in ('payload.enc','程序校验.json','manifest.json')})
    metadata['program_manifest_sha256']=file_hash(output/'程序校验.json')
    manifest = encrypt(stage,output/'payload.enc',key,metadata)
    write(output/'manifest.json',manifest)
    zip_path = output.with_suffix('.zip')
    with zipfile.ZipFile(zip_path,'w') as archive:
        for path in output.rglob('*'):
            if path.is_file():
                archive.write(path,str(path.relative_to(output.parent)),
                              compress_type=zipfile.ZIP_STORED if path.name=='payload.enc' else zipfile.ZIP_DEFLATED)
    print('部署包：',zip_path)
    print('解锁文件单独保存在本机私有目录：',key)
    return output,key


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    parser.add_argument('--resume-old',type=Path)
    parser.add_argument('--leave-old-stopped',action='store_true')
    args=parser.parse_args()
    if args.resume_old:
        resume_old(args.resume_old)
        return
    if not args.output:raise ValueError('需要输出目录')
    try:
        build(args.output,leave_old_stopped=args.leave_old_stopped)
    except BaseException:
        journal=PROJECT/'.local/mac-deploy'/args.output.resolve().name/'resume-old.json'
        if journal.exists() and read(journal)['status']!='resumed':resume_old(journal)
        raise


if __name__=='__main__':
    main()
