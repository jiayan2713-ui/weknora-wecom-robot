import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from docflow.common import WorkflowError, artifact, read_json
from docflow.config import configuration
from docflow.documents import safe_office_copy
from docflow.model import ModelClient
from docflow.parser import parse_document, markdown, reviewed_units, split_notes
from docflow.storage import Store


def response(text="测试正文"):
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": text}}], "usage": {"total_tokens": 5}}, ensure_ascii=False).encode(), 200, "test-request"


class ParserTests(unittest.TestCase):
    def test_durable_response_and_attempt_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            calls = []
            def transport(payload):
                calls.append(payload)
                return response()
            client = ModelClient(configuration(), transport, sleep=lambda _: None)
            self.assertEqual(client.call(root, "draft", []), "测试正文")
            self.assertEqual(ModelClient(configuration(), transport).call(root, "draft", []), "测试正文")
            self.assertEqual(len(calls), 1)
            calls.clear()
            def broken(_):
                calls.append(1)
                raise urllib.error.URLError("offline")
            client = ModelClient(configuration(), broken, sleep=lambda _: None)
            with self.assertRaises(WorkflowError):
                client.call(root, "failure", [])
            with self.assertRaises(WorkflowError):
                client.call(root, "failure", [])
            self.assertEqual(len(calls), 1)  # Unknown outcomes never automatically resend.
            with patch('docflow.model.time.time',return_value=10**12):
                self.assertRaises(WorkflowError, ModelClient(configuration(), broken, sleep=lambda _: None, retry_terminal=True).call, root, "failure", [])
            self.assertEqual(len(calls), 3)  # An explicit retry still inherits the original cap.

    def test_received_response_disk_failure_does_not_recall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            calls=[]
            def transport(_):
                calls.append(1)
                return response()
            client=ModelClient(configuration(), transport)
            with patch('docflow.model.atomic_bytes', side_effect=OSError('disk full')):
                with self.assertRaises(WorkflowError) as error:
                    client.call(root, 'draft', [])
            self.assertEqual(error.exception.code, 'RESPONSE_STORAGE_FAILED')
            self.assertEqual(client.call(root, 'draft', []), '测试正文')
            self.assertEqual(len(calls), 1)

    def test_cache_reuses_neutral_input_and_preserves_working(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();store=Store(root);config=configuration({"model_review": True, "parser_workers": 1})
            source=root/'a.csv';source.write_text('列\n内容\n')
            document={'name':'a.csv','extension':'.csv','source':artifact(root,source)}
            prompt=Path(config['parser_prompt']).read_text();calls=[]
            def transport(payload):
                calls.append(payload)
                return response('无需修改' if '文档转写核对员' in str(payload) else '# 正文\n内容')
            parsed=parse_document(store,document,config,prompt,lambda:None,ModelClient(config,transport))
            other={'name':'different.csv','extension':'.csv','source':artifact(root,source)}
            again=parse_document(store,other,config,prompt,lambda:None,ModelClient(config,transport))
            self.assertEqual(len(calls),2)
            self.assertTrue(other['cache_hit'])
            text=markdown(other,again)
            self.assertIn('different.csv',text)
            self.assertEqual(reviewed_units(text,other,again)[0]['body'],'# 正文\n内容')
            with self.assertRaises(WorkflowError):
                reviewed_units(text.replace('<!-- docflow:u0001 -->',''),other,again)

    def test_unsafe_office_stops_before_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();file=root/'sample.docx'
            with zipfile.ZipFile(file,'w') as archive:
                archive.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Type="x/image" TargetMode="External" Target="https://example.invalid/image.png"/></Relationships>')
            with self.assertRaises(WorkflowError) as error:
                safe_office_copy(file,root)
            self.assertEqual(error.exception.code,'UNSAFE_OFFICE')

    def test_notes_in_code_fences_are_not_removed(self):
        text='```text\n# 解析记录（非原文）\n原文\n```\n\n# 核对记录（非原文）\n疑点'
        body,notes=split_notes(text)
        self.assertIn('原文',body)
        self.assertEqual(notes,'# 核对记录（非原文）\n疑点')


if __name__=='__main__':
    unittest.main()
