"""Read-only source/Markdown/WeKnora audit; writes only a new report directory."""
import argparse
import collections
import datetime
import html
import json
from pathlib import Path
import time

from reconcile import API, KB, hashes, norm, save
from worker import SUPPORTED


def inventory(root, markdown=False):
    regular, placeholders, hidden, links = [], [], [], []
    for path in sorted(root.rglob('*')):
        relative = norm(path.relative_to(root))
        if path.is_symlink():
            links.append(relative)
            continue
        if not path.is_file():
            continue
        if path.suffix == '.icloud':
            placeholders.append(relative)
        elif any(p.startswith('.') for p in path.relative_to(root).parts):
            hidden.append(relative)
        elif not markdown or path.suffix.lower() == '.md':
            stat = path.stat()
            regular.append({'relative': relative, 'path': str(path), 'size': stat.st_size,
                            'mtime_ns': stat.st_mtime_ns})
    return regular, placeholders, hidden, links


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('use_a_fresh_report_directory')
    args.output.mkdir(parents=True)
    started = time.time()
    cfg = json.loads(args.config.read_text())
    if cfg['knowledge_base_id'] != KB:
        raise ValueError('api_inventory_scope_mismatch')
    sources, placeholders, hidden, links = inventory(Path(cfg['source_root']))
    markdowns, _, md_hidden, _ = inventory(Path(cfg['markdown_root']), markdown=True)
    md_by_path = {norm(row['path']): row for row in markdowns}
    for row in markdowns:
        try:
            row.update(hashes(Path(row['path'])))
        except (OSError, ValueError) as error:
            row['read_error'] = str(error)
    print('Local inventory:', len(sources), 'source files;', len(markdowns), 'Markdown files', flush=True)
    api = API(cfg['api_key_file'], cfg['weknora_base_url'])
    for attempt in range(3):
        try:
            docs = api.inventory()
            break
        except RuntimeError as error:
            if str(error) != 'remote_inventory_changed_retry_required' or attempt == 2:
                raise
            time.sleep(2)
    remote = {doc['id']: doc for doc in docs}
    state = json.loads((Path(cfg['state_dir']) / 'state.json').read_text())
    live = collections.defaultdict(list)
    for identity, record in state['records'].items():
        if record['state'] != 'deleted':
            live[record.get('desired_path') or record['path']].append((identity, record))
    rows, referenced, mapped_markdown = [], set(), set()
    for index, source in enumerate(sources, 1):
        relative = source['relative']
        expected_md = str(Path(cfg['markdown_root']) / (relative + '.md'))
        candidates = live.get(relative, [])
        identity, record = candidates[0] if len(candidates) == 1 else ('', {})
        md_path = record.get('md_path') or expected_md
        md = md_by_path.get(norm(md_path))
        ids = record.get('remote_ids', [])
        matches = [remote[i] for i in ids if i in remote]
        linked = bool(identity and md and ids and len(matches) == len(ids))
        supported = Path(relative).suffix.lower() in SUPPORTED
        original_mode=record.get('ingest_mode')=='original'
        source_meta={}
        source_hash = None
        source_error = None
        if identity:
            try:
                source_meta = hashes(Path(source['path']))
                source_hash = source_meta['sha256']
            except (OSError, ValueError) as error:
                source_error = str(error)
        source_current = bool(source_hash and source_hash == record.get('source_hash'))
        md_current = bool(md and not md.get('read_error') and md.get('sha256') == record.get('md_sha'))
        remote_current = bool(matches and md and all(d.get('file_hash') == md.get('md5')
            and d.get('file_size') == md['size'] for d in matches))
        indexed = bool(matches and all(d.get('parse_status') == 'completed' and d.get('enable_status') == 'enabled'
                                     and not d.get('pending_subtasks_count') for d in matches))
        if original_mode:
            linked=bool(identity and ids and len(matches)==len(ids))
            artifact=Path(record.get('original_artifact') or '/nonexistent-original-artifact')
            md_current=None
            artifact_current=artifact.is_file() and hashes(artifact)['sha256']==record.get('artifact_sha256')==source_hash
            remote_current=bool(matches and source_meta and all(d.get('file_hash')==source_meta.get('md5') and d.get('file_size')==source_meta.get('size') for d in matches))
            consistent=linked and source_current and artifact_current and remote_current and indexed
        else:
            consistent = linked and source_current and md_current and remote_current and indexed
        if relative in cfg.get('excluded_sources', {}):
            category = '既有排除项'
        elif len(candidates) > 1:
            category = '多条有效映射待核对'
        elif record.get('no_text'):
            category = '无可辨认文字，暂不入库'
        elif consistent:
            category = '原件直传版本与索引一致' if original_mode else '三方当前版本一致' if supported else '三方当前版本一致（历史解析格式）'
        elif linked:
            category = '已建立映射，当前版本或索引待核对'
        elif not supported:
            category = '当前不支持且尚未建立映射'
        else:
            category = '三方映射未完整'
        rows.append({'source_relative': relative, 'source_path': source['path'], 'source_size': source['size'],
            'source_id': identity, 'source_sha256': source_hash, 'source_matches_mapping': source_current,
            'ingest_mode':record.get('ingest_mode','markdown'),'uploaded_artifact_path':record.get('original_artifact') if original_mode else md_path if md else None,
            'markdown_path': md_path if md and not original_mode else None, 'markdown_sha256': md.get('sha256') if md else None,
            'markdown_matches_mapping': md_current, 'weknora_ids': ids,
            'weknora_documents': matches, 'remote_content_matches': remote_current, 'index_ready': indexed,
            'three_way_mapping_exists': linked, 'current_version_consistent': consistent,
            'worker_state': record.get('state'), 'category': category, 'incremental_format_supported': supported,
            'error': source_error or record.get('error') or (md or {}).get('read_error'),
            'pending_weknora_ids': (record.get('pending') or {}).get('remote_ids', []),
            'expected_folder': cfg['folder_root'] + '/' + str(Path(relative).parent)})
        referenced.update(ids)
        referenced.update((record.get('pending') or {}).get('remote_ids', []))
        if linked and md and not original_mode:
            mapped_markdown.add(norm(md_path))
        if index % 1000 == 0:
            print('Audited', index, 'sources', flush=True)
    folder = cfg['folder_root'].strip('/')
    scope_docs = [d for d in docs if (d.get('folder_path') or '').strip('/') == folder
                  or (d.get('folder_path') or '').strip('/').startswith(folder + '/')]
    mapped_rows = [row for row in rows if row['three_way_mapping_exists']]
    unique_mapped_ids = {i for row in mapped_rows for i in row['weknora_ids']}
    groups = collections.defaultdict(list)
    for row in mapped_rows:
        for identity in row['weknora_ids']:
            groups[identity].append(row['source_relative'])
    duplicates = [{'weknora_id': i, 'source_count': len(paths), 'source_paths': paths}
                  for i, paths in groups.items() if len(paths) > 1]
    orphan_md = [md for md in markdowns if norm(md['path']) not in mapped_markdown]
    existing_source_references = set(referenced)
    for records in live.values():
        for _, record in records:
            referenced.update(record.get('remote_ids', []))
            referenced.update((record.get('pending') or {}).get('remote_ids', []))
    source_names = {r['relative'] for r in sources}
    missing_sources = [{'source_id': identity, 'source_relative': record.get('desired_path') or record['path'],
                        'state': record['state'], 'weknora_ids': record.get('remote_ids', []), 'markdown_path': record.get('md_path')}
                       for relative, records in live.items() if relative not in source_names
                       for identity, record in records]
    summary = {'checked_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        'duration_seconds': round(time.time() - started), 'source_root': cfg['source_root'],
        'markdown_root': cfg['markdown_root'], 'knowledge_base_id': cfg['knowledge_base_id'],
        'source_files': len(sources), 'source_supported_files': sum(Path(r['relative']).suffix.lower() in SUPPORTED for r in sources),
        'historical_format_mapped_sources': sum(not r['incremental_format_supported'] and r['three_way_mapping_exists'] for r in rows),
        'unsupported_format_unmapped_sources': sum(not r['incremental_format_supported'] and not r['three_way_mapping_exists'] for r in rows),
        'source_placeholders': len(placeholders), 'source_hidden_auxiliary_files': len(hidden),
        'source_symlinks': len(links), 'markdown_files': len(markdowns),
        'knowledge_base_total_documents': len(docs), 'knowledge_scope_documents': len(scope_docs),
        'knowledge_outside_scope_documents': len(docs) - len(scope_docs),
        'original_direct_sources':sum(row.get('ingest_mode')=='original' for row in rows),
        'mapped_source_files': len(mapped_rows), 'mapped_markdown_files': len(mapped_markdown),
        'unique_mapped_weknora_documents': len(unique_mapped_ids),
        'current_consistent_source_files': sum(row['current_version_consistent'] for row in rows),
        'categories': dict(collections.Counter(row['category'] for row in rows)),
        'shared_document_groups': len(duplicates), 'extra_source_aliases': sum(g['source_count'] - 1 for g in duplicates),
        'markdown_without_complete_mapping': len(orphan_md),
        'knowledge_scope_without_live_source_reference': len([d for d in scope_docs if d['id'] not in referenced]),
        'knowledge_scope_without_existing_local_source': len([d for d in scope_docs if d['id'] not in existing_source_references]),
        'live_mappings_with_missing_source': len(missing_sources),
        'historical_deleted_mapping_records': sum(r['state'] == 'deleted' for r in state['records'].values()),
        'worker_states': dict(collections.Counter(r['state'] for r in state['records'].values())),
        'retirements_pending': state['retire'], 'readonly_audit': True,
        'scan_started_at': datetime.datetime.fromtimestamp(started, datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        'note': '落地文件不含占位/隐藏辅助文件；WeKnora目录数含子目录。映射一致性是文件及索引核验，不代表机器人回答验收。'}
    save(args.output / 'summary.json', summary)
    save(args.output / 'mapping.json', rows)
    save(args.output / 'shared-documents.json', duplicates)
    save(args.output / 'markdown-without-complete-mapping.json', orphan_md)
    save(args.output / 'knowledge-without-live-source-reference.json', [d for d in scope_docs if d['id'] not in referenced])
    save(args.output / 'missing-source-mappings.json', missing_sources)
    save(args.output / 'placeholders.json', placeholders)
    save(args.output / 'weknora-inventory.json', docs)
    render_html(args.output / '三方映射明细.html', summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def render_html(path, summary, rows):
    esc = lambda value: html.escape(str(value if value is not None else ''), quote=True)
    body = []
    for row in rows:
        cells = [row['source_relative'], row['category'], row['markdown_path'] or '—',
                 '; '.join(row['weknora_ids']) or '—', row['worker_state'] or '—']
        body.append('<tr data-linked="' + ('yes' if row['three_way_mapping_exists'] else 'no') + '" data-category="' + esc(row['category']) + '">' +
                    ''.join('<td>' + esc(value) + '</td>' for value in cells) + '</tr>')
    options = ''.join('<option>' + esc(value) + '</option>' for value in summary['categories'])
    text = '''<!doctype html><meta charset="utf-8"><title>机器人5三方映射盘点</title>
<style>body{font:14px system-ui;margin:28px;color:#17212b}input,select{padding:10px;margin:8px}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left;word-break:break-all}thead{position:sticky;top:0;background:#edf4f8}td:first-child{min-width:260px}pre{white-space:pre-wrap;background:#f5f7f9;padding:16px}</style>
<h1>机器人5三方映射盘点</h1><p>只读盘点；每行对应一个当前本地来源文件。可按路径、知识ID或状态筛选。</p>'''
    counts = [('本地落地来源文件', summary['source_files']), ('解析后的Markdown', summary['markdown_files']),
              ('WeKnora项目to咨询（含子目录）', summary['knowledge_scope_documents']),
              ('WeKnora全部售前文档（整个知识库）', summary['knowledge_base_total_documents']),
              ('已建立三方映射的来源', summary['mapped_source_files']),
              ('当前版本指纹及索引一致的来源', summary['current_consistent_source_files']),
              ('映射引用的唯一知识条目', summary['unique_mapped_weknora_documents']),
              ('多个来源共享知识条目的分组', summary['shared_document_groups']),
              ('共享知识条目的额外来源', summary['extra_source_aliases'])]
    text += '<p>扫描时间：' + esc(summary['scan_started_at']) + ' 至 ' + esc(summary['checked_at']) + '</p>'
    text += '<table>' + ''.join('<tr><th>'+esc(k)+'</th><td>'+esc(v)+'</td></tr>' for k,v in counts) + '</table>'
    text += '<p>另有 '+str(summary['source_placeholders'])+' 个下载占位文件、'+str(summary['source_hidden_auxiliary_files'])+' 个隐藏辅助文件，不计入落地来源。映射核验不等于人工审核或机器人回答验收。</p>'
    text += '<p>分类：' + '；'.join(esc(k)+' '+str(v)+' 份' for k,v in summary['categories'].items()) + '</p>'
    text += '<input id="q" placeholder="搜索路径或知识ID"><select id="category"><option value="">全部状态</option>' + options + '</select><label><input type="checkbox" id="linked">只看已建立映射</label><span id="count"></span>'
    text += '<table><thead><tr><th>本地来源相对路径</th><th>映射核验状态</th><th>解析后Markdown</th><th>WeKnora知识ID</th><th>同步状态</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table>'
    text += '''<script>const rs=[...document.querySelectorAll('tr[data-category]')],q=document.getElementById('q'),c=document.getElementById('category'),l=document.getElementById('linked');function filter(){let n=0;for(const r of rs){let ok=(!l.checked||r.dataset.linked==='yes')&&(!c.value||r.dataset.category===c.value)&&r.textContent.toLowerCase().includes(q.value.toLowerCase());r.hidden=!ok;if(ok)n++}document.getElementById('count').textContent='显示 '+n+' / '+rs.length+' 条'}q.oninput=filter;c.onchange=filter;l.onchange=filter;filter();</script>'''
    path.write_text(text)


if __name__ == '__main__':
    main()
