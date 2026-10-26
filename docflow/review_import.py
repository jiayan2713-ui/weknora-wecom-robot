"""Adopt an explicitly enumerated reading set without calling a parsing model.

Import is NOT human approval. It creates ordinary managed units so the existing
approve -> generation contract can freeze exactly the reviewed reading version.
"""
from pathlib import Path
import re
from .common import WorkflowError, artifact, digest, fingerprint, now, read_json, safe_id, stable_copy, write_json, atomic_text, verify
from .config import configuration
from .parser import markdown
from .reliability import quality_summary

EMPTY = '**本单元未取得解析正文，请对照原件核查。**'


def reading_units(text):
    matches = list(re.finditer(r'^## ([^\n]+)\n', text, re.M))
    sections = []
    if matches and text[:matches[0].start()].strip():
        sections.append(('文档前言与来源信息', text[:matches[0].start()]))
    if matches:
        sections.extend((m[1], text[m.end(): matches[i+1].start() if i+1<len(matches) else len(text)])
                        for i, m in enumerate(matches))
    else:
        sections.append(('完整阅读稿', text))
    units = []
    for i, (label, body) in enumerate(sections, 1):
        body = re.sub(r'\n---\s*$', '', body).strip()
        non_evidence = body.replace(EMPTY, '')
        non_evidence = re.sub(r'(?m)^> (?:解析提示|格式提示|原文格式说明)：[^\n]*$', '', non_evidence).strip()
        empty = EMPTY in body and not non_evidence
        issues = ['原阅读稿存在空正文，需补齐或明确排除'] if EMPTY in body or empty else []
        if '[无法辨认]' in body or '[待核对]' in body:
            issues.append('正文有待核对标记')
        units.append({'id': f'u{i:04d}', 'label': label + f'；阅读段{i}',
                      'body': '' if empty else body, 'notes': '从用户阅读稿导入，尚未批准。',
                      'text': '', 'image': None, 'issues': issues,
                      'content_status': 'needs_manual_confirmation' if issues else 'transcribed'})
    return units


def copy_once(source, target, expected=None):
    expected = expected or digest(Path(source))
    if target.exists():
        if digest(target) != expected:
            raise WorkflowError('INTEGRITY_ERROR', '导入暂存文件与原版本不同')
    else:
        stable_copy(Path(source), target, expected)


