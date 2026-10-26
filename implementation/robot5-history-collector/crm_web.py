"""Independent, restart-safe presentation sync; never writes the source columns."""
import datetime as dt
import time

import crm_sync as crm
from answer_format import FORMAT_VERSION,PresentationError,render_answer

FIELD_NAME='答案（网页格式）'


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS crm_web_records(
        target TEXT NOT NULL,source_key TEXT NOT NULL,row_id TEXT NOT NULL,
        desired_hash TEXT NOT NULL,synced_hash TEXT,status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,
        error_code TEXT,verified_at TEXT,PRIMARY KEY(target,source_key))''')
    db.commit()
    columns={r[1] for r in db.execute('PRAGMA table_info(crm_web_records)')}
    for name,kind in [('source_hash','TEXT'),('rendered_html','TEXT'),('format_version','INTEGER')]:
        if name not in columns:db.execute('ALTER TABLE crm_web_records ADD COLUMN '+name+' '+kind)
    db.commit()


def plan(db,records,config,client):
    init(db)
    schema=client.list_access()
    fields=crm.field_map(schema)
    field=next((f for f in schema.get('controls',[]) if f.get('controlName')==FIELD_NAME),None)
    if not field:raise crm.SyncError('missing_web_answer_field')
    if field['type']!=41:raise crm.SyncError('web_answer_requires_rich_text')
    fields['web']=field['controlId']
    target=config['base_url']+'/'+config['worksheet_id']
    items={}
    with db:
        for record in records:
            values=crm.payload(record,fields)
            if values is None:continue
            key=values[fields['source']]
            raw=db.execute("SELECT row_id FROM crm_records WHERE target=? AND source_key=? AND status='synced'",(target,key)).fetchone()
            if not raw or not raw['row_id']:continue
            source_hash=crm.digest(record.get('answer') or '')
            cached=db.execute('SELECT source_hash,rendered_html,format_version FROM crm_web_records WHERE target=? AND source_key=?',(target,key)).fetchone()
            error=None
            try:
                if not (record.get('answer') or '').strip():raise PresentationError('source_answer_empty')
                if cached and cached['source_hash']==source_hash and cached['format_version']==FORMAT_VERSION and cached['rendered_html'] is not None:
                    html=cached['rendered_html']
                else:html=render_answer(record['answer'])
            except PresentationError as exc:
                html=None;error='presentation_'+str(exc)
            wanted=crm.digest({fields['web']:html}) if html is not None else crm.digest({'version':FORMAT_VERSION,'source':record.get('answer')})
            items[key]={'html':html,'values':values,'question_at':record['question_at'],'error_code':error}
            db.execute('''INSERT INTO crm_web_records(target,source_key,row_id,desired_hash,status,source_hash,rendered_html,format_version)
                VALUES(?,?,?,?,'pending',?,?,?) ON CONFLICT(target,source_key) DO UPDATE SET
                row_id=excluded.row_id,desired_hash=excluded.desired_hash,
                status=CASE WHEN desired_hash!=excluded.desired_hash AND status!='preserved' THEN 'pending' ELSE status END,
                next_retry=CASE WHEN desired_hash!=excluded.desired_hash THEN 0 ELSE next_retry END,
                source_hash=excluded.source_hash,rendered_html=excluded.rendered_html,format_version=excluded.format_version
                WHERE desired_hash!=excluded.desired_hash OR row_id!=excluded.row_id
                OR source_hash IS NOT excluded.source_hash OR format_version IS NOT excluded.format_version''',
                (target,key,raw['row_id'],wanted,source_hash,html,FORMAT_VERSION))
            if error:
                db.execute("UPDATE crm_web_records SET status='blocked',error_code=?,next_retry=? WHERE target=? AND source_key=?",
                           (error,time.time()+3600,target,key))
    return target,fields,items


def process(item,planned,fields,client):
    """Read before writing and send only the presentation field, including for graded rows."""
    if planned.get('error_code'):raise crm.SyncError(planned['error_code'])
    actual=client.get(item['row_id'])
    if actual.get(fields['source'])!=item['source_key']:
        raise crm.SyncError('remote_row_identity_mismatch')
    # A stale local archive must never format a different answer being reviewed.
    crm.verify(planned['values'],actual,fields)
    current=actual.get(fields['web']) or ''
    wanted={fields['web']:planned['html']}
    if current==planned['html']:
        return 'synced',False,None
    if current and crm.digest({fields['web']:current})!=item['synced_hash']:
        return 'preserved',False,'existing_or_manually_changed_web_answer'
    # Include every source field and current human field in the readback guard.
    protected={k:v for k,v in actual.items() if k not in ['utime','uaid',fields['web']]}
    client.update(item['row_id'],wanted)
    after=client.get(item['row_id'])
    crm.verify(wanted,after)
    if any(after.get(k)!=value for k,value in protected.items()):
        raise crm.SyncError('protected_field_changed_during_web_sync')
    return 'synced',True,None


def save_result(db,target,item,result=None,error=None):
    if error:
        count=item['attempts']+1
        delay=min(3600,60*2**min(count-1,6)) if error.retryable else 3600
        db.execute('''UPDATE crm_web_records SET status=?,attempts=?,next_retry=?,error_code=?
            WHERE target=? AND source_key=?''',
            ('retry' if error.retryable else 'blocked',count,time.time()+delay,error.code,target,item['source_key']))
    else:
        status,updated,note=result
        db.execute('''UPDATE crm_web_records SET status=?,synced_hash=?,attempts=0,next_retry=0,
            error_code=?,verified_at=? WHERE target=? AND source_key=?''',
            (status,item['desired_hash'] if status=='synced' else item['synced_hash'],note,
             dt.datetime.now(dt.timezone.utc).isoformat(),target,item['source_key']))
    db.commit()


def run(db,records,config,limit=None,budget=None):
    client=crm.Client(config)
    started=time.monotonic()
    target,fields,planned=plan(db,records,config,client)
    planning_seconds=time.monotonic()-started
    # Setup/rendering must not starve the upload loop. Stable HTML is persisted
    # so a fresh launchd process only renders new or changed answers.
    client.deadline=time.monotonic()+float(budget if budget is not None else config.get('web_budget_seconds',20))
    queue=[r for r in db.execute("SELECT * FROM crm_web_records WHERE target=? AND status NOT IN ('synced','preserved') AND next_retry<=?",(target,time.time())) if r['source_key'] in planned and not planned[r['source_key']].get('error_code')]
    queue.sort(key=lambda r:(r['status']!='pending',dt.datetime.fromisoformat(planned[r['source_key']]['question_at'].replace('Z','+00:00')).timestamp(),r['source_key']))
    done={'attempted':0,'updated':0,'verified':0,'preserved':0}
    for item in queue:
        if done['attempted']>=int(limit if limit is not None else config.get('max_records_per_run',10)) or time.monotonic()>=client.deadline:break
        done['attempted']+=1
        try:
            result=process(item,planned[item['source_key']],fields,client)
            save_result(db,target,item,result=result)
            done['updated']+=int(result[1]);done['verified']+=int(result[0]=='synced');done['preserved']+=int(result[0]=='preserved')
        except crm.SyncError as exc:
            save_result(db,target,item,error=exc)
            if exc.retryable:break
    counts={}
    for row in db.execute('SELECT source_key,status FROM crm_web_records WHERE target=?',(target,)):
        if row['source_key'] in planned:counts[row['status']]=counts.get(row['status'],0)+1
    status='success' if counts.get('synced',0)==len(planned) else 'pending'
    if counts.get('blocked'):status='blocked'
    elif counts.get('preserved') and counts.get('synced',0)+counts['preserved']==len(planned):status='needs_review'
    return {'status':status,'format_version':FORMAT_VERSION,'planning_seconds':round(planning_seconds,3),'ready_before_run':len(queue),
            'eligible':len(planned),'counts':counts,**done}
