import io
from pathlib import Path
import tempfile
import unittest

import fitz
from PIL import Image

from compress_documents import pdf


class PDFCompressionTests(unittest.TestCase):
    def check_image(self, image_format, transparent=False):
        image = Image.new('RGBA' if transparent else 'RGB', (360, 240),
                          (80, 120, 160, 128) if transparent else (80, 120, 160))
        if not transparent:
            image.putdata([(60 + x // 3 + (x * 7 + y * 11) % 12,
                            80 + y // 3 + (x * 11 + y * 7) % 12, 140)
                           for y in range(240) for x in range(360)])
        raw = io.BytesIO()
        image.save(raw, format=image_format, compress_level=0)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'source.pdf', Path(directory) / 'out.pdf'
            with fitz.open() as document:
                first = document.new_page(width=400, height=300)
                first.insert_text((15, 25), 'Retain searchable text')
                xref = first.insert_image(fitz.Rect(20, 40, 380, 280), stream=raw.getvalue())
                second = document.new_page(width=400, height=300)
                second.insert_image(fitz.Rect(20, 40, 380, 280), xref=xref)
                document.save(source)
            original = source.read_bytes()
            report = pdf(source, output, 85)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(report['all_pages_rendered'])
            self.assertEqual(report['pages'], 2)
            with fitz.open(output) as document:
                info = document[0].get_images(full=True)[0]
                self.assertEqual(info[2:4], (360, 240))
                if transparent:
                    self.assertNotEqual(info[1], 0)
                else:
                    self.assertEqual(document.extract_image(info[0])['ext'], 'jpeg')
            return report, len(original), output.stat().st_size

    def test_opaque_png_compresses_shared_image_and_preserves_document(self):
        report, before, after = self.check_image('PNG')
        self.assertEqual(report['changed_images'], 1)
        self.assertLess(after, before)

    def test_transparent_png_retains_mask(self):
        report, _, _ = self.check_image('PNG', transparent=True)
        self.assertEqual(report['changed_images'], 0)

    def test_jpeg_still_passes_document_checks(self):
        self.check_image('JPEG')


if __name__ == '__main__':
    unittest.main()
