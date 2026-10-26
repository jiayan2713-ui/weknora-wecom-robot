"""Create a backed-up candidate from an already inspected compression output."""
import argparse
import json
from pathlib import Path

from compression_versions import stage
from reconcile import hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--relative', required=True)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--validation', required=True)
    args = parser.parse_args()
    report_path = Path(args.validation).resolve()
    report = json.loads(report_path.read_text())
    candidate = Path(args.candidate).resolve()
    if candidate != Path(report['output']).resolve() or candidate.stat().st_size != report['output_bytes']:
        raise ValueError('candidate_report_identity_mismatch')
    expected = report.get('render_output_pages')
    if not expected or report.get('render_original_pages') != expected or \
            report.get('render_native_text_difference_pages') != [] or \
            report.get('visual_review_pages') != list(range(1, expected + 1)) or \
            not report.get('text_tables_and_layout_xml_byte_identical'):
        raise ValueError('candidate_render_validation_incomplete')
    validation = {**report, 'status': 'passed', 'visual_review_complete': True,
                  'output_sha256': hashes(candidate)['sha256'], 'report_path': str(report_path)}
    cfg = {'source_root': args.source_root, 'compression_registry': args.registry}
    row = stage(cfg, args.relative, candidate, validation)
    print(json.dumps({'phase': row['phase'], 'relative': row['relative'],
                      'backup_relative': row['backup_relative'], 'output_sha256': row['output_sha256'],
                      'cloud_file_id': row['cloud_file_id']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
