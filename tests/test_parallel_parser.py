import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docflow.common import artifact, WorkflowError, write_json
from docflow.config import configuration
from docflow.model import ModelClient
from docflow.parser import cache_identity, parse_document
from docflow.storage import Store
from tests.test_parser import response


class ParallelParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.source = self.root / 'source.txt'
        self.source.write_text('测试资料')
        self.document = {'name': 'source.txt', 'extension': '.txt', 'source': artifact(self.root, self.source)}
        self.config = configuration({'parser_workers': 3})
        self.prompt = Path(self.config['parser_prompt']).read_text()
        self.units = [{'id': f'u{i:04d}', 'label': f'第{i}段', 'text': f'原文{i}', 'image': None} for i in range(1, 7)]

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, source, directory, config):
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / 'prepared.json', self.units)
        return self.units

    def test_defaults_and_config_validation(self):
        self.assertFalse(configuration()['model_review'])
        self.assertEqual(configuration()['parser_workers'], 4)
        for values in [{'parser_workers': 0}, {'parser_workers': 9}, {'parser_workers': True}, {'model_review': 'false'}]:
            with self.assertRaises(WorkflowError):
                configuration(values)

    def test_bounded_concurrency_order_skip_review_and_main_thread_checkpoints(self):
        lock = threading.Lock()
        barrier = threading.Barrier(3)
        active = peak = 0
        calls = []
        def transport(payload):
            nonlocal active, peak
            with lock:
                calls.append(payload)
                active += 1
                peak = max(peak, active)
                ordinal = len(calls)
            if ordinal <= 3:
                barrier.wait(timeout=5)
            time.sleep(0.01 * (4 - ((ordinal - 1) % 3)))
            with lock:
                active -= 1
            return response('完整原文')
        def checkpoint():
            self.assertEqual(threading.current_thread(), threading.main_thread())
        with patch('docflow.parser.prepare', self.prepare):
            result = parse_document(self.store, self.document, self.config, self.prompt, checkpoint,
                                    ModelClient(self.config, transport))
        self.assertEqual(peak, 3)
        self.assertEqual(len(calls), 6)
        self.assertEqual([u['id'] for u in result['units']], [u['id'] for u in self.units])
        self.assertTrue(all(u['model_review'] == 'skipped' for u in result['units']))
        self.assertFalse(list(self.root.rglob('review/request.json')))
        other = {k: self.document[k] for k in ('name', 'source', 'extension')}
        parse_document(self.store, other, {**self.config, 'parser_workers': 1}, self.prompt, checkpoint,
                       ModelClient(self.config, lambda _: self.fail('cache should avoid model calls')))
        self.assertTrue(other['cache_hit'])

    def test_failure_drains_inflight_checkpoints_and_resume_only_remaining_units(self):
        seen = []
        def transport(payload):
            text = payload['messages'][1]['content'][0]['text']
            unit = next(u['id'] for u in self.units if u['id'] in text)
            seen.append(unit)
            if unit == 'u0001':
                raise WorkflowError('MODEL_HTTP_ERROR', 'test terminal error')
            time.sleep(0.05)
            return response('完整原文')
        with patch('docflow.parser.prepare', self.prepare):
            with self.assertRaises(WorkflowError):
                parse_document(self.store, self.document, self.config, self.prompt, lambda: None,
                               ModelClient(self.config, transport))
        self.assertEqual(set(seen), {'u0001', 'u0002', 'u0003'})
        self.assertEqual(set(self.document['unit_refs']), {'u0002', 'u0003'})
        retries = []
        def recovered(payload):
            retries.append(payload)
            return response('完整原文')
        result = parse_document(self.store, self.document, self.config, self.prompt, lambda: None,
                                ModelClient(self.config, recovered, retry_terminal=True))
        self.assertEqual(len(result['units']), 6)
        self.assertEqual(len(retries), 4)

    def test_review_mode_separates_cache_identity(self):
        a = cache_identity(self.document, self.config, self.prompt)
        b = cache_identity(self.document, {**self.config, 'model_review': True}, self.prompt)
        self.assertNotEqual(a, b)
        legacy = {k: v for k, v in self.config.items() if k != 'model_review'}
        self.assertEqual(b, cache_identity(self.document, legacy, self.prompt))
