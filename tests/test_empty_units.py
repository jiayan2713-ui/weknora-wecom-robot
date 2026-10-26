import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image
from docflow.common import WorkflowError
from docflow.config import configuration
from docflow.parser import confirmed_blank_unit, transcribe


class EmptyUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'prepared').mkdir()
        self.work = self.root / 'u0001'
        self.work.mkdir()
        Image.new('RGB', (40, 40), 'white').save(self.root / 'prepared/page.png')
        self.unit = {'id': 'u0001', 'text': '', 'image': 'page.png', 'label': '第1页'}
        self.prompt = Path(configuration()['parser_prompt']).read_text()

    def tearDown(self):
        self.temp.cleanup()

    def test_blank_page_requires_white_pixels_and_no_text(self):
        client = Mock()
        result = transcribe(client, self.work, self.unit, '.docx', self.prompt)
        self.assertEqual(result['body'], '')
        self.assertTrue(result['issues'])
        client.call.assert_not_called()
        self.unit['text'] = '浅色文字'
        self.assertFalse(confirmed_blank_unit(self.work, self.unit, '.docx'))
        self.unit['text'] = ''
        image = Image.new('RGB', (40, 40), 'white')
        image.putpixel((1, 1), (254, 255, 255))
        image.save(self.root / 'prepared/page.png')
        self.assertFalse(confirmed_blank_unit(self.work, self.unit, '.docx'))

    def test_empty_sheet_metadata_and_tiny_image(self):
        Image.new('RGB', (3600, 2), 'white').save(self.root / 'prepared/page.png')
        marker = '原始值、公式及缓存（不得以渲染器重算代替）：\n'
        self.unit['text'] = marker + json.dumps({'cells': [], 'name': 'Sheet2'})
        client = Mock()
        self.assertEqual(transcribe(client, self.work, self.unit, '.xlsx', self.prompt)['body'], '')
        client.call.assert_not_called()
        self.unit['text'] = marker + json.dumps({'cells': [{'value': '隐藏值'}]})
        self.assertFalse(confirmed_blank_unit(self.work, self.unit, '.xlsx'))
        self.unit['text'] = marker + '{invalid'
        self.assertFalse(confirmed_blank_unit(self.work, self.unit, '.xlsx'))

    def test_notes_only_output_uses_separate_bounded_recovery(self):
        self.unit['text'] = '原文标签'
        client = Mock()
        client.call.side_effect = ['# 解析记录（非原文）\n原文标签', '# 解析记录（非原文）\n原文标签',
                                   '# 正文\n原文标签\n# 解析记录（非原文）\n遮挡说明', '无需修改']
        result = transcribe(client, self.work, self.unit, '.docx', self.prompt)
        self.assertIn('原文标签', result['body'])
        self.assertNotIn('遮挡说明', result['body'])
        self.assertIn('遮挡说明', result['notes'])
        self.assertEqual([c.args[1] for c in client.call.call_args_list],
                         ['draft', 'body-recovery', 'body-format-recovery-v1', 'review'])

    def test_still_empty_does_not_pass_or_retry_forever(self):
        self.unit['text'] = '必须保留的原文'
        client = Mock()
        client.call.return_value = '# 解析记录（非原文）\n没有正文'
        with self.assertRaises(WorkflowError) as error:
            transcribe(client, self.work, self.unit, '.docx', self.prompt)
        self.assertEqual(error.exception.code, 'EMPTY_TRANSCRIPTION')
        self.assertEqual(client.call.call_count, 3)
