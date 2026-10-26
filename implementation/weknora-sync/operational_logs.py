"""Small metadata-only file journal. Private evidence is never included in support exports."""
import collections
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager

ZONE = dt.timezone(dt.timedelta(hours=8))
SEGMENT = re.compile(r'^(events|runtime)-(\d{4}-\d{2}-\d{2})\.(\d{4})\.jsonl(?:\.gz)?$')
RAW_NAMES = {'watch.stdout.log','watch.stderr.log','service.log','service-error.log'}
FIELDS = {'unit_id','stage','request_hash','outcome_certainty','cache_hits','model_attempts','budget_used','budget_limit','elapsed_seconds','stop_reason','root_cause','source_id','source_hash','observed_source_hash','pending_source_hash','previous_source_hash','markdown_hash','job_id','knowledge_ids','pending_knowledge_ids',
          'previous_knowledge_ids','remote_id','path','old_path','new_path','report_ref','artifact_ref',
          'code','error_type','category','retryable','method','parse_status','enable_status','phase',
          'pending_subtasks_count','http_status','attempts','next_attempt','expected_units','completed_units',
          'missing_body_units','automatic','missing_scans','missing_seconds','reason','knowledge_base_id',
          'source_mode','parser_signature','record_count','retirement_pending','run_id','inserted','updated',
          'unchanged','scanned','messages','qa_records','source_upper','legacy_import','legacy_count','states','status'}


def safe_fields(fields):
    fields=dict(fields)
    for original,target in [('remote_ids','knowledge_ids'),('old','old_path'),('new','new_path'),('sources','record_count')]:
        if original in fields and target not in fields:fields[target]=fields[original]
    result = {}
    for key,value in fields.items():
        if key not in FIELDS or value is None:
            continue
        if key == 'states':
            if isinstance(value,dict):
                result[key] = {k:v for k,v in value.items() if re.fullmatch(r'[a-z_]{1,60}',str(k)) and type(v) is int}
        elif key in {'knowledge_ids','previous_knowledge_ids','pending_knowledge_ids'}:
            if isinstance(value,list):
                result[key] = [v for v in value if isinstance(v,str) and re.fullmatch(r'[a-zA-Z0-9-]{1,64}',v)][:100]
        elif key in {'source_hash','observed_source_hash','pending_source_hash','previous_source_hash','markdown_hash','job_id','parser_signature'}:
            if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value):result[key]=value
        elif key in {'path','old_path','new_path','report_ref','artifact_ref'}:
            if isinstance(value,str) and not Path(value).is_absolute() and '..' not in Path(value).parts:
                result[key]=value[:2048]
        elif key in {'code','error_type'}:
            if isinstance(value,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',value):result[key]=value
        elif type(value) in (bool,int,float):
            if type(value) is not float or abs(value)<1e15:result[key]=value
        elif isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_:.+\- TZ/]{1,160}',value):
            result[key]=value
    return result


def error_fields(error):
    prefix=str(error).split(':',1)[0]
    codes={'source_read_failed':'SOURCE_READ_FAILED','excel_export_failed':'EXPORT_FAILED',
           'robot5_binding_not_found_or_changed':'BINDING_CHANGED',
           'archive_scope_changed_use_a_separate_data_directory':'ARCHIVE_SCOPE_CHANGED'}
    return {'code':codes.get(prefix,'OPERATION_FAILED'),'error_type':type(error).__name__}


