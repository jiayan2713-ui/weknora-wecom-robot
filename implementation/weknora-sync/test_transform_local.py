import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from docflow.model import ModelClient
from docflow.runner import Workflow
from reconcile import hashes
from transform_local import render
from parse_policy import ParseFailure


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.secret = self.root / 'model-env.json'
        self.secret.write_text(json.dumps({'DASHSCOPE_API_KEY': 'isolated-fake-key'}))
        self.secret.chmod(0o600)
        self.cfg = {'reuse_robot1_artifacts': False, 'llm_enabled': True, 'allow_model_calls': True,
                    'model_secrets_file': str(self.secret),
                    'parser_config': {'model_review': False, 'parser_workers': 1,
                                      'empty_body_policy': 'review'}}
        self.calls = []
        self.workflows = []

    def run_render(self, source, notes_only=False, response=None):
        def model_factory(config):
            def transport(payload):
                self.calls.append(payload)
                text = '# 正文\n项目A费用为10万元。'
                if response:
                    text = response(len(self.calls), payload)
                if notes_only and len(self.calls) == 1:
                    text = '# 解析记录（非原文）\n正文需要补全。'
                return json.dumps({'choices': [{'finish_reason': 'stop',
                    'message': {'content': text}}]}, ensure_ascii=False).encode(), 200, 'isolated-test'
            return ModelClient(config, transport, sleep=lambda _: None)

        def workflow_factory(directory):
            workflow = Workflow(directory, client_factory=model_factory)
            self.workflows.append(workflow)
            return workflow

        with patch.dict(os.environ), patch('docflow.runner.Workflow', side_effect=workflow_factory), \
                patch('docflow.model.ModelClient', side_effect=model_factory):
            return render(self.cfg, source, source.name, hashes(source)['sha256'], self.root / 'job')

    def test_txt_uses_complete_batch_instead_of_public_status_projection(self):
        source = self.root / 'fixture.txt'
        source.write_text('项目A费用为10万元。')
        output, provenance = self.run_render(source)
        workflow = self.workflows[0]
        public = workflow.status(provenance['batch_id'])['documents'][0]
        self.assertNotIn('cache_ref', public)
        self.assertNotIn('extension', public)
        self.assertIn('项目A费用为10万元。', output.read_text())
        self.assertEqual(provenance['source_hash'], hashes(source)['sha256'])
        self.assertFalse(provenance['human_review'])
        self.assertEqual(len(self.calls), 1)
        report = json.loads(Path(provenance['parse_report_path']).read_text())
        self.assertEqual(report['expected_units'], 1)
        self.assertEqual(report['completed_units'], 1)
        self.assertEqual(report['text_units'], 1)
        self.assertEqual(report['coverage_scope'], 'prepared_units')

    def test_image_body_repair_uses_internal_extension_and_verified_cache(self):
        from PIL import Image
        source = self.root / 'fixture.png'
        Image.new('RGB', (160, 160), '#cccccc').save(source)
        output, provenance = self.run_render(source, notes_only=True)
        self.assertIn('项目A费用为10万元。', output.read_text())
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(provenance['human_review'])
        self.assertEqual(provenance['parse_report']['status'], 'complete_with_warnings')

    def test_new_markdown_preserves_content_without_model_or_secret(self):
        source = self.root / 'fixture.md'
        body = '# 项目A\n\n费用为10万元。\n'
        source.write_text(body)
        self.cfg.pop('model_secrets_file')
        self.cfg['allow_model_calls'] = False
        output, provenance = render(self.cfg, source, source.name, hashes(source)['sha256'], self.root / 'job')
        self.assertIn(body, output.read_text())
        self.assertEqual(provenance['method'], 'local_text')
        self.assertFalse(provenance['human_review'])
        self.assertEqual(provenance['parse_report']['coverage_scope'], 'artifact_only')
        self.assertIsNone(provenance['parse_report']['expected_units'])

    def test_missing_native_text_unit_recovers_without_another_model_request(self):
        source = self.root / 'fixture.txt'
        source.write_text('项目A费用为10万元。\n' * 201)
        def response(number, payload):
            return '# 正文\n项目A费用为10万元。' if number == 1 else '# 解析记录（非原文）\n正文缺失。'
        output, provenance = self.run_render(source, response=response)
        report = json.loads(Path(provenance['parse_report_path']).read_text())
        self.assertEqual(report['expected_units'], 2)
        self.assertEqual(report['missing_body_units'], 0)
        self.assertIn('项目A费用为10万元。', output.read_text())
        self.assertEqual(len(self.calls), 2)  # Native text recovery needs no extra model request.

    def test_model_truncation_retains_specific_failure_and_stops_before_resume(self):
        source = self.root / 'fixture.txt'
        source.write_text('项目A费用为10万元。')
        def model_factory(config):
            def transport(payload):
                self.calls.append(payload)
                return json.dumps({'choices': [{'finish_reason': 'length',
                    'message': {'content': '不完整正文'}}]}).encode(), 200, 'isolated-test'
            return ModelClient(config, transport, sleep=lambda _: None)
        with patch.dict(os.environ), patch('docflow.runner.Workflow',
                side_effect=lambda directory: Workflow(directory, client_factory=model_factory)):
            with self.assertRaises(ParseFailure) as raised:
                render(self.cfg, source, source.name, hashes(source)['sha256'], self.root / 'job')
        self.assertEqual(raised.exception.code, 'MODEL_TRUNCATED')
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / 'job/publication.md').exists())

    def test_supplier_wait_keeps_real_deadline_without_another_model_attempt(self):
        source = self.root / 'fixture.txt'
        source.write_text('项目A费用为10万元。')
        def model_factory(config):
            def transport(payload):
                self.calls.append(payload)
                raise urllib.error.HTTPError('https://isolated.invalid', 429, 'wait',
                                             {'Retry-After': '300'}, None)
            return ModelClient(config, transport, sleep=lambda _: None)
        with patch.dict(os.environ), patch('docflow.runner.Workflow',
                side_effect=lambda directory: Workflow(directory, client_factory=model_factory)):
            with self.assertRaises(ParseFailure) as raised:
                render(self.cfg, source, source.name, hashes(source)['sha256'], self.root / 'job')
        self.assertEqual(raised.exception.code, 'RETRY_AFTER')
        self.assertGreater(raised.exception.retry_at, time.time() + 290)
        self.assertEqual(len(self.calls), 1)

    def test_corrupt_previous_report_does_not_hide_original_failure(self):
        source = self.root / 'fixture.txt'
        source.write_text('项目A费用为10万元。')
        work = self.root / 'job'
        work.mkdir()
        (work / 'parse-report.json').write_text('{broken')
        self.cfg['allow_model_calls'] = False
        with self.assertRaises(ParseFailure) as raised:
            render(self.cfg, source, source.name, hashes(source)['sha256'], work)
        self.assertEqual(raised.exception.code, 'MODEL_AUTHORIZATION_REQUIRED')
        report = json.loads((work / 'parse-report.json').read_text())
        self.assertEqual(report['failure']['category'], 'credentials')


if __name__ == '__main__':
    unittest.main()
