"""Simulate Git staging in a temporary repository; never commits or pushes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='robot5-git-preview-') as directory:
        subprocess.run(['git', 'init', '--quiet', directory], check=True)
        command = ['git', '-c', 'core.quotepath=false', '--git-dir', directory + '/.git', '--work-tree', str(ROOT)]
        stage = subprocess.run(command + ['add', '--all'], cwd=ROOT, capture_output=True, text=True)
        names = set(subprocess.check_output(command + ['ls-files', '-z'], cwd=ROOT).decode().split('\0')) - {''}
        ignored = subprocess.check_output(command + ['ls-files', '--others', '--ignored', '--exclude-standard'], cwd=ROOT).decode().splitlines()
        unexpected = [name for name in ignored if '__pycache__' not in name and not name.endswith('.raw.log')]
        source = json.loads((ROOT / 'checks/source-manifest.json').read_text())['files']
        missing = [row['path'] for row in source if row['path'] not in names]
        sensitive = ['.env', '.local/weknora-sync/config.json', 'runtime/history/history.sqlite3',
                     'outputs/问答.xlsx', 'private/unlock.key', 'config/example.local.json']
        result = subprocess.run(command + ['check-ignore', '--stdin'], cwd=ROOT,
                                input='\n'.join(sensitive) + '\n', text=True, capture_output=True)
        protected = result.stdout.splitlines()
        passed = stage.returncode == 0 and not unexpected and not missing and set(protected) == set(sensitive)
        report = {'status': 'passed' if passed else 'failed', 'candidate_file_count': len(names),
                  'unexpected_ignored_paths': unexpected, 'copied_source_missing_from_stage': missing,
                  'sensitive_path_examples_protected': protected,
                  'note': 'Temporary Git index only; no .git, commit, remote or push in the release directory.'}
        (ROOT / 'checks/git-preview.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return passed


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
