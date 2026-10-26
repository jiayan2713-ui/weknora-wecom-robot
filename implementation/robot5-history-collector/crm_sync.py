"""Mingdao synchronization inside the collector; SQLite is the source, never XLSX.

Only completed, unambiguous QAs are published. Every successful write is read
back. API bodies, credentials and question/answer text never enter health logs.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

FIELD_NAMES = {'time':'提问时间','user':'提问人','question':'问题','answer':'答案（原始格式）',
               'reasoning':'推理过程','source':'来源记录ID'}
HUMAN_FIELDS = ['答案评分','评测备注','评测时间','评测人']
CONFIG_RELATIVE = Path('private/crm-config.json')


class SyncError(RuntimeError):
    def __init__(self, code, retryable=False):
        self.code=str(code)
        self.retryable=retryable
        super().__init__(self.code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


def read_config(root):
    path=Path(root)/CONFIG_RELATIVE
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise SyncError('private_config_permissions_must_be_0600')
    config=json.loads(path.read_text())
    if config.get('base_url')!='https://api.mingdao.com':
        raise SyncError('unapproved_crm_endpoint')
    for key in ['app_key','app_sign','worksheet_id']:
        if not config.get(key):
            raise SyncError('missing_config_'+key)
    return config


class Client:
    def __init__(self,config):
        self.config=config
        self.opener=urllib.request.build_opener(NoRedirect())
        self.timeout=min(float(config.get('timeout_seconds',10)),20)
        self.last_request=0
        self.deadline=None
        self.aliases={}

    def request(self,path,body=None,version=2,method=None):
        wait=float(self.config.get('request_interval_seconds',0.25))-(time.monotonic()-self.last_request)
        if wait>0:time.sleep(wait)
        remaining=self.deadline-time.monotonic() if self.deadline is not None else self.timeout
        if remaining<=0:raise SyncError('run_budget_exhausted',True)
        headers={'Content-Type':'application/json'}
        if version==2:
            body={**(body or {}),'appKey':self.config['app_key'],'sign':self.config['app_sign'],
                  'worksheetId':self.config['worksheet_id']}
        else:
            headers.update({'HAP-Appkey':self.config['app_key'],'HAP-Sign':self.config['app_sign']})
        request=urllib.request.Request(self.config['base_url']+path,
            data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
            headers=headers,method=method)
        self.last_request=time.monotonic()
        try:
            with self.opener.open(request,timeout=min(self.timeout,remaining)) as response:
                result=json.load(response)
        except urllib.error.HTTPError as exc:
            raise SyncError('http_'+str(exc.code),exc.code in [408,429] or exc.code>=500) from None
        except (urllib.error.URLError,TimeoutError,OSError):
            raise SyncError('transport_outcome_unknown',True) from None
        except (ValueError,TypeError):
            raise SyncError('invalid_api_response',True) from None
        if not isinstance(result,dict) or result.get('success') is not True:
            code=result.get('error_code',result.get('code','unknown')) if isinstance(result,dict) else 'unknown'
            code=str(code) if str(code).isdigit() and len(str(code))<=8 else 'unknown'
            raise SyncError('api_'+str(code),str(code) in ['51','90000','99999'])
        return result.get('data')

    def schema(self):
        schema=self.request('/v2/open/worksheet/getWorksheetInfo',{}) or {}
        self.aliases={field['alias']:field['controlId'] for field in schema.get('controls',[]) if field.get('alias')}
        return schema

    def list_access(self):
        schema=self.schema()
        views=schema.get('views') or []
        self.request('/v2/open/worksheet/getFilterRows',{
            'viewId':self.config.get('view_id') or (views[0]['viewId'] if views else ''),
            'pageIndex':1,'pageSize':1,'listType':1})
        return schema

    def prepare_schema(self):
        """Called only by the explicit, user-approved schema preparation command."""
        schema=self.schema()
        fields={field['controlName']:field for field in schema.get('controls',[])}
        edits=[]
        stamp=fields.get('提问时间')
        if not stamp:raise SyncError('missing_question_time_field')
        if stamp['type']==46:
            edits.append({'id':stamp['controlId'],'name':'提问时间','type':16})
        elif stamp['type']!=16:
            raise SyncError('unsupported_question_time_conversion')
        additions=[]
        if '来源记录ID' not in fields:
            additions.append({'name':'来源记录ID','alias':'robot5_source_id','type':2,
                              'required':False,'isHidden':True,'isUnique':True})
        if edits or additions:
            self.request('/v3/app/worksheets/'+self.config['worksheet_id'],
                         {'editFields':edits,'addFields':additions},version=3)
        return self.schema()

    def find(self,field,source_key):
        data=self.request('/v2/open/worksheet/getFilterRows',{
            'pageIndex':1,'pageSize':2,'listType':1,'viewId':self.config.get('view_id',''),
            'filters':[{'controlId':field,'dataType':2,'spliceType':1,'filterType':2,'value':source_key}]})
        rows=data.get('rows',[]) if isinstance(data,dict) else data or []
        if not isinstance(rows,list):raise SyncError('invalid_list_response')
        matches=[]
        for row in rows:
            normalized=normalize_row(row,self.aliases)
            if normalized.get(field)!=source_key:
                raise SyncError('source_filter_not_exact')
            row_id=row.get('rowid') or row.get('rowId') or row.get('id')
            if not row_id:raise SyncError('row_identity_missing')
            matches.append((row_id,normalized))
        if len(matches)>1:raise SyncError('duplicate_remote_source_id')
        return matches[0] if matches else None

    def get(self,row_id):
        data=self.request('/v2/open/worksheet/getRowByIdPost',{'rowId':row_id})
        return normalize_row(data,self.aliases)

    def create(self,values):
        data=self.request('/v2/open/worksheet/addRow',{
            'controls':[{'controlId':key,'value':value} for key,value in values.items()],
            'triggerWorkflow':False})
        row_id=data if isinstance(data,str) else (data or {}).get('rowId') or (data or {}).get('id')
        if not row_id:raise SyncError('create_response_missing_row_id',True)
        return row_id

    def update(self,row_id,values):
        self.request('/v2/open/worksheet/editRow',{
            'rowId':row_id,'controls':[{'controlId':key,'value':value} for key,value in values.items()],
            'triggerWorkflow':False})


def normalize_row(data,aliases=None):
    if not isinstance(data,dict):raise SyncError('invalid_row_response')
    if isinstance(data.get('fields'),list):
        data={field['id']:field.get('value') for field in data['fields']}
    result=dict(data)
    for alias,field_id in (aliases or {}).items():
        if alias in data and field_id not in result:
            result[field_id]=data[alias]
    return result


def unreviewed_option(field):
    for option in field.get('options',[]):
        if not option.get('isDeleted') and option.get('value') in ['未评分','未评测','待评分','待评测']:
            return option
    return None


def field_map(schema):
    fields={field.get('controlName'):field for field in schema.get('controls') or []}
    result={}
    for key,name in FIELD_NAMES.items():
        field=fields.get(name)
        if not field:raise SyncError('missing_field_'+key)
        if key=='time' and field.get('type')!=16:
            raise SyncError('question_time_requires_datetime_field')
        if key!='time' and field.get('type')!=2:
            raise SyncError('text_field_required_'+key)
        result[key]=field['controlId']
    score_field=fields.get('答案评分',{})
    unreviewed=unreviewed_option(score_field)
    result['human']=[fields[name]['controlId'] for name in HUMAN_FIELDS if name in fields]
    result['score']=fields.get('答案评分',{}).get('controlId')
    result['unreviewed_values']=[unreviewed['key'],unreviewed['value']] if unreviewed else []
    result['create_defaults']={result['score']:unreviewed['value']} if unreviewed else {}
    return result


def is_reviewed(value,fields):
    if value is None or value=='' or value==[]:return False
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:pass
    if value==[]:return False
    values=value if isinstance(value,list) else [value]
    return any(item not in fields.get('unreviewed_values',[]) for item in values)


def payload(record,fields):
    if record.get('status')!='已完成' or not record.get('assistant_message_id') or not record.get('user_message_id'):
        return None
    source='robot5:'+record['channel_id']+':'+record['assistant_message_id']
    stamp=dt.datetime.fromisoformat(record['question_at'].replace('Z','+00:00')).astimezone(
        dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    return {fields['time']:stamp,fields['user']:record.get('user_id') or '',
            fields['question']:record.get('question') or '',fields['answer']:record.get('answer') or '',
            fields['reasoning']:record.get('reasoning_process') or '',fields['source']:source}


def init(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS crm_records(
        target TEXT NOT NULL,source_key TEXT NOT NULL,row_id TEXT,
        desired_hash TEXT NOT NULL,synced_hash TEXT,status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,
        error_code TEXT,verified_at TEXT,PRIMARY KEY(target,source_key));
    ''')
    if 'readback_note' not in {row[1] for row in db.execute('PRAGMA table_info(crm_records)')}:
        db.execute('ALTER TABLE crm_records ADD COLUMN readback_note TEXT')
        db.commit()


def field_matches(key,value,actual,fields=None):
    remote=actual.get(key) or ''
    if remote==value:return True
    # Both Mingdao V2 and V3 remove trailing ASCII spaces from answer text.
    # Keep the archive unchanged and record this difference on successful readback.
    return bool(fields and key==fields['answer'] and isinstance(value,str)
                and remote==value.rstrip(' '))


def verify(values,actual,fields=None):
    notes=[]
    for key,value in values.items():
        if not field_matches(key,value,actual,fields):
            raise SyncError('remote_content_mismatch')
        if (actual.get(key) or '')!=value:
            notes.append({'field':'answer','normalization':'trailing_ascii_spaces_removed_by_crm',
                          'removed_characters':len(value)-len(actual.get(key) or ''),
                          'local_hash':digest(value),'remote_hash':digest(actual.get(key) or '')})
    return notes


def run(db,records,config,client=None,limit=None,budget=None):
    init(db)
    target=config['base_url']+'/'+config['worksheet_id']
    client=client or Client(config)
    started=time.monotonic()
    deadline=float(budget if budget is not None else config.get('budget_seconds',20))
    client.deadline=started+deadline
    schema=client.list_access()
    fields=field_map(schema)
    web_field=next((f['controlId'] for f in schema.get('controls',[]) if f.get('controlName')=='答案（网页格式）' and f.get('type')==41),None)
    planned={}
    question_times={}
    with db:
        for record in records:
            values=payload(record,fields)
            if values is None:continue
            key=values[fields['source']]
            planned[key]=values
            question_times[key]=dt.datetime.fromisoformat(
                record['question_at'].replace('Z','+00:00')).timestamp()
            wanted=digest(values)
            previous=db.execute('SELECT * FROM crm_records WHERE target=? AND source_key=?',(target,key)).fetchone()
            if previous and previous['desired_hash']==wanted:continue
            db.execute('''INSERT INTO crm_records(target,source_key,desired_hash,status) VALUES(?,?,?,'pending')
                ON CONFLICT(target,source_key) DO UPDATE SET desired_hash=excluded.desired_hash,
                status='pending',attempts=0,next_retry=0,error_code=NULL''',(target,key,wanted))
    planning_seconds=time.monotonic()-started
    started=time.monotonic()
    client.deadline=started+deadline
    maximum=int(limit if limit is not None else config.get('max_records_per_run',10))
    done={'created':0,'created_with_web':0,'updated':0,'recovered':0,'verified':0,'attempted':0}
    ready=db.execute("SELECT * FROM crm_records WHERE target=? AND status!='synced' AND next_retry<=?",
                     (target,time.time())).fetchall()
    # New/changed records are sent oldest-question-first. Due retries follow
    # in a separate phase so an old failing record cannot head every new batch.
    # Compare actual instants, not UUIDs or differently-offset timestamp strings.
    queue=sorted((item for item in ready if item['source_key'] in planned),
                 key=lambda item:(item['status']!='pending',question_times[item['source_key']],item['source_key']))
    for item in queue:
        if done['attempted']>=maximum or time.monotonic()-started>=deadline:break
        if item['source_key'] not in planned:continue
        done['attempted']+=1
        values=planned[item['source_key']]
        row_id=item['row_id']
        try:
            if not row_id:
                found=client.find(fields['source'],item['source_key'])
                if found:
                    row_id,actual=found
                    done['recovered']+=1
                else:
                    db.execute("UPDATE crm_records SET status='sending' WHERE target=? AND source_key=?",(target,item['source_key']))
                    db.commit()
                    web_values={}
                    if web_field and values[fields['answer']].strip():
                        from answer_format import PresentationError,render_answer
                        try:web_values={web_field:render_answer(values[fields['answer']])}
                        except PresentationError:pass  # The independent web queue records the specific error.
                    row_id=client.create({**fields.get('create_defaults',{}),**values,**web_values})
                    done['created']+=1
                    done['created_with_web']+=int(bool(web_values))
                db.execute('UPDATE crm_records SET row_id=? WHERE target=? AND source_key=?',(row_id,target,item['source_key']))
                db.commit()
            actual=client.get(row_id)
            if actual.get(fields['source'])!=item['source_key']:
                raise SyncError('remote_row_identity_mismatch')
            if any(not field_matches(key,value,actual,fields) for key,value in values.items()):
                score=actual.get(fields.get('score'))
                if is_reviewed(score,fields):
                    raise SyncError('reviewed_answer_changed_requires_review')
                client.update(row_id,values)
                done['updated']+=1
                actual=client.get(row_id)
            notes=verify(values,actual,fields)
            db.execute("UPDATE crm_records SET status='synced',synced_hash=desired_hash,attempts=0,next_retry=0,error_code=NULL,readback_note=?,verified_at=? WHERE target=? AND source_key=?",
                       (canonical(notes),dt.datetime.now(dt.timezone.utc).isoformat(),target,item['source_key']))
            db.commit()
            done['verified']+=1
        except SyncError as exc:
            count=item['attempts']+1
            delay=min(3600,60*(2**min(count-1,6)))
            status='retry' if exc.retryable else 'blocked'
            db.execute('UPDATE crm_records SET status=?,attempts=?,next_retry=?,error_code=? WHERE target=? AND source_key=?',
                       (status,count,time.time()+delay if exc.retryable else time.time()+3600,exc.code,target,item['source_key']))
            db.commit()
            if exc.retryable or exc.code.startswith(('api_10005','api_101','http_401','http_403')):break
    counts={}
    for row in db.execute('SELECT source_key,status FROM crm_records WHERE target=?',(target,)):
        if row['source_key'] in planned:
            counts[row['status']]=counts.get(row['status'],0)+1
    return {'status':'success' if counts.get('synced',0)==len(planned) else 'pending',
            'eligible':len(planned),'counts':counts,'planning_seconds':round(planning_seconds,3),**done}


def tick(db,records,root,force=False,limit=None,budget=None,logs=None):
    try:
        config=read_config(root)
        if config is None:return {'status':'not_configured'}
        if not config.get('enabled') and not force:
            result={'status':'disabled'}
        else:
            result=run(db,records,config,limit=limit,budget=budget)
            # Presentation has its own queue so HTML backfill never rewrites
            # original answers or interferes with reviewed-answer protection.
            try:
                import crm_web
                result['web_format']=crm_web.run(db,records,config,limit=limit,budget=budget)
            except SyncError as exc:
                db.rollback()
                result['web_format']={'status':'blocked','error_code':exc.code}
            except Exception as exc:
                db.rollback()
                result['web_format']={'status':'failed','error_code':'internal_'+type(exc).__name__}
    except SyncError as exc:
        db.rollback()
        result={'status':'blocked','error_code':exc.code}
    except Exception as exc:
        db.rollback()
        result={'status':'failed','error_code':'internal_'+type(exc).__name__}
    atomic_json(Path(root)/'crm-health.json',{'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),**result})
    if logs:
        failed=result['status'] in ['failed','blocked'] or result.get('web_format',{}).get('status') in ['failed','blocked']
        logs.record('crm_sync_checked',result,level='ERROR' if failed else 'INFO')
    return result
