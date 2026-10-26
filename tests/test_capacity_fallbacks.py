import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree as E
from PIL import Image
from docflow.common import WorkflowError
from docflow.documents import page_image_units, safe_office_copy, trim_full_width_style_tail


class CapacityFallbackTests(unittest.TestCase):
    def test_large_image_tiles_preserve_pixels_and_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            import random
            root = Path(temp).resolve(); path = root / 'page.png'
            original = Image.frombytes('RGB', (1800, 1800), random.Random(1).randbytes(1800 * 1800 * 3))
            original.save(path)
            self.assertGreater(path.stat().st_size, 8 * 1024 * 1024)
            units = page_image_units(path, 'PDF第1页', 'text')
            self.assertGreater(len(units), 1)
            restored = Image.new('RGB', original.size)
            import ast
            for unit in units:
                box = ast.literal_eval(unit['label'].split('像素区域')[1])
                with Image.open(root / unit['image']) as tile:
                    self.assertEqual(tile.tobytes(), original.crop(box).tobytes())
                    restored.paste(tile, box[:2])
                self.assertLessEqual((root / unit['image']).stat().st_size, 8 * 1024 * 1024)
            self.assertEqual(restored.tobytes(), original.tobytes())

    def test_full_width_style_tail_preserves_values_and_merges(self):
        tree = E.fromstring(b'<worksheet><dimension ref="A1:XFD180"/><sheetData><row><c r="A1"><v>1</v></c><c r="F180"><f>1+1</f></c><c r="XFD134" s="1"/></row></sheetData><mergeCells><mergeCell ref="A1:F1"/></mergeCells></worksheet>')
        self.assertIsNotNone(trim_full_width_style_tail(tree))
        self.assertEqual(tree.find('dimension').get('ref'), 'A1:F180')
        self.assertEqual([n.get('r') for n in tree.iter('c')], ['A1', 'F180'])
        tree.find('dimension').set('ref','A1:XFD180')
        E.SubElement(tree,'drawing')
        self.assertIsNone(trim_full_width_style_tail(tree))

    def test_external_image_requires_valid_embedded_fallback(self):
        relns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        for embedded in [True, False]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); source = root/'sample.xlsx'
                with zipfile.ZipFile(source,'w') as z:
                    z.writestr('xl/drawings/_rels/drawing1.xml.rels', f'<Relationships><Relationship Id="rId1" Type="{relns}/image" Target="../media/p.png"/><Relationship Id="rId2" Type="{relns}/image" TargetMode="External" Target="https://example.invalid/p.png"/></Relationships>')
                    embed = 'r:embed="rId1"' if embedded else ''
                    z.writestr('xl/drawings/drawing1.xml',f'<drawing xmlns:r="{relns}"><blip {embed} r:link="rId2"/></drawing>')
                    z.writestr('xl/media/p.png',b'embedded fixture')
                before = source.read_bytes()
                if not embedded:
                    with self.assertRaises(WorkflowError):safe_office_copy(source,root)
                else:
                    target,notes,_ = safe_office_copy(source,root)
                    with zipfile.ZipFile(target) as z:
                        self.assertNotIn(b'TargetMode="External"',z.read('xl/drawings/_rels/drawing1.xml.rels'))
                        self.assertNotIn(b'link=',z.read('xl/drawings/drawing1.xml'))
                        self.assertIn(b'embed=',z.read('xl/drawings/drawing1.xml'))
                    self.assertTrue(notes)
                self.assertEqual(source.read_bytes(),before)
