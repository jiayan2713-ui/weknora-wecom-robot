"""Real Compose/PostgreSQL isolation test using only an archive copy and fixture database."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import collector as c
from migration import restore


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError('隔离验收子步骤失败（数据库诊断未输出）')
    return result.stdout


def verify(archive, root):
    if root.exists():
        raise ValueError('验收目录已存在，拒绝覆盖')
    root.mkdir(parents=True, mode=0o700)
    scope = json.loads((archive/'snapshot.json').read_text())['scope']
    with sqlite3.connect(archive/'history.sqlite3') as db:
        rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM messages ORDER BY created_at,id')]
    if not rows:
        raise ValueError('需使用真实历史档案副本验收')
    cfg = {'name':'robot5-history-test-'+uuid.uuid4().hex[:10], 'services':{'postgres':{
        'image':'paradedb/paradedb:v0.22.6-pg17', 'pull_policy':'never',
        'network_mode':'none','environment':{'POSTGRES_USER':'fixture','POSTGRES_DB':'fixture',
                                            'POSTGRES_HOST_AUTH_METHOD':'trust'},
        'tmpfs':['/var/lib/postgresql/data']}}}
    compose = root/'compose.json'
    c.atomic_json(compose, cfg)
    base = ['docker','compose','-f',str(compose)]
    def sql(text):
        return run([*base,'exec','-T','postgres','psql','-X','-q','-U','fixture','-d','fixture',
                    '-At','-v','ON_ERROR_STOP=1'],input=text.encode())
    try:
        run([*base,'up','-d'])
        deadline = time.monotonic()+60
        # pg_isready alone can see the entrypoint's temporary initialization server.
        ready = [*base,'exec','-T','postgres','sh','-c',
                 'test "$(cat /proc/1/comm)" = postgres && pg_isready -U fixture -d fixture']
        while subprocess.run(ready,capture_output=True).returncode:
            if time.monotonic()>deadline:
                raise RuntimeError('隔离PostgreSQL未就绪')
            time.sleep(1)
        ddl = '''CREATE TABLE im_channels(id uuid,tenant_id bigint,agent_id text,platform text);
CREATE TABLE sessions(id text,tenant_id bigint,title text,deleted_at timestamptz);
CREATE TABLE im_channel_sessions(id text,session_id text,im_channel_id uuid,tenant_id bigint,user_id text,
chat_id text,platform text,status text,deleted_at timestamptz,updated_at timestamptz);
CREATE TABLE messages(id uuid,session_id text,request_id text,role text,content text,is_completed boolean,
created_at timestamptz,updated_at timestamptz,deleted_at timestamptz,channel text,agent_id text,model_id text,
knowledge_references jsonb,usage jsonb,images jsonb,attachments jsonb);
'''
        sql(ddl)
        insert = lambda table, value: sql(f"INSERT INTO {table} SELECT * FROM jsonb_populate_record(NULL::{table}, {c.literal(c.canonical(value))}::jsonb)")
        insert('im_channels',{**scope,'id':scope['channel_id'],'platform':'wecom'})
        sessions={row['session_id']:row for row in rows}
        for session,row in sessions.items():
            insert('sessions',dict(id=session,tenant_id=scope['tenant_id'],title=row.get('session_title'),deleted_at=row.get('session_deleted_at')))
            insert('im_channel_sessions',dict(id=str(uuid.uuid4()),session_id=session,im_channel_id=scope['channel_id'],
                   tenant_id=scope['tenant_id'],user_id=row.get('im_user_id'),chat_id=row.get('im_chat_id'),platform='wecom',
                   status=row.get('mapping_status'),deleted_at=row.get('mapping_deleted_at'),updated_at=c.now()))
        # Batch load copied rows; SQL literal quoting never sends raw contents through a shell.
        sql('INSERT INTO messages SELECT * FROM jsonb_populate_recordset(NULL::messages,'+c.literal(c.canonical(rows))+'::jsonb)')
        restored=root/'archive'
        restore(archive,restored,compose)
        binding=json.loads((restored/'binding.json').read_text())
        db=c.connect(restored)
        try:
            first=c.sync(db,restored,binding)
            if first['inserted']!=0 or first['messages']!=len(rows):
                raise RuntimeError('迁移首次核对产生重复或缺失')
            request=str(uuid.uuid4()); user=str(uuid.uuid4()); answer=str(uuid.uuid4())
            fresh={**rows[0],'session_id':next(iter(sessions)),'request_id':request,'created_at':c.now(),
                   'updated_at':c.now(),'deleted_at':None,'channel':'im','agent_id':scope['agent_id']}
            insert('messages',{**fresh,'id':user,'role':'user','content':'隔离验收新增问题 =1+1','is_completed':True})
            insert('messages',{**fresh,'id':answer,'role':'assistant','content':'生成中','is_completed':False})
            added=c.sync(db,restored,binding)
            if added['inserted']!=2:
                raise RuntimeError('新问答未增量采集')
            sql(f"UPDATE messages SET content='隔离验收完整回答',is_completed=true,updated_at=now() WHERE id={c.literal(answer)}")
            completed=c.sync(db,restored,binding)
            repeated=c.sync(db,restored,binding)
            qa=next(row for row in c.qa_records(db) if row['record_id']==answer)
            if completed['updated']<1 or qa['answer']!='隔离验收完整回答' or qa['status']!='已完成' or repeated['inserted'] or repeated['updated']:
                raise RuntimeError('回答补齐或重复采集验收失败')
            validation=c.verify_excel(restored/'机器人5问答.xlsx',c.qa_records(db))
            report={'status':'passed','database':'isolated_compose_postgres','network':'none',
                    'production_database_modified':False,'archive_messages':len(rows),
                    'initial_reconcile_inserted':first['inserted'],'new_messages_inserted':added['inserted'],
                    'completion_updated':True,'repeat_inserted':repeated['inserted'],'repeat_updated':repeated['updated'],
                    'excel_validation':validation,'intel_mac_execution':'not_run'}
            c.atomic_json(root/'验收结果.json',report)
            print(json.dumps(report,ensure_ascii=False))
        finally:
            db.close()
    finally:
        run([*base,'down','--volumes'])


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077)
    verify(args.archive.resolve(),args.root.resolve())
