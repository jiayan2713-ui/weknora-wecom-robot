import json
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from docflow.common import WorkflowError
from docflow.config import configuration
from docflow.documents import prepare
from docflow.model import ModelClient
from docflow.parser import transcribe, cache_identity
from docflow.pipeline import LimitedClient
from docflow.runner import Workflow
from tests.test_parser import response


class PipelineTests(unittest.TestCase):
    def test_empty_output_is_flagged_once_not_silently_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); config = configuration({'empty_body_policy': 'review'})
            prompt = Path(config['parser_prompt']).read_text()
            unit = {'id': 'u0001', 'label': '第1页', 'text': '必须核对的原生文字', 'image': None}
            for i, text in enumerate(['# 解析记录（非原文）\n未看到正文', '[[DOCFLOW_NO_TEXT]]', '# 正文\n[[DOCFLOW_NO_TEXT]]']):
                calls = []
                def transport(payload):
                    calls.append(payload)
                    return response(text)
                result = transcribe(ModelClient(config, transport), root / str(i), unit, '.txt', prompt)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['body'], '')
                self.assertEqual(result['content_status'], 'needs_manual_confirmation')
                self.assertTrue(result['issues'])

    def test_cancelled_gate_does_not_consume_an_attempt(self):
        client = Mock();client.config = {}; stopped = threading.Event();stopped.set()
        with self.assertRaises(WorkflowError):
            LimitedClient(client, threading.BoundedSemaphore(1), stopped).call('unused', 'draft', [])
        client.call.assert_not_called()

    def test_config_bounds_and_empty_policy_cache_isolation(self):
        for values in [{'document_workers': 0}, {'document_workers': 9}, {'render_workers': 5}, {'render_workers': True}, {'empty_body_policy': 'ignore'}]:
            with self.assertRaises(WorkflowError):configuration(values)
        c = configuration();doc = {'source': {'sha256': 'abc'}, 'extension': '.txt'}
        self.assertNotEqual(cache_identity(doc, c, 'p'), cache_identity(doc, {**c, 'empty_body_policy':'recover'}, 'p'))
        self.assertEqual(cache_identity(doc, c, 'p'), cache_identity(doc, {**c,'document_workers':4,'render_workers':4,'parser_workers':8}, 'p'))

    def test_cross_document_limit_and_single_batch_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();files=[]
            for i in range(4):
                p=root/f'{i}.txt';p.write_text((f'DOC{i}原文\n')*401);files.append(str(p))
            lock=threading.Lock();barrier=threading.Barrier(8);active=peak=0;calls=[]
            def factory(config):
                def transport(payload):
                    nonlocal active,peak
                    with lock:active+=1;peak=max(peak,active);calls.append(payload);ordinal=len(calls)
                    if ordinal<=8:barrier.wait(timeout=8)
                    time.sleep(.015)
                    with lock:active-=1
                    return response('原文正文')
                return ModelClient(config,transport)
            w=Workflow(root/'state',client_factory=factory);save=w._save_batch
            def checked_save(batch):
                self.assertIs(threading.current_thread(),threading.main_thread());save(batch)
            with patch.object(w,'_save_batch',checked_save):
                result=w.execute('start',{'request_id':'parallel','project':'test','sources':files,'config':{'document_workers':4,'parser_workers':8}})
            self.assertEqual(result['phase'],'awaiting_review',result)
            self.assertEqual(len(calls),12)
            self.assertEqual(peak,8)
            batch=w.store.batch(result['batch_id'])
            self.assertEqual([d['name'] for d in batch['documents']],['0.txt','1.txt','2.txt','3.txt'])
            self.assertTrue(all(d['state']=='parsed' and d['units_done']==3 for d in batch['documents']))

    def test_duplicate_source_reuses_cache_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();files=[];calls=[]
            for name in ['a.txt','b.txt']:
                p=root/name;p.write_text('完全相同资料');files.append(str(p))
            def factory(config):
                def transport(payload):calls.append(payload);time.sleep(.02);return response('完全相同资料')
                return ModelClient(config,transport)
            w=Workflow(root/'state',client_factory=factory)
            result=w.execute('start',{'request_id':'duplicate','project':'test','sources':files,'config':{'document_workers':2,'parser_workers':8}})
            self.assertEqual(result['phase'],'awaiting_review',result)
            self.assertEqual(len(calls),1)
            self.assertEqual(sum(d.get('cache_hit',False) for d in result['documents']),1)

    def test_fatal_failure_stops_queued_documents(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();files=[];calls=[]
            for i in range(6):
                p=root/f'{i}.txt';p.write_text(f'原文{i}');files.append(str(p))
            def factory(config):
                def transport(payload):
                    calls.append(payload)
                    raise urllib.error.HTTPError('https://example.invalid',401,'Unauthorized',{},None)
                return ModelClient(config,transport)
            w=Workflow(root/'state',client_factory=factory)
            result=w.execute('start',{'request_id':'failed','project':'test','sources':files,'config':{'document_workers':2,'parser_workers':1}})
            self.assertEqual(result['errors'][0]['code'],'MODEL_HTTP_ERROR',result)
            self.assertEqual(len(calls),1)
            self.assertTrue(all(d['state']=='pending' for d in result['documents'][2:]))

    def test_render_global_bound_and_output_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();src=root/'source.pdf';src.write_bytes(b'fixture')
            config=configuration({'render_workers':4});slots=threading.BoundedSemaphore(2)
            lock=threading.Lock();active=peak=0
            class Page(dict):
                def extract_text(self):return '原文'
            reader=Mock();reader.is_encrypted=False;reader.pages=[Page() for _ in range(6)]
            def render(command,timeout,**kwargs):
                nonlocal active,peak
                with lock:active+=1;peak=max(peak,active)
                n=int(command[command.index('-f')+1]);time.sleep(.005*(7-n))
                Image.new('RGB',(12,12),(n,n,n)).save(command[-1]+'.png')
                with lock:active-=1
            with patch('pypdf.PdfReader',return_value=reader),patch('docflow.documents.run',side_effect=render):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(prepare,src,root/str(i),config,slots) for i in range(2)]
                    results=[f.result() for f in futures]
            self.assertEqual(peak,2)
            for result in results:
                self.assertEqual([u['label'] for u in result],[f'PDF第{i}页' for i in range(1,7)])
                self.assertEqual([u['id'] for u in result],[f'u{i:04d}' for i in range(1,7)])

    def test_checkpoint_failure_stops_request_production(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();files=[];calls=[]
            for i in range(4):
                p=root/f'{i}.txt';p.write_text((f'原文{i}\n')*401);files.append(str(p))
            def factory(config):
                def transport(payload):
                    calls.append(payload);time.sleep(.08);return response('完整正文')
                return ModelClient(config,transport)
            w=Workflow(root/'state',client_factory=factory);save=w._save_batch;failed=False
            def fail_once(batch):
                nonlocal failed
                if not failed and any(d.get('cache_key') for d in batch.get('documents',[])):
                    failed=True
                    raise OSError('simulated checkpoint failure')
                save(batch)
            with patch.object(w,'_save_batch',fail_once):
                result=w.execute('start',{'request_id':'disk','project':'test','sources':files,'config':{'document_workers':4,'parser_workers':8}})
            self.assertTrue(failed)
            self.assertEqual(result['errors'][0]['code'],'IO_ERROR')
            self.assertLessEqual(len(calls),8)
