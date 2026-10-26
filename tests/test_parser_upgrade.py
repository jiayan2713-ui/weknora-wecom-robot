import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docflow.common import WorkflowError, artifact, digest, read_json, write_json
from docflow.config import configuration
from docflow.migration import upgrade_document
from docflow.model import ModelClient
from docflow.parser import parse_document
from docflow.storage import Store
from tests.test_parser import response


class ParserUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.config = configuration({"model_review": True, "parser_workers": 1})
        self.prompt = Path(self.config['parser_prompt']).read_text()
        self.source = self.root / 'a.txt'
        self.source.write_text('原文\n' * 201)
        self.doc = {'id': 'd0001', 'name': 'a.txt', 'extension': '.txt', 'source': artifact(self.root, self.source)}

    def tearDown(self):
        self.temp.cleanup()

    def test_completed_result_retained_without_model_recall(self):
        calls = []
        def transport(payload):
            calls.append(1)
            return response('无需修改' if '文档转写核对员' in str(payload) else '真实正文')
        with patch('docflow.parser.cache_identity', return_value='old'):
            before = parse_document(self.store, self.doc, self.config, self.prompt, lambda: None, ModelClient(self.config, transport))
        snapshots = {p: digest(p) for p in self.root.rglob('*') if p.is_file()}
        with patch('docflow.migration.cache_identity', return_value='new'):
            upgrade_document(self.store, self.doc, self.config, self.prompt, 'upgrade1')
        with patch('docflow.parser.cache_identity', return_value='new'):
            after = parse_document(self.store, self.doc, self.config, self.prompt, lambda: None, ModelClient(self.config, transport))
        self.assertEqual(before, after)
        self.assertEqual(len(calls), 4)
        for p, sha in snapshots.items():self.assertEqual(digest(p), sha)
        with patch('docflow.parser.cache_identity', return_value='unapproved'):
            with self.assertRaises(WorkflowError):
                parse_document(self.store, self.doc, self.config, self.prompt, lambda: None)

    def test_partial_result_forks_preserves_budget_and_completed_units(self):
        calls = []
        def transport(payload):
            calls.append(payload)
            if len(calls) == 3:
                raise WorkflowError('MODEL_TRUNCATED', 'fixture')
            return response('无需修改' if '文档转写核对员' in str(payload) else '真实正文')
        with patch('docflow.parser.cache_identity', return_value='old'):
            with self.assertRaises(WorkflowError):
                parse_document(self.store, self.doc, self.config, self.prompt, lambda: None, ModelClient(self.config, transport))
        old_version = self.doc['cache_version']
        old_files = {p: digest(p) for p in self.root.rglob('*') if p.is_file()}
        with patch('docflow.migration.cache_identity', return_value='new'):
            upgrade_document(self.store, self.doc, self.config, self.prompt, 'upgrade2')
        self.assertNotEqual(old_version, self.doc['cache_version'])
        self.assertEqual(list(self.doc['unit_refs']), ['u0001'])
        copied_call = self.root / 'parse-cache/new' / self.doc['cache_version'] / 'u0002/draft/call.json'
        self.assertEqual(len(read_json(copied_call)['attempts']), 1)
        # Explicit retry of fixture terminal error keeps the old count; u0001 is not called again.
        with patch('docflow.parser.cache_identity', return_value='new'):
            result = parse_document(self.store, self.doc, self.config, self.prompt, lambda: None,
                                    ModelClient(self.config, transport, retry_terminal=True))
        self.assertEqual(len(result['units']), 2)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(read_json(copied_call)['attempts']), 2)
        for p, sha in old_files.items():self.assertEqual(digest(p), sha)

    def test_upgrade_mode_reaches_review_without_recalling_complete_document(self):
        from tests.fixtures import workflow, start
        w, calls = workflow(self.root / 'workflow')
        before = start(w, self.root)
        with patch('docflow.parser.cache_identity', return_value='new'), patch('docflow.migration.cache_identity', return_value='new'):
            after = w.execute('resume', {'request_id': 'upgrade-cli', 'batch_id': before['batch_id'],
                              'expected_revision': before['revision'], 'mode': 'upgrade_parser'})
        self.assertEqual(after['phase'], 'awaiting_review', after)
        self.assertEqual(after['documents'][0]['working'], before['documents'][0]['working'])
        self.assertEqual(len(calls), 2)

    def test_tampered_checkpoint_refused(self):
        def transport(payload):return response('无需修改' if '文档转写核对员' in str(payload) else '真实正文')
        with patch('docflow.parser.cache_identity', return_value='old'):
            parse_document(self.store, self.doc, self.config, self.prompt, lambda: None, ModelClient(self.config, transport))
        target = self.root / self.doc['unit_refs']['u0001']['path']
        target.write_text('{}')
        with patch('docflow.migration.cache_identity', return_value='new'):
            with self.assertRaises(WorkflowError):upgrade_document(self.store,self.doc,self.config,self.prompt,'upgrade3')
