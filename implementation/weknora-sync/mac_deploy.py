"""Staged installation on a different Mac; channels/deletion start disabled."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import time

from mac_bundle_crypto import unpack


def read(path):
    return json.loads(Path(path).read_text())


def write(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    temporary.chmod(0o600)
    os.replace(temporary,path)


def run(args, **kwargs):
    result=subprocess.run(args,capture_output=True,**kwargs)
    if result.returncode:
        raise RuntimeError('部署步骤失败：'+args[0]+'；诊断可能含凭据，未输出到聊天')
    return result.stdout


def machine_id():
    import re
    match=re.search(rb'"IOPlatformUUID"\s*=\s*"([^"]+)"',run(['ioreg','-rd1','-c','IOPlatformExpertDevice']))
    if not match:
        raise RuntimeError('无法核验机器身份')
    return hashlib.sha256(match[1]).hexdigest()


def compose(home,*args):
    return run(['docker','compose','-f',str(home/'private/platform/compose.json'),*args])


def container(home,name):
    value=compose(home,'ps','-q',name).decode().strip()
    if not value:
        raise ValueError('容器尚未启动：'+name)
    return value


def setup(bundle,home,key):
    if sys.platform!='darwin' or sys.version_info<(3,11):
        raise ValueError('需在新macOS电脑用Python3.11以上运行')
    metadata=read(bundle/'manifest.json')
    if machine_id()==metadata['source_machine']:
        raise ValueError('这是来源Mac，禁止在原机恢复，以免覆盖当前运行环境')
    context=home/'install-context.json'
    if home.exists() and any(home.iterdir()) and not context.exists():
        raise ValueError('安装目录须为空，已有目录不会覆盖')
    home.mkdir(parents=True,mode=0o700,exist_ok=True)
    home.chmod(0o700)
    if context.exists() and read(context)['bundle_authentication']!=metadata['authentication']:
        raise ValueError('安装目录属于另一份迁移包，拒绝覆盖')
    write(context,{'bundle_authentication':metadata['authentication'],'status':'installing'})
    if not (home/'private/snapshot.json').exists():
        unpack(bundle/'payload.enc',key,metadata,home/'private')
    if metadata.get('program_manifest_sha256'):
        program_manifest=bundle/'程序校验.json'
        if hashlib.sha256(program_manifest.read_bytes()).hexdigest()!=metadata['program_manifest_sha256']:
            raise ValueError('程序清单指纹不匹配，未安装依赖')
        for name,checksum in read(program_manifest).items():
            path=bundle/name
            if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(bundle.resolve()):
                raise ValueError('程序清单路径越界')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
                raise ValueError('程序文件校验失败，未安装依赖')
    if not (home/'app').exists():
        shutil.copytree(bundle/'app',home/'app')
    run([sys.executable,'-m','venv',str(home/'.venv')])
    python=home/'.venv/bin/python3'
    run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(home/'app/requirements.txt')])
    write(context,{'bundle_authentication':metadata['authentication'],'status':'installed'})
    print('已解密并安装程序；尚未启动同步或机器人渠道。')


def wait_postgres(pg):
    # pg_isready also succeeds against the entrypoint's temporary init server.
    # Wait until PID 1 has exec'd the permanent server before streaming pg_restore.
    for _ in range(120):
        ready=subprocess.run(['docker','exec',pg,'sh','-c',
            'test "$(cat /proc/1/comm)" = postgres && pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],capture_output=True)
        if ready.returncode==0:
            return
        time.sleep(1)
    raise RuntimeError('PostgreSQL正式服务未就绪，未启动问答渠道')


def restore_platform(home):
    platform=home/'private/platform'
    snapshot=read(home/'private/snapshot.json')
    if machine_id()==snapshot['source_machine'] or hashlib.sha256(
            run(['docker','info','--format','{{.ID}}']).strip()).hexdigest()==snapshot['source_docker_engine']:
        raise ValueError('不能向来源Mac或来源Docker引擎恢复')
    cfg=read(platform/'compose.json')
    project=cfg['name']
    marker=home/'restore-platform.json'
    if marker.exists():
        raise ValueError('已开始或完成恢复，先查看restore-platform.json，不自动覆盖数据库')
    existing=run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project]).decode().strip()
    volumes=run(['docker','volume','ls','-q','--filter','label=com.docker.compose.project='+project]).decode().strip()
    if existing or volumes:
        raise ValueError('检测到同名部署数据，拒绝覆盖')
    for service in cfg['services'].values():
        for mount in service.get('volumes',[]):
            if mount['type']=='bind':
                mount['source']=str((platform/mount['source']).resolve())
    write(platform/'compose.json',cfg)
    write(marker,{'status':'restoring','project':project})
    compose(home,'pull')
    compose(home,'create')
    helper=cfg['services']['neo4j']['image']
    for archive in (platform/'volumes').glob('*.tgz'):
        with tarfile.open(archive,'r:gz') as data:
            for member in data.getmembers():
                if member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise ValueError('存储卷备份路径不安全')
        volume=project+'_'+archive.stem
        run(['docker','run','--rm','--network','none','--user','0',
             '--mount','type=volume,src='+volume+',dst=/restore',
             '--mount','type=bind,src='+str(archive.parent)+',dst=/backup,readonly',
             '--entrypoint','tar',helper,'-xzf','/backup/'+archive.name,'-C','/restore'])
    compose(home,'up','-d','postgres')
    pg=container(home,'postgres')
    wait_postgres(pg)
    with (platform/'postgres.dump').open('rb') as dump:
        result=subprocess.run(['docker','exec','-i',pg,'sh','-c',
            'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --exit-on-error'],
            stdin=dump,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('数据库恢复失败，服务保留为暂停状态，不重置任何已有数据')
    run(['docker','exec',pg,'sh','-c',
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "UPDATE im_channels SET enabled=false WHERE enabled=true"'])
    count=int(run(['docker','exec',pg,'sh','-c',
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT count(*) FROM knowledges WHERE deleted_at IS NULL"']).strip())
    if count!=read(home/'private/snapshot.json')['platform_knowledge_count']:
        raise RuntimeError('知识数量不符，停止后续启用')
    compose(home,'up','-d')
    write(marker,{'status':'restored_channels_disabled','project':project,'knowledge_count':count})
    print('知识库已恢复；企业微信问答渠道保持关闭。打开 http://127.0.0.1:8090 核对。')


def bind(home,source_root):
    sys.path.insert(0,str(home/'app/implementation/weknora-sync'))
    from reconcile import hashes
    from replica_protocol import relative
    old=read(home/'private/sync/source-config.json')
    state=read(home/'private/sync/state.json')
    expected=read(home/'private/sync/source-fingerprints.json')
    source_root=Path(source_root).resolve()
    missing,changed=[],[]
    for name,checksum in expected.items():
        path=source_root/relative(name)
        if not checksum or not path.is_file():
            missing.append(name)
        elif hashes(path)['sha256']!=checksum:
            changed.append(name)
    report={'status':'matched' if not missing and not changed else 'blocked',
            'expected_sources':len(expected),'missing':missing,'changed':changed,
            'source_root':str(source_root)}
    write(home/'source-check.json',report)
    if report['status']!='matched':
        raise ValueError('首次对账未通过；查看source-check.json，未启用同步与删除')
    if (home/'state/state.json').exists():
        raise ValueError('已绑定状态，拒绝覆盖')
    soffice=shutil.which('soffice') or '/Applications/LibreOffice.app/Contents/MacOS/soffice'
    pdftoppm=shutil.which('pdftoppm')
    if not Path(soffice).is_file() or not pdftoppm or not Path('/usr/bin/sandbox-exec').is_file():
        raise ValueError('缺少LibreOffice、Poppler或macOS转换隔离机制，尚未启用后台')
    new_md=home/'markdown'
    from compression_deploy import restore_archives
    compression_paths=restore_archives(home,source_root)
    shutil.copytree(home/'private/sync/markdown',new_md,dirs_exist_ok=True)
    (home/'state').mkdir(exist_ok=True)
    for name in ('artifacts','history','retired-backups','jobs','failure-alerts','logs','pdf-compatibility'):
        source=home/'private/sync'/name
        if source.exists():
            shutil.copytree(source,home/'state'/name,dirs_exist_ok=True)
    old_home=Path(old['state_dir'])
    old_md=Path(old['markdown_root'])
    def rebind(value):
        path=Path(value)
        for original,target in [(old_home,home/'state'),(old_md,new_md),
                                *((Path(a),Path(b)) for a,b in compression_paths.items())]:
            if path.is_relative_to(original):
                return str(target/path.relative_to(original))
        return value
    def rebind_tree(value):
        if isinstance(value,dict):return {k:rebind_tree(v) for k,v in value.items()}
        if isinstance(value,list):return [rebind_tree(v) for v in value]
        return rebind(value) if isinstance(value,str) and value.startswith('/') else value
    for record in state['records'].values():
        for field in ('md_path','cached_md','latest_parse_report_path','original_artifact','direct_report_path'):
            if record.get(field):
                record[field]=rebind(record[field])
        if record.get('pending'):
            record['pending']=rebind_tree(record['pending'])
        for provenance in (record.get('provenance'),record.get('pending',{}).get('provenance')):
            if isinstance(provenance,dict):
                for field in ('original_snapshot','parse_report_path'):
                    if provenance.get(field):provenance[field]=rebind(provenance[field])
        record.pop('missing_since',None)
        record['missing_scans']=0
        if record.get('compression'):
            record['compression']=rebind_tree(record['compression'])
            record['compression']['source_root']=str(source_root)
    stat=source_root.stat()
    state.update(identity=[str(source_root),old['knowledge_base_id'],'http://127.0.0.1:8090'],
                 root_identity=[stat.st_dev,stat.st_ino],cache={},seeded=True)
    cfg={**old,'source_root':str(source_root),'markdown_root':str(new_md),'state_dir':str(home/'state'),
         'source_mode':'local','weknora_base_url':'http://127.0.0.1:8090',
         'api_key_file':str(home/'private/sync/private/api-key.txt'),
         'model_secrets_file':str(home/'private/sync/private/model-env.json'),
         'automatic_deletion_enabled':False,'reuse_robot1_artifacts':False}
    cfg['parser_config']={**old.get('parser_config',{}),'soffice':soffice,'pdftoppm':pdftoppm}
    cfg['pdf_compatibility_python']=str(home/'.venv/bin/python3')
    if cfg.get('parser_skip_extensions'):
        cfg['table_docreader_container']=container(home,'docreader')
    if compression_paths:
        cfg['compression_registry']=compression_paths[old['compression_registry']]
        cfg['compression_additional_registries']=[compression_paths[p] for p in old.get('compression_additional_registries',[])]
        cfg['compression_local_test_sources']=[]
    if isinstance(cfg.get('operational_logging'),dict):
        cfg['operational_logging']={**cfg['operational_logging'],'runtime_files':[
            str(home/'watch.stdout.log'),str(home/'watch.stderr.log')]}
    from pdf_compatibility import migration_retries
    pdf_retries=migration_retries(cfg,state['records'])
    write(home/'state/state.json',state)
    write(home/'config.json',cfg)
    for request in pdf_retries:
        request['requested_at']=time.time()
        name=hashlib.sha256(request['source_id'].encode()).hexdigest()+'.json'
        write(home/'state/parse-retries'/name,request)
    print('原件对账与映射绑定通过；自动删除仍关闭。')


def start_sync(home):
    if read(home/'source-check.json')['status']!='matched':
        raise ValueError('先完成来源对账')
    label='com.robot5.mac-sync'
    plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    plist.parent.mkdir(parents=True,exist_ok=True)
    if plist.exists():raise ValueError('已有同名后台任务，未覆盖')
    data={'Label':label,'ProgramArguments':[str(home/'.venv/bin/python3'),
          str(home/'app/implementation/weknora-sync/worker.py'),'watch','--config',str(home/'config.json')],
          'RunAtLoad':True,'KeepAlive':True,'WorkingDirectory':str(home/'app'),
          'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'},'ThrottleInterval':30,'ExitTimeOut':600,
          'StandardOutPath':str(home/'watch.stdout.log'),'StandardErrorPath':str(home/'watch.stderr.log')}
    with plist.open('wb') as stream:plistlib.dump(data,stream)
    run(['launchctl','bootstrap','gui/'+str(os.getuid()),str(plist)])
    print('新电脑同步已启动，问答渠道和自动删除仍关闭。')


def start_history(home):
    if read(home/'restore-platform.json')['status']!='restored_channels_disabled':
        raise ValueError('先恢复知识库，采集不启用机器人渠道')
    root=home/'history-archive'
    python=home/'.venv/bin/python3'
    if not (root/'binding.json').exists():
        run([str(python),str(home/'app/implementation/robot5-history-collector/migration.py'),
             '--source',str(home/'private/history-collector'),'--root',str(root),
             '--compose-file',str(home/'private/platform/compose.json')])
    expected=read(home/'private/history-collector/binding.json')
    actual=read(root/'binding.json')
    if any(actual[key]!=expected[key] for key in ('channel_id','agent_id')) or \
            actual.get('compose_file')!=str((home/'private/platform/compose.json').resolve()):
        raise ValueError('采集绑定不属于当前部署，未启动')
    script=home/'app/implementation/robot5-history-collector/collector.py'
    run([str(python),str(script),'sync','--root',str(root),'--binding',str(root/'binding.json'),'--full'])
    label='com.robot5.mac-history-collector'
    plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    data={'Label':label,'ProgramArguments':[str(python),str(script),'sync','--root',str(root),
          '--binding',str(root/'binding.json')],'RunAtLoad':True,'StartInterval':60,
          'WorkingDirectory':str(home/'app'),'Umask':63,
          'EnvironmentVariables':{'PATH':'/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin'},
          'StandardOutPath':str(root/'service.log'),'StandardErrorPath':str(root/'service-error.log')}
    content=plistlib.dumps(data)
    if plist.exists() and plist.read_bytes()!=content:
        raise ValueError('已有其他部署的问答采集服务，未覆盖')
    plist.parent.mkdir(parents=True,exist_ok=True)
    plist.write_bytes(content)
    plist.chmod(0o600)
    domain='gui/'+str(os.getuid())
    if subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode:
        run(['launchctl','bootstrap',domain,str(plist)])
    print('历史档案已恢复并完成全量去重核对；问答采集每60秒运行，问答渠道仍关闭。')


def activate(home,old_stopped):
    if not old_stopped or read(home/'private/snapshot.json')['old_services_resumed']:
        raise ValueError('正式切换需最终冻结包，并确认旧电脑服务停止；预部署包不直接接管机器人')
    if (home/'private/history-collector').exists():
        import datetime as dt
        history_health=read(home/'history-archive/health.json')
        stamp=dt.datetime.fromisoformat(history_health['completed_at'])
        age=(dt.datetime.now(dt.timezone.utc)-stamp).total_seconds()
        history_plist=Path.home()/'Library/LaunchAgents/com.robot5.mac-history-collector.plist'
        if history_health['status']!='success' or not 0<=age<=300 or not history_plist.exists() or \
                plistlib.loads(history_plist.read_bytes())['ProgramArguments'][0]!=str(home/'.venv/bin/python3'):
            raise ValueError('问答采集尚未健康运行，未启用机器人渠道')
    health=read(home/'state/health.json')
    state=read(home/'state/state.json')
    if health['status']!='running' or time.time()-health['time']>300 or \
            any(r.get('pending') or r['state'] in ('queued','waiting_stable','missing_hold',
                'retry_wait','parse_retry_wait','parse_blocked_hold') for r in state['records'].values()):
        raise ValueError('新电脑同步尚未收敛或存在暂停项，未启用问答/删除')
    ids=read(home/'private/platform/enabled-channel-ids.json')
    if not all(isinstance(value,str) and value and all(c.isalnum() or c=='-' for c in value) for value in ids):
        raise ValueError('渠道ID格式不合法')
    cfg=read(home/'config.json')
    cfg['automatic_deletion_enabled']=True
    write(home/'config.json',cfg)
    if ids:
        sql="UPDATE im_channels SET enabled=true WHERE id IN ("+','.join("'"+value+"'" for value in ids)+");"
        run(['docker','exec','-i',container(home,'postgres'),'sh','-c',
             'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'],input=sql.encode())
        compose(home,'restart','app')
    run(['launchctl','kickstart','-k','gui/'+str(os.getuid())+'/com.robot5.mac-sync'])
    write(home/'activation.json',{'activated_at':time.time(),'old_stopped':True})
    print('正式接管已执行；继续在企业微信验收新增、修改、删除和真实回答。')


def stop_stage(home):
    from compression_deploy import LABELS
    for label in LABELS:
        path=Path.home()/'Library/LaunchAgents'/(label+'.plist')
        if path.exists():
            if plistlib.loads(path.read_bytes())['ProgramArguments'][0]!=str(home/'.venv/bin/python3'):
                raise ValueError('压缩任务属于其他部署，未停止')
            domain='gui/'+str(os.getuid())
            if subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode==0:
                run(['launchctl','bootout',domain,str(path)])
            path.unlink()
    history_plist=Path.home()/'Library/LaunchAgents/com.robot5.mac-history-collector.plist'
    if history_plist.exists():
        data=plistlib.loads(history_plist.read_bytes())
        if data['ProgramArguments'][0]!=str(home/'.venv/bin/python3'):
            raise ValueError('问答采集服务属于其他部署，未停止')
        run(['launchctl','bootout','gui/'+str(os.getuid()),str(history_plist)])
        history_plist.unlink()
    label='com.robot5.mac-sync'
    plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    if plist.exists():
        with plist.open('rb') as stream:data=plistlib.load(stream)
        if data['ProgramArguments'][0]!=str(home/'.venv/bin/python3'):
            raise ValueError('后台任务属于其他安装目录，未操作')
        run(['launchctl','bootout','gui/'+str(os.getuid()),str(plist)])
        plist.unlink()
    compose(home,'stop')
    print('预部署实例已停止，数据保留；可安装最终冻结包。')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['setup','restore','bind','start-sync','start-history','bind-compression','start-compression','activate','stop-stage'])
    parser.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--home',type=Path,default=Path.home()/'Robot5')
    parser.add_argument('--key-file',type=Path)
    parser.add_argument('--source-root',type=Path)
    parser.add_argument('--client-database',type=Path)
    parser.add_argument('--old-stopped',action='store_true')
    args=parser.parse_args()
    home=args.home.expanduser().resolve()
    if args.operation=='setup':
        if not args.key_file:raise ValueError('需要单独传输的解锁文件')
        setup(args.bundle.resolve(),home,args.key_file.resolve())
    elif args.operation=='restore':restore_platform(home)
    elif args.operation=='bind':
        if not args.source_root:raise ValueError('需要新账号的项目to咨询本地同步目录')
        bind(home,args.source_root)
    elif args.operation=='start-sync':start_sync(home)
    elif args.operation=='start-history':start_history(home)
    elif args.operation=='bind-compression':
        if not args.client_database:raise ValueError('需要新专用企微账号的本地微盘缓存数据库路径')
        from compression_deploy import bind_client
        bind_client(home,args.client_database)
    elif args.operation=='start-compression':
        from compression_deploy import install
        install(home,run)
    elif args.operation=='stop-stage':stop_stage(home)
    else:activate(home,args.old_stopped)


if __name__=='__main__':main()
