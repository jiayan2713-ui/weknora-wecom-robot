import tempfile
import unittest
from pathlib import Path

from docflow.common import WorkflowError
from docflow.config import configuration
from docflow.documents import prepare, SKIPPED_MEDIA
from tests.fixtures import workflow, approve, candidate_files, SMALL


class TextMediaTests(unittest.TestCase):
    def test_encodings_and_line_locations(self):
        text = '10:00 张三：费用10万元。\r\n\r\n办理周期3个月。\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for encoding in ['utf-8', 'utf-8-sig', 'utf-16', 'gb18030']:
                source = root / 'chat.txt'
                source.write_bytes(text.encode(encoding))
                units = prepare(source, root / encoding, configuration())
                self.assertEqual(units[0]['text'], text)
                self.assertEqual(units[0]['label'], '原文件第1–3行')
                self.assertIsNone(units[0]['image'])

    def test_chunking_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source = root / 'chat.txt'
            text = ''.join(f'{i} 客户：测试\n' for i in range(405))
            source.write_text(text)
            units = prepare(source, root / 'chunks', configuration())
            self.assertEqual([u['label'] for u in units], ['原文件第1–200行', '原文件第201–400行', '原文件第401–405行'])
            self.assertEqual(''.join(u['text'] for u in units), text)
            for data in [b'\xff', b'a\x00b', ('长' * 12001).encode()]:
                source.write_bytes(data)
                with self.assertRaises(WorkflowError):
                    prepare(source, root / 'invalid', configuration())
            source.write_bytes(b'')
            self.assertEqual(prepare(source, root / 'empty', configuration())[0]['text'], '')

    def test_txt_to_export_with_all_media_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); w, calls = workflow(root / 'state')
            source = root / 'chat.txt'
            source.write_text('项目A的费用为10万元。办理周期为3个月。')
            files = [str(source)]
            for ext in sorted(SKIPPED_MEDIA):
                media = root / ('sample' + ext.upper())
                media.write_bytes(b'archive only')
                files.append(str(media))
            status = w.execute('start', {'request_id': 'txt-start', 'project': 'TXT测试', 'sources': files, 'config': SMALL})
            self.assertEqual(status['phase'], 'awaiting_review', status)
            self.assertEqual(sum(d['state'] == 'skipped' for d in status['documents']), len(SKIPPED_MEDIA))
            self.assertEqual(len(calls), 2)
            status = approve(w, status)
            status = w.execute('submit', candidate_files(status))
            self.assertEqual(status['phase'], 'completed', status)
            self.assertEqual(len(calls), 2)
