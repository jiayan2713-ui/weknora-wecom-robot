"""Verify processing and indexed retrieval, independently of bot answer acceptance."""
import argparse
import collections
import json
from pathlib import Path
import re
import time

from reconcile import API, KB, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--max-rounds', type=int, default=12)
    parser.add_argument('--only-unverified', action='store_true')
    args = parser.parse_args()
    api = API(args.key_file)
    attempts = json.loads((args.output / 'publish-attempts.json').read_text())
    ids = [a['remote_id'] for a in attempts.values() if a['status'] in ('uploaded', 'duplicate_linked')]
    check_path = args.output / 'upload-retrieval-verification.json'
    previous_checks = json.loads(check_path.read_text()) if args.only_unverified and check_path.exists() else []
    verified_ids = {c['id'] for c in previous_checks if c.get('retrieval_verified')}
    if args.only_unverified:
        ids = [identity for identity in ids if identity not in verified_ids]
    states = []
    for turn in range(args.max_rounds):
        states = []
        for identity in ids:
            status, response = api.request('/knowledge/' + identity)
            if status != 200 or not response.get('success'):
                raise ValueError('knowledge_status_read_failed')
            doc = response['data']
            states.append({k: doc.get(k) for k in ('id', 'file_name', 'file_hash', 'file_size',
                'parse_status', 'enable_status', 'summary_status', 'pending_subtasks_count', 'error_message', 'folder_path')})
        status_path = args.output / 'upload-status.json'
        old_states = json.loads(status_path.read_text()) if args.only_unverified and status_path.exists() else []
        save(status_path, [d for d in old_states if d['id'] not in ids] + states)
        counts = dict(collections.Counter(d['parse_status'] for d in states))
        print('Processing', counts, 'pending subtasks', sum(d.get('pending_subtasks_count') or 0 for d in states), flush=True)
        if all(d['parse_status'] in ('completed', 'failed') for d in states):
            break
        if turn + 1 < args.max_rounds:
            time.sleep(20)
    checks = []
    for doc in states:
        check = {'id': doc['id'], 'file_name': doc['file_name'], 'parse_status': doc['parse_status'],
                 'enable_status': doc['enable_status'], 'retrieval_verified': False, 'bot_answer_verified': False}
        if doc['parse_status'] != 'completed' or doc['enable_status'] != 'enabled':
            checks.append(check)
            continue
        status, response = api.request('/chunks/' + doc['id'] + '?page=1&page_size=100')
        chunks = response.get('data') or []
        chunk_total = response.get('total')
        if status != 200 or not chunks:
            check['error'] = 'no_readable_chunks'
            checks.append(check)
            continue
        usable = next((c for c in chunks if c.get('chunk_type') == 'text' and c.get('is_enabled') is True and c.get('content', '').strip()), None)
        if not usable:
            check['error'] = 'no_enabled_text_chunk'
            checks.append(check)
            continue
        # A content-derived probe tests indexing; it is not an end-user question.
        query = re.sub(r'<!--.*?-->', '', usable['content'], flags=re.S).strip()[:160]
        body = {'query_text': query, 'match_count': 5, 'knowledge_ids': [doc['id']],
                'vector_threshold': 0, 'keyword_threshold': 0, 'disable_keywords_match': False,
                'disable_vector_match': True, 'skip_context_enrichment': True}
        status, response = api.request('/knowledge-bases/' + KB + '/hybrid-search',
            json.dumps(body, ensure_ascii=False).encode(), {'Content-Type': 'application/json'})
        hits = response.get('data') or []
        check.update(chunk_total=chunk_total, probe_query=query, retrieval_mode='keyword', retrieval_http_status=status,
                     hit_ids=[h.get('knowledge_id') for h in hits])
        check['retrieval_verified'] = status == 200 and any(h.get('knowledge_id') == doc['id'] for h in hits)
        checks.append(check)
    save(check_path, [c for c in previous_checks if c['id'] not in ids] + checks)
    print('Indexed retrieval', sum(c['retrieval_verified'] for c in checks), '/', len(checks), flush=True)


if __name__ == '__main__':
    main()
