"""Original-file admission and activation using the existing worker's API, locks and retirement queue."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from reconcile import hashes, save
from fill_missing import multipart
from table_native import inventory, coverage

VERSION='original-table-v1'
FORMATS={'.csv','.xlsx','.xls'}


def wants_original(cfg,relative):
    values=cfg.get('parser_skip_extensions',[])
    if not isinstance(values,list) or any(not isinstance(v,str) or v.lower() not in FORMATS for v in values):
        raise ValueError('parser_skip_extensions_must_be_supported_table_extensions')
    return Path(relative).suffix.lower() in {v.lower() for v in values}


def platform_preflight(cfg,path):
    engine='markitdown' if path.suffix.lower()=='.csv' else 'builtin'
    payload={'name':path.name,'type':path.suffix.lower().lstrip('.'),'engine':engine,'content':base64.b64encode(path.read_bytes()).decode()}
    script='''import sys,json,base64,os,grpc
from docreader.proto import docreader_pb2 as p,docreader_pb2_grpc as g
v=json.load(sys.stdin);channel=grpc.insecure_channel("localhost:50051",options=[("grpc.max_receive_message_length",64*1024*1024),("grpc.max_send_message_length",64*1024*1024)])
grpc.channel_ready_future(channel).result(timeout=20)
token=os.environ.get("GRPC_AUTH_TOKEN");metadata=[("authorization","Bearer "+token)] if token else []
r=g.DocReaderStub(channel).Read(p.ReadRequest(file_content=base64.b64decode(v["content"]),file_name=v["name"],file_type=v["type"],config=p.ReadConfig(parser_engine=v["engine"])),metadata=metadata,timeout=180)
print(json.dumps({"text":r.markdown_content,"error":r.error},ensure_ascii=False))
'''
    candidates=[cfg.get('table_docker_binary'),shutil.which('docker'),'/Applications/Docker.app/Contents/Resources/bin/docker','/usr/local/bin/docker','/opt/homebrew/bin/docker']
    docker=next((p for p in candidates if p and Path(p).is_file() and os.access(p,os.X_OK)),None)
    if not docker:raise ValueError('table_docker_cli_unavailable')
    result=subprocess.run([docker,'exec','-i',cfg.get('table_docreader_container','WeKnora-docreader'),'python','-c',script],input=json.dumps(payload).encode(),capture_output=True,timeout=210)
    if result.returncode:raise ValueError('table_platform_preflight_unavailable')
    response=json.loads(result.stdout)
    if response.get('error'):raise ValueError('table_platform_preflight_failed')
    return response.get('text','')


def chunk_text(worker,kid):
    parts=[];positioned=[];page=1
    while True:
        code,res=worker.api.request('/chunks/'+kid+'?page='+str(page)+'&page_size=100')
        if code!=200:raise ValueError('table_chunk_read_failed')
        batch=res.get('data') or []
        for c in batch:
            if not c.get('is_enabled') or c.get('chunk_type') not in ('text','table'):continue
            content=c.get('content','');start=c.get('start_at');end=c.get('end_at')
            if isinstance(start,int) and isinstance(end,int) and start>=0 and end-start==len(content) and end<=64*1024*1024:
                positioned.append((start,end,content))
            else:parts.append(content)
        total=res.get('total')
        if len(batch)<100 or isinstance(total,int) and page*100>=total:break
        page+=1
        if page>1000:raise ValueError('table_chunk_read_capacity')
    # Rebuild overlapping text spans before matching whole source cells.
    # Offsets refer to source characters; generated summaries are excluded above.
    text='';last_end=0
    for start,end,content in sorted(positioned,key=lambda row:(row[0],row[1])):
        overlap=last_end-start
        if overlap>0:
            compared=min(overlap,len(content))
            offset=start-(last_end-len(text))
            if offset<0 or text[offset:offset+compared]!=content[:compared]:
                raise ValueError('table_indexed_span_conflict')
            if end<=last_end:continue
            content=content[overlap:]
        elif start>last_end:
            text+='\n'*(start-last_end)
        text+=content;last_end=end
    return text+'\n'+'\n'.join(parts)


def block(worker,identity,record,digest,reason,report,pending=None):
    record.update(state='original_ingest_hold',direct_blocked_hash=digest,
        direct_policy=worker.cfg.get('direct_ingest_policy_revision',VERSION),
        direct_failure={'code':'ORIGINAL_TABLE_INCOMPLETE','category':'quality','retryable':False,'reason':reason,'report_path':str(report)})
    failed_ids=list(record.pop('direct_superseded_ids',[])) + (pending.get('remote_ids',[]) if pending else [])
    if failed_ids:
        # Only retire new owned attempts; existing shared documents remain untouched.
        for kid in set(failed_ids):
            if kid in worker.state['owned_ids'] and kid not in record.get('remote_ids',[]):
                worker.state['retire'].append({'id':kid,'reason':'direct_validation_failed','source_id':identity})
        save(report.parent/'failed-upload.json',pending or {'superseded_ids':failed_ids})
        record.pop('pending',None)
    worker.event('direct_ingest_blocked',source_id=identity,code='ORIGINAL_TABLE_INCOMPLETE',reason=reason)
    worker.persist();worker.report_failures({identity:record})


def cleanup_legacy(worker,identity,record,legacy):
    old_path,old_sha=legacy.get('md_path'),legacy.get('md_sha')
    worker.archive_md(old_path,old_sha,identity)
    cached=legacy.get('cached_md')
    if cached and not any(r.get('cached_md')==cached for i,r in worker.state['records'].items() if i!=identity and r['state']!='deleted'):
        p=Path(cached)
        if p.is_file() and p.resolve().is_relative_to(worker.home/'artifacts') and hashes(p)['sha256']==old_sha:
            p.unlink()
    job_report=legacy.get('latest_parse_report_path')
    if job_report:
        job=Path(job_report).parent
        if job.is_dir() and job.resolve().parent == (worker.home/'jobs').resolve():
            shared=False
            for other_id,other in worker.state['records'].items():
                if other_id==identity or other['state']=='deleted':continue
                active_fields={k:v for k,v in other.items() if k not in ('direct_migration',)}
                if str(job)+'/' in json.dumps(active_fields,ensure_ascii=False) or (other.get('parse_intent') or {}).get('job_id')==job.name:
                    shared=True;break
            if not shared:
                target=worker.home/'history/direct-migration'/identity/'jobs'/job.name
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():shutil.move(str(job),str(target))
                record['direct_migration']['archived_job']=str(target)
    record['legacy_cleanup_complete']=not old_path or not Path(old_path).exists()
    worker.persist()


def process(worker,identity,record,relative,digest,docs):
    revision=worker.cfg.get('direct_ingest_policy_revision',VERSION)
    if record.get('direct_blocked_hash')==digest and record.get('direct_policy')==revision:
        record['state']='original_ingest_hold';return
    pending=record.get('pending')
    superseded=list(record.get('direct_superseded_ids',[]))
    if pending and pending.get('ingest_mode')!='original':
        if pending['phase'] in ('upload_intent','uncertain') and not pending.get('remote_ids'):
            worker.recover_upload(pending,docs)
            if not pending.get('remote_ids'):record['state']='upload_uncertain_hold';return
        superseded+=pending.get('remote_ids',[])+pending.get('superseded_ids',[]);record['direct_superseded_ids']=superseded;record.pop('pending');pending=None
    if pending and (pending['source_hash']!=digest or pending['path']!=relative):
        if pending['phase'] in ('upload_intent','uncertain') and not pending.get('remote_ids'):
            worker.recover_upload(pending,docs)
            if not pending.get('remote_ids'):record['state']='upload_uncertain_hold';return
        superseded+=pending.get('remote_ids',[])+pending.get('superseded_ids',[]);record['direct_superseded_ids']=superseded;record.pop('pending');pending=None
    if not pending:
        key=hashlib.sha256((identity+relative+digest+':original:'+revision).encode()).hexdigest();work=worker.home/'jobs'/key;work.mkdir(parents=True,exist_ok=True)
        path=work/Path(relative).name
        if not path.exists():shutil.copy2(worker.source_path(relative),path)
        meta=hashes(path)
        if meta['sha256']!=digest:raise ValueError('table_snapshot_changed')
        report=work/'direct-validation.json'
        try:native=inventory(path)
        except Exception as error:
            save(report,{'source_hash':digest,'error_type':type(error).__name__,'status':'native_inventory_failed','human_review':False})
            block(worker,identity,record,digest,'native_inventory_failed',report);return
        text=platform_preflight(worker.cfg,path);check=coverage(native,text)
        save(report,{'source_hash':digest,'ingest_mode':'original','preflight':check,'human_review':False})
        if not check['passed']:
            block(worker,identity,record,digest,'platform_native_content_incomplete',report);return
        save(work/'native-inventory.json',native)
        pending={'ingest_mode':'original','file_type':path.suffix.lower().lstrip('.'),'path':relative,'source_hash':digest,
            'artifact':str(path),'artifact_sha':digest,'md5':meta['md5'],'size':meta['size'],'phase':'prepared','remote_ids':[],
            'superseded_ids':superseded,'validation':str(report),'native_inventory':str(work/'native-inventory.json'),
            'legacy':{k:record.get(k) for k in ('md_path','md_sha','cached_md','latest_parse_report_path','source_hash')},
            'provenance':{'method':'original_file_direct','source_hash':digest,'self_parser_used':False,'human_review':False}}
        record['pending']=pending;worker.persist();worker.event('original_prepared',source_id=identity)
    if hashes(Path(pending['artifact']))['sha256']!=digest or hashes(worker.source_path(relative))['sha256']!=digest:
        raise ValueError('table_source_changed_before_upload')
    if pending['phase'] in ('uncertain','upload_intent'):
        worker.recover_upload(pending,docs)
        if not pending['remote_ids']:record['state']='upload_uncertain_hold';return
    if pending['phase']=='prepared':
        hits=[d for d in docs if d.get('file_hash')==pending['md5'] and d.get('file_size')==pending['size'] and '.'+str(d.get('file_type','')).lower() in FORMATS]
        if hits:pending.update(remote_ids=[hits[0]['id']],phase='learning')
        else:
            pending['phase']='upload_intent';worker.persist()
            fields={'sync_scope':'project-consultation','sync_ingest_mode':'original','sync_source_relative':relative,'sync_source_sha256':digest,'sync_artifact_sha256':digest}
            fields['_process_config']={'parser_engine_rules':[{'file_types':[pending['file_type']],'engine':'markitdown' if pending['file_type']=='csv' else 'builtin','xlsx_first_row_as_header':False}]}
            body,headers=multipart(Path(pending['artifact']),worker.folder+'/'+relative,fields)
            try:code,res=worker.api.request('/knowledge-bases/'+worker.kb+'/knowledge/file',body,headers)
            except Exception:pending['phase']='uncertain';worker.persist();raise
            doc=res.get('data') or {}
            if code not in (200,201,409) or not doc.get('id'):
                pending['phase']='prepared' if code in (400,401,403,413,429) else 'uncertain';worker.persist();raise ValueError('upload_rejected_'+str(code))
            if doc.get('file_hash')!=pending['md5'] or doc.get('file_size')!=pending['size'] or '.'+str(doc.get('file_type','')).lower() not in FORMATS:
                pending['phase']='uncertain';worker.persist();raise ValueError('direct_upload_identity_mismatch')
            pending.update(remote_ids=[doc['id']],phase='learning')
            if code!=409:worker.state['owned_ids'].append(doc['id'])
            docs.append(doc);worker.persist();worker.event('original_upload_accepted',source_id=identity,http_status=code)
    kid=pending['remote_ids'][0];code,res=worker.api.request('/knowledge/'+kid);doc=res.get('data') or {}
    if code!=200:raise ValueError('remote_status_unavailable')
    if doc.get('parse_status')=='failed':block(worker,identity,record,digest,'platform_parse_failed',Path(pending['validation']),pending);return
    if doc.get('parse_status')!='completed' or doc.get('enable_status')!='enabled' or doc.get('pending_subtasks_count'):
        record['state']='learning';return
    raw_request=__import__('urllib.request',fromlist=['Request']).Request(worker.api.base+'/knowledge/'+kid+'/download',headers=worker.api.auth)
    with worker.api.opener.open(raw_request,timeout=90) as response:raw=response.read()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('direct_download_hash_mismatch')
    native=json.loads(Path(pending['native_inventory']).read_text());check=coverage(native,chunk_text(worker,kid))
    report=Path(pending['validation']);saved=json.loads(report.read_text());save(report,{**saved,'indexed':check,'download_sha256':digest})
    if not check['passed']:block(worker,identity,record,digest,'indexed_content_incomplete',report,pending);return
    if not worker.index_probe(kid):raise ValueError('retrieval_probe_failed')
    desired=(worker.folder+'/'+str(Path(relative).parent)).rstrip('/.')
    if (doc.get('folder_path') or '')!=desired:
        from worker import references
        if references(worker.state['records'],kid)==1 and kid in worker.state['owned_ids']:
            code,res=worker.api.request('/knowledge/folder',json.dumps({'kb_id':worker.kb,'knowledge_ids':[kid],'folder_path':desired}).encode(),{'Content-Type':'application/json'})
            if code!=200 or not res.get('success'):raise ValueError('directory_alignment_failed')
        else:record['shared_folder_alias']=True
    if record.get('ingest_mode')=='original' and record['path']!=relative and Path(record['path']).name!=Path(relative).name:
        from worker import references
        if references(worker.state['records'],kid)==1 and kid in worker.state['owned_ids']:
            title=Path(relative).name
            code,res=worker.api.request('/knowledge/'+kid,json.dumps({'title':title},ensure_ascii=False).encode(),{'Content-Type':'application/json'},method='PUT')
            if code!=200 or not res.get('success'):raise ValueError('original_title_update_failed')
    if hashes(worker.source_path(relative))['sha256']!=digest:raise ValueError('table_source_changed_after_indexing')
    previous=list(set(record.get('remote_ids',[])+pending.get('superseded_ids',[])));legacy=pending['legacy']
    worker.state['retire'] += [{'id':v,'reason':'original_direct_verified','source_id':identity} for v in previous if v not in pending['remote_ids']]
    record.update(path=relative,source_hash=digest,ingest_mode='original',artifact_sha256=digest,artifact_md5=pending['md5'],
        original_artifact=pending['artifact'],remote_ids=pending['remote_ids'],state='active',provenance=pending['provenance'],
        direct_report_path=pending['validation'],direct_policy=revision,keyword_retrieval_verified=True,no_text=False,attempts=0,next_attempt=0,
        direct_migration={'legacy':legacy,'previous_ids':previous,'verified':True})
    for key in ('pending','md_path','md_sha','cached_md','latest_parse_report_path','parse_failure','last_parse_failure','parse_intent','parse_recovery_checkpoint','parse_retry_requested','direct_failure','direct_blocked_hash','blocked_source_hash','blocked_reason','blocked_parser_signature','error','desired_path','direct_superseded_ids'):
        record.pop(key,None)
    worker.persist();cleanup_legacy(worker,identity,record,legacy);worker.event('original_version_activated',source_id=identity,remote_ids=record['remote_ids'],previous_knowledge_ids=previous)
