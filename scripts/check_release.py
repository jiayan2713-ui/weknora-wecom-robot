"""Offline release integrity check; never starts business services."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def check():
    errors = []
    copied = json.loads((ROOT / 'checks/source-manifest.json').read_text())['files']
    for row in copied:
        path = ROOT / row['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.get('release_sha256', row['sha256']):
            errors.append({'kind': 'copy_mismatch', 'path': row['path']})
    owned_python = []
    for folder in ['docflow', 'implementation', 'scripts', 'tests']:
        for path in (ROOT / folder).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            owned_python.append(path)
            try:
                ast.parse(path.read_text(), filename=str(path))
            except (SyntaxError, UnicodeError) as exc:
                errors.append({'kind': 'python_syntax', 'path': str(path.relative_to(ROOT)), 'error': str(exc)})
    for path in (ROOT / 'config').rglob('*.json'):
        try:
            json.loads(path.read_text())
        except ValueError:
            errors.append({'kind': 'json_invalid', 'path': str(path.relative_to(ROOT))})
    # Only authored release docs are checked; upstream/historical docs retain their original context.
    authored = [ROOT / 'README.md', ROOT / 'THIRD_PARTY_NOTICES.md']
    authored += list((ROOT / 'docs').glob('*.md'))
    authored = [p for p in authored if p.name != 'original-document-workflow.md']
    for path in authored:
        for target in re.findall(r'(?<!!)\[[^\]]+\]\(([^)]+)\)', path.read_text()):
            if '://' in target or target.startswith('#'):
                continue
            target = target.split('#', 1)[0]
            if not (path.parent / target).exists():
                errors.append({'kind': 'broken_document_link', 'path': str(path.relative_to(ROOT)), 'target': target})
    files = []
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in {'.git', '__pycache__', '.venv', 'node_modules'} for part in relative.parts):
            continue
        if path.is_symlink():
            errors.append({'kind': 'symlink', 'path': str(relative)})
            continue
        if not path.is_file():
            continue
        if path.name in {'release-check.json', '检查报告.md'} or path.name.endswith('.raw.log'):
            continue
        data = path.read_bytes()
        if len(data) > 100 * 1024 * 1024:
            errors.append({'kind': 'oversized_file', 'path': str(relative)})
        if path.name in {'.env', 'unlock.key', 'api-key.txt', 'model-env.json', 'crm-config.json', 'wecom-connection.json'}:
            errors.append({'kind': 'runtime_private_file', 'path': str(relative)})
        if relative.parts[0] != 'platform' and path.suffix in {'.sqlite', '.sqlite3', '.db', '.dump', '.enc', '.zip', '.xlsx', '.pdf'}:
            errors.append({'kind': 'unexpected_runtime_artifact', 'path': str(relative)})
        files.append({'path': str(relative), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    result = {'status': 'passed' if not errors else 'failed', 'copied_files_verified': len(copied),
              'python_files_syntax_checked': len(owned_python), 'release_files': len(files),
              'total_bytes': sum(f['bytes'] for f in files), 'errors': errors, 'files': files,
              'limits': ['Static verification only; no model, CRM, Docker daemon, or WeCom login calls.',
                         'Secret-value comparison is recorded separately by the preparation session.',
                         'No new-machine or production end-to-end acceptance is implied.']}
    (ROOT / 'checks/release-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=False, indent=2))
    return not errors


if __name__ == '__main__':
    sys.exit(0 if check() else 1)
