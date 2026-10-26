from pathlib import Path
import tempfile
import unittest
import zipfile

from compact_xlsx import compact
from reconcile import hashes


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fixture(self, extra_content='', merge=''):
        path = self.root / 'source.xlsx'
        sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<dimension ref="A1:B1000"/><sheetData>'
                 '<row r="1"><c r="A1" s="1" t="inlineStr"><is><t>费用</t></is></c></row>'
                 '<row r="2"><c r="B2" s="2"><f>A3*2</f><v>0</v></c></row>'
                 '<row r="3"><c r="A3"><v>0</v></c></row>' +
                 ''.join('<row r="'+str(n)+'" s="6"><c r="B'+str(n)+'" s="6"/></row>' for n in range(4, 501)) +
                 extra_content + '</sheetData>' + merge + '</worksheet>')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('xl/worksheets/sheet2.xml', sheet)
            archive.writestr('xl/styles.xml', '<styles>unchanged</styles>')
            archive.writestr('xl/media/image1.png', b'unchanged-image')
        return path

    def test_only_empty_tail_removed_and_formula_cache_and_source_preserved(self):
        source = self.fixture()
        original = hashes(source)['sha256']
        destination = self.root / 'private/compact.xlsx'
        proof = compact(source, destination, self.root / 'proof.json', threshold_bytes=0)
        self.assertEqual(hashes(source)['sha256'], original)
        self.assertTrue(proof['all_value_formula_cache_records_equal'])
        self.assertEqual(proof['sheets'][0]['nonempty_cells'], 3)
        self.assertEqual(proof['sheets'][0]['removed_empty_tail_rows'], 496)
        with zipfile.ZipFile(destination) as archive:
            sheet = archive.read('xl/worksheets/sheet2.xml').decode()
            self.assertIn('<f>A3*2</f><v>0</v>', sheet)
            self.assertIn('r="4"', sheet)
            self.assertNotIn('r="5"', sheet)
            self.assertEqual(archive.read('xl/media/image1.png'), b'unchanged-image')

    def test_real_tail_content_is_never_discarded(self):
        source = self.fixture('<row r="700"><c r="A700"><v>9</v></c></row>')
        with self.assertRaisesRegex(ValueError, 'real_content'):
            compact(source, self.root/'private.xlsx', self.root/'proof.json', threshold_bytes=0)

    def test_large_merge_extent_blocks_compaction(self):
        source = self.fixture(merge='<mergeCells><mergeCell ref="A1:B800"/></mergeCells>')
        with self.assertRaisesRegex(ValueError, 'visible_extent'):
            compact(source, self.root/'private.xlsx', self.root/'proof.json', threshold_bytes=0)


if __name__ == '__main__':
    unittest.main()
