from datetime import datetime, timedelta
import io
import json
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from zoneinfo import ZoneInfo

from PIL import Image

from compress_documents import office, compress, jpeg
from compression_failures import record_failure, retry_gate, failure_path
from compression_reporting import publish_daily
from compression_batch import run
from reconcile import hashes
import test_compression_batch as batch_tests


class XLSXTests(unittest.TestCase):
    def test_large_high_entropy_image_encodes_without_optimized_buffer_failure(self):
        raw = io.BytesIO()
        Image.frombytes('RGB', (1024, 1024), random.Random(42).randbytes(1024 * 1024 * 3)).save(raw, format='PNG')
        result = jpeg(raw.getvalue(), 85)
        self.assertLess(len(result), len(raw.getvalue()))
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (1024, 1024))
    def test_xlsx_keeps_formulas_styles_sheets_and_drawing_geometry_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'book.xlsx', Path(directory) / 'out.xlsx'
            raw = io.BytesIO()
            Image.effect_noise((200, 150), 10).convert('RGB').save(raw, format='PNG')
            parts = {'xl/workbook.xml': '<workbook><sheets><sheet name="Data"/></sheets></workbook>',
                     'xl/worksheets/sheet1.xml': '<worksheet><row><c r="A1"><v>2</v></c><c r="B1"><f>A1*3</f><v>6</v></c></row></worksheet>',
                     'xl/styles.xml': '<styleSheet><font name="Arial"/></styleSheet>',
                     'xl/drawings/drawing1.xml': '<drawing><anchor x="100" y="200"/></drawing>',
                     'xl/drawings/_rels/drawing1.xml.rels': '<Relationships><Relationship Target="../media/image1.png"/></Relationships>',
                     '[Content_Types].xml': '<Types><Default Extension="png" ContentType="image/png"/></Types>',
                     'xl/media/image1.png': raw.getvalue()}
            with zipfile.ZipFile(source, 'w') as archive:
                for name, value in parts.items():
                    archive.writestr(name, value)
            original = source.read_bytes()
            report = compress(source, output)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(report['changed_images'], 1)
            with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
                for name in parts:
                    if not name.endswith('.rels') and name != '[Content_Types].xml' and '/media/' not in name:
                        self.assertEqual(before.read(name), after.read(name))
                self.assertIn('xl/media/image1_compressed.jpg', after.namelist())
                self.assertIn(b'../media/image1_compressed.jpg', after.read('xl/drawings/_rels/drawing1.xml.rels'))

    def test_transparent_xlsx_image_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'book.xlsx', Path(directory) / 'out.xlsx'
            raw = io.BytesIO()
            Image.new('RGBA', (20, 20), (10, 20, 30, 100)).save(raw, format='PNG')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('xl/media/transparent.png', raw.getvalue())
            report = office(source, output, 85)
            self.assertEqual(report['changed_images'], 0)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('xl/media/transparent.png'), raw.getvalue())


class ImprovementBatchTests(unittest.TestCase):
    setUp = batch_tests.BatchTests.setUp
    office = batch_tests.BatchTests.office
    metadata = batch_tests.BatchTests.metadata
    candidate = batch_tests.BatchTests.candidate
    def test_deterministic_failure_is_not_recompressed_or_backed_up_again(self):
        with patch('compression_batch.LIMIT', 1), patch('compression_batch.compress', side_effect=ValueError('target_size_not_reached_without_resizing')) as operation:
            first = run(self.cfg)
            second = run(self.cfg)
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(first['files'][0]['phase'], 'failed')
            self.assertEqual(second['files'][0]['phase'], 'compression_blocked')
            self.assertEqual(second['status'], 'needs_attention')
            self.assertEqual(len(list((self.registry / 'jobs').glob('*'))), 1)
            self.assertTrue(Path(second['files'][0]['backup']).exists())
            with patch('compression_failures.algorithm_version', return_value='new-compressor'):
                run(self.cfg)
            self.assertEqual(operation.call_count, 2)
            self.office(self.source)
            run(self.cfg)
            self.assertEqual(operation.call_count, 3)
            run(self.cfg, retry_failed=True)
            self.assertEqual(operation.call_count, 4)

    def test_transient_retry_waits_then_stops_at_limit(self):
        digest = hashes(self.source)['sha256']
        relative = str(self.source.relative_to(self.root))
        for attempt in range(3):
            record_failure(self.cfg, self.registry, relative, digest, self.source, self.home / 'candidate', OSError('temporary_io'))
            held = retry_gate(self.cfg, self.registry, relative, digest)
            self.assertEqual(held['attempts'], attempt + 1)
            self.assertEqual(held['phase'], 'compression_blocked' if attempt == 2 else 'retry_deferred')
            future = datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(days=20)
            self.assertEqual(retry_gate(self.cfg, self.registry, relative, digest, now=future) is None, attempt < 2)

    def test_corrupt_failure_record_is_reported_without_aborting_other_files(self):
        digest = hashes(self.source)['sha256']
        relative = str(self.source.relative_to(self.root))
        path = failure_path(self.registry, relative, digest)
        path.parent.mkdir()
        path.write_text('{invalid')
        other = self.source.with_name('other.docx')
        self.office(other)
        with patch('compression_batch.LIMIT', 1), patch('compression_batch.client_metadata', side_effect=self.metadata):
            result = run(self.cfg)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual({f['phase'] for f in result['files']}, {'failed', 'prepared'})

    def test_small_and_unsupported_files_are_reported_without_backup(self):
        (self.scope / 'old.xls').write_bytes(b'old Excel')
        with patch('compression_batch.LIMIT', 1), patch('compression_batch.prepare') as operation:
            self.cfg['excluded_paths'] = [str(self.source.relative_to(self.root))]
            result = run(self.cfg)
        operation.assert_not_called()
        self.assertEqual(result['summary']['格式不支持'], 1)
        self.assertEqual(result['summary']['其他跳过'], 1)
        self.assertFalse((self.registry / 'jobs').exists())
        self.cfg.pop('excluded_paths')
        result = run(self.cfg)
        self.assertEqual(result['summary']['无需压缩'], 1)
        self.assertTrue((self.registry / '每日结果_最新.md').exists())

    def test_daily_waiting_result_is_updated_when_recovery_confirms(self):
        row = self.candidate()
        self.cfg['acceptance_mode'] = 'client_sync'
        with patch('compression_batch.client_status', return_value={'ready': False, 'reason': 'client_not_running'}):
            daily = run(self.cfg, apply=True)
        self.assertEqual(daily['summary']['等待处理'], 1)
        def metadata(*_):
            result = self.metadata()
            if hashes(self.source)['sha256'] == row['output_sha256']:
                result.update(history_cnt=2, seq_id='2')
            return result
        with patch('compression_batch.client_metadata', side_effect=metadata):
            run(self.cfg, apply=True, resume_only=True)
        latest = json.loads((self.registry / '每日结果_最新.json').read_text())
        self.assertEqual(latest['summary']['同步成功'], 1)
        self.assertEqual(latest['summary']['等待处理'], 0)
        self.assertEqual(latest['status'], 'success')


if __name__ == '__main__':
    unittest.main()
