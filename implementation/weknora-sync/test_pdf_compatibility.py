import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter
from pdf_compatibility import compatible_pdf, migration_retries


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'state_dir': self.temp.name}
        self.digest = 'a' * 64
        self.root = Path(self.temp.name) / 'pdf-compatibility' / self.digest
        self.root.mkdir(parents=True)
        self.pdf = self.root / 'compatible.pdf'
        writer = PdfWriter()
        writer.add_blank_page(100, 200)
        writer.write(self.pdf)
        self.evidence = {'source_sha256': self.digest,
                         'compatible_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
                         'pages': [{'page': 1, 'same_rect': True,
                                    'same_pixels_108dpi': True, 'same_text': True}]}
        self.save()

    def save(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.evidence))

    def test_unregistered_source_has_no_effect(self):
        self.assertEqual(compatible_pdf(self.cfg, 'b' * 64), (None, None))

    def test_valid_derivative_and_changed_bytes(self):
        self.assertEqual(compatible_pdf(self.cfg, self.digest)[0], self.pdf)
        self.pdf.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            compatible_pdf(self.cfg, self.digest)

    def test_missing_page_evidence_and_page_count_rejected(self):
        self.evidence['pages'][0]['same_text'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'coverage'):
            compatible_pdf(self.cfg, self.digest)
        self.evidence['pages'][0]['same_text'] = True
        self.evidence['pages'].append({**self.evidence['pages'][0], 'page': 2})
        self.save()
        with self.assertRaisesRegex(ValueError, 'page_mismatch'):
            compatible_pdf(self.cfg, self.digest)

    def test_migration_requests_only_matching_paused_file_without_changing_mapping(self):
        row = {'path': 'x.pdf', 'state': 'parse_blocked_hold',
               'blocked_reason': 'INVALID_DOCUMENT', 'blocked_source_hash': self.digest,
               'remote_ids': []}
        before = json.dumps(row, sort_keys=True)
        requests = migration_retries(self.cfg, {'source': row})
        self.assertEqual(requests[0]['source_hash'], self.digest)
        self.assertEqual(json.dumps(row, sort_keys=True), before)
        for state in ('active', 'deleted', 'learning'):
            self.assertEqual(migration_retries(self.cfg, {'source': {**row, 'state': state}}), [])
        self.assertEqual(migration_retries(self.cfg, {'source': {**row, 'blocked_source_hash': 'b' * 64}}), [])


if __name__ == '__main__':
    unittest.main()