def private_json(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w',encoding='utf-8') as stream:
        os.fchmod(stream.fileno(),0o600)
        json.dump(value,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)


class OperationalLogs:
    def __init__(self,cfg,clock=time.time,component='sync_worker'):
        self.home=Path(cfg['state_dir']).resolve();self.root=self.home/'logs'
        settings=cfg.get('operational_logging',{})
        self.settings=settings if isinstance(settings,dict) else {}
        self.enabled=self.settings.get('enabled') is True
        self.cfg,self.clock,self.component=cfg,clock,component
        self.service_run_id=uuid.uuid4().hex
        self.started=False;self.next_maintenance=0;self.last_error=None
        value=self.settings.get('segment_bytes',10*1024*1024)
        self.segment_bytes=value if type(value) is int and 256<=value<=100*1024*1024 else 10*1024*1024

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root.chmod(0o700)
        for name in ('audit','runtime','private-runtime','private-legacy'):
            (self.root/name).mkdir(exist_ok=True,mode=0o700)
            (self.root/name).chmod(0o700)
        lock=self.root/'writer.lock'
        with os.fdopen(os.open(lock,os.O_CREAT|os.O_RDWR,0o600),'a') as stream:
            os.fchmod(stream.fileno(),0o600);fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def _guard(self,action):
        if not self.enabled:return
        try:
            with self.locked():action()
            self.last_error=None
        except Exception as error:
            self.last_error=type(error).__name__
            # Never echo arbitrary exception content, request payloads, or keys.
            print('Operational log unavailable: '+self.last_error,flush=True)

    def _row(self,event,fields,level='INFO',timestamp=None,event_id=None):
        timestamp=self.clock() if timestamp is None else timestamp
        return {'schema_version':1,'event_id':event_id or uuid.uuid4().hex,
                'time_beijing':dt.datetime.fromtimestamp(timestamp,ZONE).isoformat(timespec='milliseconds'),
                'timestamp':timestamp,'component':self.component,'service_run_id':self.service_run_id,
                'pid':os.getpid(),'level':level if level in ('INFO','WARNING','ERROR') else 'INFO',
                'event':event if re.fullmatch(r'[a-z_]{1,80}',event) else 'unrecognized_event',**safe_fields(fields)}

    def _append(self,kind,rows):
        date=dt.datetime.fromtimestamp(self.clock(),ZONE).strftime('%Y-%m-%d')
        touched=set()
        for row in rows:
            line=(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n').encode()
            index=1
            while True:
                path=self.root/kind/f'{"events" if kind=="audit" else "runtime"}-{date}.{index:04}.jsonl'
                if not path.with_suffix(path.suffix+'.gz').exists() and (not path.exists() or path.stat().st_size+len(line)<=self.segment_bytes):break
                index+=1
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'ab') as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(line);stream.flush()
                touched.add(path)
        if kind=='audit':
            for path in touched:
                fd=os.open(path,os.O_WRONLY|os.O_APPEND)
                try:os.fsync(fd)
                finally:os.close(fd)

    def record(self,event,fields=None,*,level='INFO',runtime=False):
        self._guard(lambda:self._append('runtime' if runtime else 'audit',[self._row(event,fields or {},level)]))

    def bootstrap(self,legacy_events=()):
        if not self.enabled or self.started:return
        self.started=True
        def action():
            marker=self.root/'legacy-import.json'
            if not marker.exists():
                rows=[]
                for old in legacy_events:
                    if not isinstance(old,dict) or type(old.get('time')) not in (int,float):continue
                    fields={**old,'legacy_import':True};event=str(old.get('kind','legacy_event'))
                    # Old events cannot be assigned the file's CURRENT version retroactively.
                    clean=safe_fields(fields)
                    key=hashlib.sha256(json.dumps([old['time'],event,clean],sort_keys=True).encode()).hexdigest()
                    rows.append(self._row(event,clean,timestamp=old['time'],event_id=key))
                self._append('audit',rows)
                private_json(marker,{'imported_events':len(rows),'older_overwritten_events_recoverable':False})
            raw_marker=self.root/'legacy-raw-archive.json'
            if not raw_marker.exists():
                self._rotate_raw(initial=True)
                private_json(raw_marker,{'initial_outputs_archived':True})
            self._append('runtime' if self.component=='qa_collector' else 'audit',[self._row('service_started',{})])
            (self.root/'说明.md').write_text('# 日志范围\n\naudit：关键元数据事件，长期保留，满7天压缩；旧历史只回填现存事件，不补造版本。\nruntime：普通运行记录，保留30天。\nprivate-legacy：启用前旧运行输出，私有保留，不自动删除。\nprivate-runtime：后续原始输出轮转，私有保留30天。\n单段默认10MiB；原件、模型请求响应、问答正文、state和密钥不属于分享日志。导出只含audit、runtime、去敏后的当前告警摘要及说明；仍含业务文件名和标识，仅向授权人员分享。\n',encoding='utf-8')
            (self.root/'说明.md').chmod(0o600)
        for stream in (sys.stdout,sys.stderr):
            try:
                fd=stream.fileno()
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    fcntl.fcntl(fd,fcntl.F_SETFL,fcntl.fcntl(fd,fcntl.F_GETFL)|os.O_APPEND)
            except (OSError,ValueError):pass
        self._guard(action)
        if self.last_error:self.started=False

    def _gzip(self,path,destination):
        temp=destination.with_suffix(destination.suffix+'.tmp')
        expected=hashlib.sha256()
        with path.open('rb') as source,os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'wb') as raw:
            os.fchmod(raw.fileno(),0o600)
            with gzip.GzipFile(fileobj=raw,mode='wb') as target:
                for block in iter(lambda:source.read(1024*1024),b''):
                    expected.update(block);target.write(block)
        actual=hashlib.sha256()
        with gzip.open(temp,'rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):actual.update(block)
        if expected.digest()!=actual.digest():raise ValueError('archive_hash_mismatch')
        if destination.exists():raise ValueError('archive_destination_exists')
        os.replace(temp,destination)

    def baseline(self,records,knowledge_base_id):
        if not self.enabled or (self.root/'mapping-baseline.json').exists():return
        def action():
            marker=self.root/'mapping-baseline.json'
            if marker.exists():return
            rows=[]
            for identity,record in records.items():
                pending=record.get('pending') or {}
                fields={
                    'source_id':identity,'path':record.get('desired_path') or record.get('path'),
                    'source_hash':record.get('source_hash'),'observed_source_hash':record.get('latest_observed_hash'),
                    'pending_source_hash':pending.get('source_hash'),'markdown_hash':record.get('md_sha'),
                    'knowledge_ids':record.get('remote_ids',[]),'pending_knowledge_ids':pending.get('remote_ids',[]),
                    'status':record.get('state'),'knowledge_base_id':knowledge_base_id}
                report=record.get('latest_parse_report_path')
                if report and Path(report).resolve().is_relative_to(self.home/'jobs'):
                    fields['report_ref']=str(Path(report).resolve().relative_to(self.home))
                    fields['job_id']=Path(report).parent.name
                rows.append(self._row('logging_baseline_snapshot',fields))
            self._append('audit',rows)
            private_json(marker,{'record_count':len(rows),'snapshot_only_not_revalidation':True})
        self._guard(action)

    def _rotate_raw(self,initial=False):
        for value in self.settings.get('runtime_files',[]):
            path=Path(value)
            if path.name not in RAW_NAMES or path.is_symlink() or not path.is_file():continue
            forbidden=[self.home/'jobs',self.home/'artifacts']+[Path(self.cfg[k]).resolve() for k in ('source_root','markdown_root') if self.cfg.get(k)]
            if any(path.resolve().is_relative_to(base) for base in forbidden):continue
            if not path.stat().st_size or not initial and path.stat().st_size<self.segment_bytes:continue
            directory='private-legacy' if initial else 'private-runtime'
            target=self.root/directory/(dt.datetime.fromtimestamp(self.clock(),ZONE).strftime('%Y-%m-%d')+'-'+uuid.uuid4().hex+'-'+path.name+'.gz')
            self._gzip(path,target)
            # Raw fallback output only; structured audit is independently append-only.
            with path.open('r+b') as stream:stream.truncate(0)
            path.chmod(0o600)

    def maintain(self,force=False):
        if not self.enabled or not force and self.clock()<self.next_maintenance:return
        self.next_maintenance=self.clock()+3600
        def action():
            today=dt.datetime.fromtimestamp(self.clock(),ZONE).date()
            for kind in ('audit','runtime'):
                for path in list((self.root/kind).iterdir()):
                    match=SEGMENT.fullmatch(path.name)
                    if not match or path.is_symlink() or not path.is_file():continue
                    age=(today-dt.date.fromisoformat(match[2])).days
                    if kind=='runtime' and age>30:path.unlink()
                    elif age>=7 and path.suffix=='.jsonl' and not path.with_suffix(path.suffix+'.gz').exists():
                        self._gzip(path,path.with_suffix(path.suffix+'.gz'));path.unlink()
            for path in (self.root/'private-runtime').iterdir():
                if path.is_file() and not path.is_symlink() and re.fullmatch(r'\d{4}-\d{2}-\d{2}-[a-f0-9]{32}-(?:watch.stdout|watch.stderr|service|service-error)\.log\.gz',path.name):
                    if (today-dt.date.fromisoformat(path.name[:10])).days>30:path.unlink()
            for stream in (sys.stdout,sys.stderr):
                try:stream.flush()
                except (OSError,ValueError):pass
            self._rotate_raw()
        self._guard(action)

    def _records(self,kind='audit'):
        for path in sorted((self.root/kind).glob('*')):
            if path.is_symlink() or not path.is_file() or not SEGMENT.fullmatch(path.name):continue
            try:
                with (gzip.open(path,'rt',encoding='utf-8') if path.suffix=='.gz' else path.open(encoding='utf-8')) as stream:
                    for line in stream:
                        try:
                            row=json.loads(line)
                            if not isinstance(row,dict) or not isinstance(row.get('event_id'),str):
                                self.read_errors+=1;continue
                            yield row
                        except (ValueError,TypeError):self.read_errors+=1
            except (OSError,UnicodeError):self.read_errors+=1

    def query(self,source_id=None,path=None,source_hash=None,limit=100):
        self.read_errors=0;identities={source_id} if source_id else set()
        if path:
            for row in self._records():
                if path in [row.get(k) for k in ('path','old_path','new_path')] and row.get('source_id'):identities.add(row['source_id'])
        output=collections.deque(maxlen=max(1,min(1000,limit)));seen=set()
        for row in self._records():
            if (source_id or path) and row.get('source_id') not in identities:continue
            if source_hash and source_hash not in [row.get(key) for key in ('source_hash','observed_source_hash','pending_source_hash')]:continue
            if row.get('event_id') in seen:continue
            seen.add(row.get('event_id'));output.append(row)
        return {'events':list(output),'read_errors':self.read_errors,'history_boundary':'Only retained legacy events and events after enablement are available.'}

    def export(self,destination):
        destination=Path(destination).resolve()
        if destination.exists():raise ValueError('support_destination_exists')
        if self.cfg.get('source_root') and destination.is_relative_to(Path(self.cfg['source_root']).resolve()):raise ValueError('support_export_inside_source')
        with self.locked():
            with os.fdopen(os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output, \
                    zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for kind in ('audit','runtime'):
                    for path in sorted((self.root/kind).iterdir()):
                        if not path.is_file() or path.is_symlink() or not SEGMENT.fullmatch(path.name):continue
                        with (gzip.open(path,'rt',encoding='utf-8') if path.suffix=='.gz' else path.open(encoding='utf-8')) as stream:
                            rows=[]
                            for line in stream:
                                try:
                                    row=json.loads(line)
                                    if not isinstance(row,dict):continue
                                    clean=self._row(row.get('event','unrecognized_event'),row,
                                        row.get('level','INFO'),timestamp=row.get('timestamp',self.clock()))
                                    if re.fullmatch(r'[a-f0-9]{32}',str(row.get('service_run_id',''))):clean['service_run_id']=row['service_run_id']
                                    if type(row.get('pid')) is int:clean['pid']=row['pid']
                                    if row.get('component') in ('sync_worker','qa_collector'):clean['component']=row['component']
                                    # Recheck the allowlist on export, too; never copy arbitrary extra fields.
                                    clean['event_id']=row.get('event_id',clean['event_id']) if re.fullmatch(r'[a-f0-9]{32,64}',str(row.get('event_id',''))) else clean['event_id']
                                    rows.append(json.dumps(clean,ensure_ascii=False)+'\n')
                                except (ValueError,TypeError,OverflowError):continue
                        name=path.name[:-3]+'.archived.jsonl' if path.suffix=='.gz' else path.name
                        archive.writestr(kind+'/'+name,''.join(rows))
                current=self.home/'failure-alerts/current.json'
                if current.is_file() and not current.is_symlink():
                    from failure_alerts import REASONS
                    issues=[]
                    for issue in json.loads(current.read_text()).get('issues',[]):
                        row=safe_fields(issue)
                        row['reason']=REASONS.get(row.get('code'),('','请按错误码检查服务及私有解析报告',''))[1]
                        row['needs_attention']=issue.get('needs_attention') is True
                        report=issue.get('report_path')
                        if isinstance(report,str) and Path(report).resolve().is_relative_to(self.home/'jobs'):
                            row['report_ref']=str(Path(report).resolve().relative_to(self.home))
                        issues.append(row)
                    archive.writestr('failures/current.json',json.dumps({'issues':issues},ensure_ascii=False,indent=2))
                archive.writestr('说明.md','仅含结构化元数据及当前告警摘要；不含原件、模型请求响应、密钥、state、问答正文或原始stdout/stderr。\n')
        destination.chmod(0o600)
        return destination