def import_reading_set(workflow, args):
    allowed = {'request_id', 'project', 'manifest', 'config'}
    if set(args)-allowed or not isinstance(args.get('project'), str) or not args['project'].strip():
        raise WorkflowError('REQUEST_FIELDS', '需要request_id、project、manifest，可选config')
    rid = safe_id(args.get('request_id'))
    manifest_path = Path(args.get('manifest', ''))
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or not isinstance(manifest.get('documents'), list) or not manifest['documents']:
        raise WorkflowError('REVIEW_IMPORT_INVALID', '导入清单结构无效或为空')
    config = configuration(args.get('config'))
    identities = []
    for row in manifest['documents']:
        for key in ['source', 'reading', 'provenance']:
            ref = row.get(key)
            if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or digest(Path(ref['path'])) != ref.get('sha256'):
                raise WorkflowError('CONTENT_CHANGED', '导入资料、阅读稿或溯源与指定版本不一致', scope=key)
        if not isinstance(row.get('name'), str) or not row['name'].strip():
            raise WorkflowError('REVIEW_IMPORT_INVALID', '缺少原文件名称')
        identities.append(row['source']['sha256'])
    if len(set(identities)) != len(identities):
        raise WorkflowError('REVIEW_IMPORT_INVALID', '同一原件重复，需先合并来源')
    identity = fingerprint({'manifest': manifest, 'project': args['project'], 'config': config})
    with workflow.store.lock():
        index_path = workflow.store.path(f'review-imports/{rid}.json')
        if index_path.exists():
            old = read_json(index_path)
            if old['identity'] != identity:
                raise WorkflowError('REQUEST_CONFLICT', '同一导入request_id不能改变资料或配置')
            if old.get('complete'):
                for ref in old['artifacts']:
                    verify(workflow.store.root, ref)
                return workflow._response(workflow.store.batch(old['batch_id']))
        bid = 'b' + fingerprint({'review_import': rid})[:31]
        base = workflow.store.path(f'batches/{bid}')
        write_json(index_path, {'identity': identity, 'batch_id': bid, 'complete': False})
        batch = {'id': bid, 'project': args['project'], 'config': config, 'phase': 'awaiting_review',
                 'revision': 1, 'documents': [], 'reviews': [], 'errors': [], 'created_at': now(),
                 'verified_at': now(), 'pending': None, 'review_import': str(index_path)}
        refs = []
        for key in ['parser_prompt', 'evaluation_prompt']:
            dest = base/'prompts'/(key+'.md')
            copy_once(Path(config[key]), dest)
            batch[key] = artifact(workflow.store.root, dest)
            refs.append(batch[key])
        for i, row in enumerate(manifest['documents'], 1):
            docid = f'd{i:04d}';suffix = Path(row['name']).suffix
            source = base/'sources'/('original-'+row['source']['sha256']+suffix)
            copy_once(Path(row['source']['path']), source, row['source']['sha256'])
            reading = base/'imported-readings'/(docid+'.md')
            provenance = base/'imported-readings'/(docid+'.provenance.json')
            copy_once(Path(row['reading']['path']), reading, row['reading']['sha256'])
            copy_once(Path(row['provenance']['path']), provenance, row['provenance']['sha256'])
            text = reading.read_text(encoding='utf-8');units = reading_units(text)
            cache_key = fingerprint({'batch_id': bid, 'source': row['source']['sha256'], 'reading': row['reading']['sha256'], 'provenance': row['provenance']['sha256'], 'version': 'reading-import-v1'})
            cache_dir = workflow.store.path(f'parse-cache/{cache_key}/reading-import-v1')
            safe_name = row['name'].replace('/','_')
            while len(safe_name.encode('utf-8')) > 180:
                safe_name = safe_name[:-1]
            doc = {'id': docid, 'name': docid+'_'+safe_name, 'original_name': row['name'],
                   'extension': suffix.lower(), 'source': artifact(workflow.store.root, source), 'url': None,
                   'state': 'parsed', 'cache_key': cache_key, 'cache_version': 'reading-import-v1',
                   'quality': quality_summary(units), 'units_done': len(units), 'units_total': len(units),
                   'issues': [x for u in units for x in u['issues']],
                   'reading_input': {**row['reading'], 'archived': artifact(workflow.store.root, reading)},
                   'provenance_input': {**row['provenance'], 'archived': artifact(workflow.store.root, provenance)}}
            cache = {'schema_version': 1, 'cache_key': cache_key, 'cache_version': 'reading-import-v1',
                     'source_hash': row['source']['sha256'], 'units': units,
                     'artifacts': [doc['source'], doc['reading_input']['archived'], doc['provenance_input']['archived']]}
            cache_path=cache_dir/'parse-manifest.json';write_json(cache_path,cache)
            doc['cache_ref']=artifact(workflow.store.root,cache_path)
            auto=base/'parsing/auto'/(docid+'.md');working=base/'parsing/working'/(docid+'.md')
            text=markdown(doc,cache)
            if auto.exists() and auto.read_text()!=text:
                raise WorkflowError('INTEGRITY_ERROR','导入自动稿变化')
            if not auto.exists():atomic_text(auto,text,replace=False)
            copy_once(auto,working)
            doc['auto_ref']=artifact(workflow.store.root,auto);doc['working_path']=str(working.relative_to(workflow.store.root))
            refs += cache['artifacts']+[doc['cache_ref'],doc['auto_ref'],artifact(workflow.store.root,working)]
            batch['documents'].append(doc)
        workflow._save_batch(batch)
        write_json(index_path,{'identity':identity,'batch_id':bid,'complete':True,'artifacts':refs})
        return workflow._response(batch)
