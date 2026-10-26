#!/usr/bin/env python3
"""Read-only WeKnora history -> transactional local SQLite archive."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
import sqlite3
import subprocess
import uuid
import zipfile
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'implementation/weknora-sync'))
from operational_logs import OperationalLogs, error_fields
DEFAULT_ROOT = PROJECT / 'outputs/机器人5历史问答采集'
DEFAULT_BINDING = Path('/Users/xinchengyuan/Documents/ChatGPT/调研/weknora-local/wecom-connection.json')
NODE = '/Users/xinchengyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def connect(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    db = sqlite3.connect(root / 'history.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS messages(
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT,
        role TEXT NOT NULL, content TEXT, completed INTEGER NOT NULL,
        deleted_at TEXT, created_at TEXT, updated_at TEXT,
        payload TEXT NOT NULL, digest TEXT NOT NULL,
        first_collected_at TEXT NOT NULL, last_changed_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS messages_request ON messages(session_id,request_id);
      CREATE TABLE IF NOT EXISTS versions(
        id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, digest TEXT NOT NULL,
        collected_at TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
        scanned INTEGER, inserted INTEGER, updated INTEGER, unchanged INTEGER,
        source_upper TEXT, error TEXT);
    ''')
    os.chmod(root / 'history.sqlite3', 0o600)
    return db


def query_source(binding, cursor=None, pending=(), full=False):
    # SQL executes inside a server-enforced read-only repeatable-read snapshot.
    # A five-minute overlap plus explicit pending IDs catches late completion.
    channel = str(uuid.UUID(binding['channel_id']))
    agent = str(uuid.UUID(binding['agent_id']))
    bound = "TRUE" if full or not cursor else (
        f"greatest(m.created_at,m.updated_at,coalesce(m.deleted_at,m.updated_at)) >= "
        f"({literal(cursor)}::timestamptz - interval '5 minutes')")
    if pending:
        bound += ' OR m.id IN (' + ','.join(literal(str(uuid.UUID(x))) for x in pending) + ')'
    sql = f'''
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='30s';
WITH channel AS (
 SELECT id,tenant_id,agent_id,platform FROM im_channels
 WHERE id={literal(channel)} AND agent_id={literal(agent)} AND platform='wecom'
), scoped AS (
 SELECT m.id,m.session_id,m.request_id,m.role,m.content,m.is_completed,
 m.created_at,m.updated_at,m.deleted_at,m.channel,m.agent_id,m.model_id,
 m.knowledge_references,m.usage,m.images,m.attachments,m.agent_steps,
 s.tenant_id,s.title AS session_title,s.deleted_at AS session_deleted_at,
 i.user_id AS im_user_id,i.chat_id AS im_chat_id,i.platform,
 i.im_channel_id,i.status AS mapping_status,i.deleted_at AS mapping_deleted_at,
 CASE WHEN coalesce(m.agent_id,'')='' THEN 'channel_mapping' ELSE 'message_agent_id' END AS attribution
 FROM messages m JOIN sessions s ON s.id=m.session_id
 JOIN LATERAL (
   SELECT i.* FROM im_channel_sessions i JOIN channel c
   ON i.im_channel_id=c.id AND i.tenant_id=c.tenant_id
   WHERE i.session_id=m.session_id ORDER BY i.updated_at DESC,i.id DESC LIMIT 1
 ) i ON TRUE
 WHERE m.role IN ('user','assistant') AND m.channel='im'
 AND (coalesce(m.agent_id,'')='' OR m.agent_id={literal(agent)})
), changed AS (
 SELECT m.* FROM scoped m WHERE {bound}
), selected AS (
 SELECT m.* FROM scoped m WHERE m.id IN (SELECT id FROM changed)
 OR EXISTS (SELECT 1 FROM changed d WHERE d.session_id=m.session_id
   AND coalesce(d.request_id,'')<>'' AND d.request_id=m.request_id)
)
SELECT json_build_object('upper',transaction_timestamp(),'channel_count',(SELECT count(*) FROM channel),
 'source', (SELECT row_to_json(c) FROM channel c),
 'rows',coalesce((SELECT json_agg(r ORDER BY r.created_at,r.id) FROM selected r),'[]'::json));
COMMIT;
'''
    docker = binding.get('docker') or shutil.which('docker') or '/usr/local/bin/docker'
    target = binding.get('compose_file')
    command = ([docker, 'compose', '-f', str(target), 'exec', '-T', 'postgres']
               if target else [docker, 'exec', '-i', binding.get('postgres_container', 'WeKnora-postgres')])
    result = subprocess.run([*command, 'sh', '-c',
        'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'
    ], input=sql, text=True, capture_output=True, timeout=60)
    if result.returncode:
        # Do not log SQL or message contents from database errors.
        raise RuntimeError('source_read_failed: ' + result.stderr[:300])
    snapshot = json.loads(result.stdout)
    if snapshot['channel_count'] != 1:
        raise RuntimeError('robot5_binding_not_found_or_changed')
    if not isinstance(snapshot['rows'], list):
        raise RuntimeError('invalid_source_snapshot')
    return snapshot


def apply_snapshot(db, snapshot, binding):
    scope = canonical({'channel_id': binding['channel_id'], 'agent_id': binding['agent_id'],
                       'tenant_id': snapshot['source']['tenant_id']})
    prior = db.execute("SELECT value FROM state WHERE key='scope'").fetchone()
    if prior and prior[0] != scope:
        raise RuntimeError('archive_scope_changed_use_a_separate_data_directory')
    counts = {'scanned': len(snapshot['rows']), 'inserted': 0, 'updated': 0, 'unchanged': 0}
    stamp = now()
    with db:
        for row in snapshot['rows']:
            payload = canonical(row)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            old = db.execute('SELECT digest,first_collected_at FROM messages WHERE id=?', (row['id'],)).fetchone()
            if old and old['digest'] == digest:
                counts['unchanged'] += 1
                continue
            counts['updated' if old else 'inserted'] += 1
            db.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id,request_id=excluded.request_id,
              role=excluded.role,content=excluded.content,completed=excluded.completed,
              deleted_at=excluded.deleted_at,created_at=excluded.created_at,updated_at=excluded.updated_at,
              payload=excluded.payload,digest=excluded.digest,last_changed_at=excluded.last_changed_at''',
              (row['id'], row['session_id'], row.get('request_id'), row['role'], row.get('content'),
               bool(row.get('is_completed')), row.get('deleted_at'), row['created_at'], row['updated_at'],
               payload, digest, old['first_collected_at'] if old else stamp, stamp))
            db.execute('INSERT INTO versions(message_id,digest,collected_at,payload) VALUES(?,?,?,?)',
                       (row['id'], digest, stamp, payload))
        for key, value in [('scope', scope), ('cursor', snapshot['upper'])]:
            db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,value))
    return counts


def reasoning_process(answer):
    """Render only persisted execution data; never reconstruct missing thinking."""
    if answer is None:
        return '暂无回答，尚无执行步骤。'
    if 'agent_steps' not in answer:
        return '尚未补采执行步骤。'
    steps = answer.get('agent_steps')
    if not steps:
        return '源记录未保存执行步骤。'
    if not isinstance(steps, list):
        return '执行步骤格式异常；源字段已原样保存在本地记录。'

    def display(value):
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)

    blocks = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            blocks.append(f'【第{index}个执行步骤】\n' + display(step))
            continue
        lines = [f'【第{index}个执行步骤】']
        for key, label in [('iteration','迭代编号'),('timestamp','记录时间'),
                           ('thought','模型本轮输出'),('reasoning_content','模型返回的思考文本')]:
            value = step.get(key)
            if value is not None and value != '':
                lines.append(label + '：\n' + display(value))
        calls = step.get('tool_calls') or []
        for call_index, call in enumerate(calls, 1):
            if not isinstance(call, dict):
                lines.append(f'工具调用{call_index}：\n' + display(call))
                continue
            lines.append(f'工具调用{call_index}：' + str(call.get('name') or '名称未记录'))
            for key, label in [('id','调用ID'),('target','实际工具目标'),('args','调用参数'),('duration','耗时（源字段值）')]:
                if call.get(key) is not None:
                    lines.append(label + '：\n' + display(call[key]))
            result = call.get('result')
            if isinstance(result, dict):
                for key, label in [('success','工具执行成功标记'),('output','工具返回文本'),
                                   ('data','工具结构化返回'),('error','工具错误')]:
                    value = result.get(key)
                    if value is not None and value != '':
                        lines.append(label + '：\n' + display(value))
            else:
                lines.append('工具执行结果尚未保存。' if result is None else '工具返回：\n' + display(result))
            if call.get('reflection'):
                lines.append('已保存的工具反思：\n' + display(call['reflection']))
        if step.get('truncated'):
            lines.append('源记录标记：本轮模型输出被截断。')
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)


def qa_records(db):
    groups = {}
    for stored in db.execute('SELECT * FROM messages ORDER BY created_at,id'):
        row = json.loads(stored['payload'])
        row['first_collected_at'] = stored['first_collected_at']
        row['last_changed_at'] = stored['last_changed_at']
        # Missing request IDs never fall back to positional/adjacent pairing.
        key = (row['session_id'], row.get('request_id') or 'unpaired:' + row['id'])
        groups.setdefault(key, []).append(row)
    records = []
    for (session, request), rows in groups.items():
        users = [r for r in rows if r['role'] == 'user']
        answers = [r for r in rows if r['role'] == 'assistant']
        user = users[0] if len(users) == 1 else None
        for answer in answers or [None]:
            origin = answer or users[0]
            if len(users) > 1:
                status = '配对歧义'
            elif not user:
                status = '缺少问题'
            elif user.get('deleted_at') or answer and answer.get('deleted_at'):
                status = '源记录已删除'
            elif not answer:
                status = '等待回答'
            elif not answer.get('is_completed'):
                status = '未完成'
            elif not (answer.get('content') or '').strip():
                status = '完成但空回答'
            else:
                status = '已完成'
            records.append({
                'record_id': (answer or origin)['id'], 'session_id': session,
                'request_id': origin.get('request_id'),
                'user_message_id': user['id'] if user else None,
                'assistant_message_id': answer['id'] if answer else None,
                'user_id': origin.get('im_user_id'), 'chat_id': origin.get('im_chat_id'),
                'question': user.get('content') if user else None,
                'answer': answer.get('content') if answer else None,
                'agent_steps': answer.get('agent_steps') if answer else None,
                'reasoning_process': reasoning_process(answer),
                'question_at': user.get('created_at') if user else None,
                'answer_at': answer.get('created_at') if answer else None,
                'answer_updated_at': answer.get('updated_at') if answer else None,
                'status': status, 'platform': origin.get('platform'),
                'channel_id': origin.get('im_channel_id'), 'attribution': origin.get('attribution'),
                'references': answer.get('knowledge_references') if answer else None,
                'usage': answer.get('usage') if answer else None,
                'session_deleted_at': origin.get('session_deleted_at'),
                'first_collected_at': origin['first_collected_at'],
                'last_changed_at': origin['last_changed_at'],
            })
    return sorted(records, key=lambda r: (r['question_at'] or r['answer_at'] or '', r['record_id']))


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temp, 0o600)
    temp.replace(path)


def export_version(db, backend='artifact'):
    builder = 'portable_excel.py' if backend == 'openpyxl' else 'export.mjs'
    return str(db.execute('SELECT count(*) FROM versions').fetchone()[0]) + ':' + hashlib.sha256(
        Path(__file__).with_name(builder).read_bytes()).hexdigest()


def restore_literal_quotes(path):
    """Repair the library's missing literal-'= capability, never insert formulas."""
    sidecar = Path(str(path)+'.literal-restores.json')
    restores = json.loads(sidecar.read_text())
    if restores:
        uri = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        tag = lambda name: '{'+uri+'}'+name
        with zipfile.ZipFile(path) as source:
            sheets = {}
            for restore in restores:
                sheet_path=restore.get('sheet','xl/worksheets/sheet1.xml')
                if sheet_path not in ['xl/worksheets/sheet1.xml','xl/worksheets/sheet2.xml']:
                    raise RuntimeError('Unexpected literal repair sheet')
                if sheet_path not in sheets:
                    sheets[sheet_path]=ET.fromstring(source.read(sheet_path))
                cells={cell.get('r'):cell for cell in sheets[sheet_path].findall('.//'+tag('c'))}
                if not restore['value'].startswith("'="):
                    raise RuntimeError('Unexpected literal repair')
                cell=cells[restore['address']]
                if cell.find(tag('f')) is not None:
                    raise RuntimeError('Unexpected formula in literal repair')
                for child in list(cell):
                    cell.remove(child)
                cell.set('t','inlineStr')
                text=ET.SubElement(ET.SubElement(cell,tag('is')),tag('t'))
                text.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
                text.text=restore['value']
            repaired=path.with_suffix('.repair.xlsx')
            with zipfile.ZipFile(repaired,'w') as target:
                for info in source.infolist():
                    data=ET.tostring(sheets[info.filename],encoding='utf-8',xml_declaration=True) if info.filename in sheets else source.read(info.filename)
                    target.writestr(info,data)
        os.chmod(repaired,0o600)
        repaired.replace(path)
    sidecar.unlink()


def verify_excel(path, records):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(node.itertext()) for node in ET.fromstring(
                archive.read('xl/sharedStrings.xml')).findall('m:si', ns)]
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        if [sheet.get('name') for sheet in workbook.findall('m:sheets/m:sheet',ns)] != ['问答记录','采集明细']:
            raise RuntimeError('Unexpected workbook sheets')
        sheets = [ET.fromstring(archive.read(f'xl/worksheets/sheet{i}.xml')) for i in [1,2]]
    if any(sheet.findall('.//m:f', ns) for sheet in sheets):
        raise RuntimeError('Unexpected Excel formula in original text archive')
    grids = [{cell.attrib['r']: cell for cell in sheet.findall('.//m:sheetData/m:row/m:c', ns)} for sheet in sheets]

    def value(grid, address):
        cell = grids[grid].get(address)
        if cell is None:
            return None
        val = cell.find('m:v', ns)
        if cell.get('t') == 's':
            return strings[int(val.text)]
        if cell.get('t') == 'inlineStr':
            inline = cell.find('m:is', ns)
            return ''.join(inline.itertext()) if inline is not None else ''
        return val.text if val is not None else None

    headers=[value(0,f'{col}5') for col in 'ABCDEFG']
    if headers != ['时间（北京时间）','用户','问题','回答','状态','引用资料','推理过程']:
        raise RuntimeError('Unexpected main table headers')
    main_headers=[cell.get('r') for cell in sheets[0].findall('.//m:row[@r="5"]/m:c',ns)]
    if len(main_headers)!=7:
        raise RuntimeError('Technical columns leaked into the main table')
    for sheet in sheets:
        data_rows=[int(row.get('r')) for row in sheet.findall('.//m:row',ns) if int(row.get('r'))>=6]
        if data_rows!=list(range(6,len(records)+6)):
            raise RuntimeError('Excel must have exactly one row per QA on each sheet')
    detail_headers={cell.get('r')[:-1]:value(1,cell.get('r')) for cell in sheets[1].findall('.//m:row[@r="5"]/m:c',ns)}
    if '企微送达状态' in detail_headers.values():
        raise RuntimeError('Removed delivery-status column reappeared')
    overflow_count=0
    ids=[]
    for index,record in enumerate(records,6):
        if int(value(1,f'A{index}'))!=index-5:
            raise RuntimeError('Detail row index mismatch')
        ids.append(value(1,f'B{index}'))
        for col,key in [('B','record_id'),('C','session_id'),('D','request_id'),('E','user_message_id'),('F','assistant_message_id')]:
            if (value(1,f'{col}{index}') or '')!=(record.get(key) or ''):
                raise RuntimeError('Detail linkage mismatch')
        for col,key in [('B','user_id'),('E','status')]:
            if (value(0,f'{col}{index}') or '')!=(record.get(key) or ''):
                raise RuntimeError('Main user/status mismatch')
        names=[]
        for ref in record.get('references') or []:
            name=ref.get('knowledge_title') or ref.get('knowledge_filename') or ref.get('knowledge_id') or ref.get('id')
            if name and name not in names:
                names.append(name)
        fields=[('C','question','问题原文',record.get('question') or ''),
                ('D','answer','回答原文',record.get('answer') or ''),
                ('F','references','引用资料名称','\n'.join(names)),
                ('G','reasoning_process','推理过程',record.get('reasoning_process') or '')]
        for col,key,label,original in fields:
            if len(original.encode('utf-16-le'))//2>32767:
                overflow_count+=1
                expected=f'【{label}超过 Excel 单元格上限，完整内容见“采集明细”同序号行的{label}片段列】'
                if value(0,f'{col}{index}')!=expected:
                    raise RuntimeError('Missing explicit Excel overflow reference')
                restored=''.join(value(1,f'{detail_col}{index}') or '' for detail_col,header in detail_headers.items()
                                 if header and header.startswith(label+'片段'))
            else:
                restored=value(0,f'{col}{index}') or ''
            if restored != original:
                raise RuntimeError(f'Excel original text mismatch: {record["record_id"]} {key}')
    if len(set(ids))!=len(records):
        raise RuntimeError('Duplicate Excel QA record ID')
    return {'format':'qa-main-and-collection-detail-v3-reasoning','records':len(records),
            'excel_rows':len(records),'detail_rows':len(records),'main_columns':7,
            'original_text_exact':True,'overflow_fields':overflow_count,'formulas':0}


def export_snapshot(db, root, records, version, preview=False, backend='artifact', logs=None):
    """Publish a captured archive version; callers must own excel.lock."""
    # Complete original text remains in SQLite; JSON is also usable without Excel.
    atomic_json(root / '问答记录.json', records)
    pending = root / 'export-pending.xlsx'
    if logs:logs.record('history_export_started',{'qa_records':len(records)})
    if backend == 'openpyxl':
        from portable_excel import write_excel
        write_excel(records, pending)
    elif backend == 'artifact':
        result = subprocess.run([NODE, str(Path(__file__).with_name('export.mjs')),
                             str(pending), *(['--preview'] if preview else [])],
                            input=json.dumps(records, ensure_ascii=False), text=True,
                            capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError('excel_export_failed: ' + result.stderr[-500:])
        restore_literal_quotes(pending)
    else:
        raise ValueError('unsupported_excel_backend')
    validation = verify_excel(pending,records)
    pending.replace(root / '机器人5问答.xlsx')
    atomic_json(root / 'excel-validation.json',{'verified_at':now(),**validation})
    db.execute("INSERT INTO state VALUES('export_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (version,))
    db.commit()
    if logs:logs.record('history_export_completed',{'qa_records':len(records)})
    return len(records)


def capture_export(db,backend):
    # Release the read transaction before rendering. A source writer must never
    # wait on a long export, and the saved version must match the captured rows.
    db.execute('BEGIN')
    try:
        version=export_version(db,backend)
        records=qa_records(db)
        db.commit()
        return records,version
    except Exception:
        db.rollback()
        raise


def export(db, root, preview=False, backend='artifact', logs=None):
    with (root/'excel.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        records,version=capture_export(db,backend)
        return export_snapshot(db,root,records,version,preview,backend,logs)


def sync(db, root, binding, full=False, preview=False, logs=None, after_collect=None, async_export=False):
    start = now()
    run_id = db.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')", (start,)).lastrowid
    db.commit()
    if logs:logs.record('history_run_started',{'run_id':run_id},runtime=True)
    try:
        cursor_row = db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
        full_row = db.execute("SELECT value FROM state WHERE key='last_full'").fetchone()
        reconcile_full = full or not full_row or (
            dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(full_row[0])).total_seconds()>=86400
        pending = [r[0] for r in db.execute("SELECT id FROM messages WHERE role='assistant' AND completed=0 AND deleted_at IS NULL")]
        snapshot = query_source(binding, cursor_row[0] if cursor_row else None, pending, reconcile_full)
        counts = apply_snapshot(db, snapshot, binding)
        if reconcile_full:
            db.execute("INSERT INTO state VALUES('last_full',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now(),))
            db.commit()
        if after_collect:after_collect()
        backend = binding.get('export_backend', 'artifact')
        version = export_version(db, backend)
        exported = db.execute("SELECT value FROM state WHERE key='export_version'").fetchone()
        excel_state={'status':'current','version':version}
        if not exported or exported[0] != version or not (root / '机器人5问答.xlsx').exists() or preview:
            if async_export:
                from excel_jobs import request_export
                excel_state=request_export(root,backend,preview,logs)
            else:
                export(db, root, preview, backend=backend, logs=logs)
        summary = {**counts, 'source_upper': snapshot['upper'], 'completed_at': now(),
                   'qa_records': len(qa_records(db)), 'messages': db.execute('SELECT count(*) FROM messages').fetchone()[0],
                   'excel_export':excel_state}
        db.execute('''UPDATE runs SET finished_at=?,status='success',scanned=?,inserted=?,updated=?,unchanged=?,source_upper=? WHERE id=?''',
                   (now(),counts['scanned'],counts['inserted'],counts['updated'],counts['unchanged'],snapshot['upper'],run_id))
        db.commit()
        if logs:
            logs.record('history_run_completed',{'run_id':run_id,**summary},runtime=True)
            if counts['inserted'] or counts['updated']:
                logs.record('history_archive_updated',{'run_id':run_id,**summary})
        atomic_json(root / 'health.json', {'status':'success', **summary,
            **({'operational_logs':{'enabled':logs.enabled,'logging_error':logs.last_error}} if logs else {})})
        return summary
    except Exception as exc:
        db.rollback()
        db.execute("UPDATE runs SET finished_at=?,status='failed',error=? WHERE id=?", (now(),str(exc),run_id))
        db.commit()
        atomic_json(root / 'health.json', {'status':'failed','at':now(),'run_id':run_id,**error_fields(exc)})
        if logs:logs.record('history_run_failed',{'run_id':run_id,**error_fields(exc)},level='ERROR')
        raise


def run_cycle(db,root,binding,full=False,preview=False,logs=None):
    """Publish committed QAs before Excel work, with recovery on source failure."""
    import crm_sync
    failure=None
    crm_result=None
    def sync_crm():
        nonlocal crm_result
        try:crm_result=crm_sync.tick(db,qa_records(db),root,logs=logs)
        except Exception as exc:crm_result={'status':'failed','error_code':'internal_'+type(exc).__name__}
    try:
        result=sync(db,root,binding,full,preview,logs=logs,after_collect=sync_crm,async_export=binding.get('export_async',False))
    except Exception as exc:
        failure=exc
        result={}
    if crm_result is None:sync_crm()
    result['crm_sync']=crm_result
    if failure is not None:
        raise failure
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sync','export','export-job','status','export-logs','crm-check','crm-sync','crm-status','crm-prepare'])
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--binding', type=Path, default=DEFAULT_BINDING)
    parser.add_argument('--full', action='store_true', help='Reconcile all retained source rows; never erase the local archive')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--export-backend', choices=['artifact','openpyxl'], default='artifact')
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--crm-limit',type=int,default=None)
    parser.add_argument('--crm-budget',type=float,default=None)
    parser.add_argument('--approve-field-changes',action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.command=='export-job':
        from excel_jobs import run_export
        print(json.dumps(run_export(args.root,args.export_backend,args.preview),ensure_ascii=False))
        return
    if args.command=='export-logs':
        if not args.destination:raise ValueError('support_log_destination_required')
        logs=OperationalLogs({'state_dir':str(args.root),'operational_logging':{'enabled':True}},component='qa_collector')
        print(str(logs.export(args.destination)))
        return
    with open(args.root / 'collector.lock','a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"already_running"}')
            return
        db = connect(args.root)
        logs=OperationalLogs({'state_dir':str(args.root),'operational_logging':{'enabled':True,
            'runtime_files':[str(args.root/'service.log'),str(args.root/'service-error.log')]}},component='qa_collector')
        if args.command in ('sync','export'):logs.bootstrap()
        try:
            if args.command == 'sync':
                result = run_cycle(db,args.root,json.loads(args.binding.read_text()),args.full,args.preview,logs=logs)
            elif args.command == 'export':
                result = {'qa_records':export(db,args.root,args.preview,backend=args.export_backend,logs=logs)}
            elif args.command.startswith('crm-'):
                import crm_sync
                if args.command=='crm-sync':
                    result=crm_sync.tick(db,qa_records(db),args.root,force=True,limit=args.crm_limit,budget=args.crm_budget,logs=logs)
                elif args.command in ['crm-check','crm-prepare']:
                    try:
                        config=crm_sync.read_config(args.root)
                        if config is None:raise crm_sync.SyncError('not_configured')
                        client=crm_sync.Client(config)
                        schema=client.list_access()
                        if args.command=='crm-prepare':
                            if not args.approve_field_changes:raise crm_sync.SyncError('explicit_field_change_approval_required')
                            stamp=dt.datetime.now().strftime('%Y%m%d_%H%M%S')
                            atomic_json(args.root/'private'/('crm-schema-before-'+stamp+'.json'),schema)
                            schema=client.prepare_schema()
                        crm_sync.field_map(schema)
                        result={'status':'ready','worksheet_name':schema.get('name')}
                    except crm_sync.SyncError as exc:
                        result={'status':'blocked','error_code':exc.code}
                else:
                    path=args.root/'crm-health.json'
                    result=json.loads(path.read_text()) if path.exists() else {'status':'not_started'}
            else:
                result = {'messages':db.execute('SELECT count(*) FROM messages').fetchone()[0],
                          'qa_records':len(qa_records(db)),
                          'last_run':dict(db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone() or {}),
                          'statuses':{}}
                for record in qa_records(db):
                    result['statuses'][record['status']] = result['statuses'].get(record['status'],0)+1
            print(json.dumps(result,ensure_ascii=False))
        finally:
            db.close()
            if args.command in ('sync','export'):logs.maintain()


if __name__ == '__main__':
    main()
