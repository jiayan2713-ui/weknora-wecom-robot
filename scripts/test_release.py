"""Run existing isolated unit suites; no production configuration is loaded."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    requested = sys.argv[1:]
    groups = requested or ['sync', 'history', 'docflow']
    allowed = {'sync': ROOT / 'implementation/weknora-sync',
               'history': ROOT / 'implementation/robot5-history-collector', 'docflow': ROOT / 'tests'}
    if any(group not in allowed for group in groups):
        raise SystemExit('Choose sync, history, docflow')
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(allowed['sync']))
    results = []
    for group in groups:
        folder = allowed[group]
        if group == 'history':
            sys.path.insert(0, str(folder))
            import collector
            import test_collector
            artifact_available = Path(collector.NODE).is_file() and (folder / 'node_modules/@oai/artifact-tool').exists()
            if not artifact_available:
                for name in ['test_real_excel_long_unicode_formula_like_text_and_tamper',
                             'test_reasoning_export_full_text_and_cell_limit']:
                    for obj in vars(test_collector).values():
                        if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and name in obj.__dict__:
                            setattr(obj, name, unittest.skip('Optional Codex artifact backend is not distributed; openpyxl is the standalone backend.')(getattr(obj, name)))
        loader = unittest.TestLoader()
        suite = loader.discover(str(folder), pattern='test_*.py', top_level_dir=str(ROOT) if group == 'docflow' else str(folder))
        runner = unittest.TextTestRunner(verbosity=1)
        result = runner.run(suite)
        results.append({'group': group, 'python': sys.executable, 'run': result.testsRun, 'failures': len(result.failures),
                        'errors': len(result.errors), 'skipped': len(result.skipped),
                        'skip_reasons': sorted({reason for _, reason in result.skipped}),
                        'success': result.wasSuccessful()})
    report = ROOT / 'checks/unit-tests.json'
    previous = {row['group']: row for row in json.loads(report.read_text())} if report.exists() else {}
    previous.update({row['group']: row for row in results})
    report.write_text(json.dumps(list(previous.values()), ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return all(r['success'] for r in results)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
