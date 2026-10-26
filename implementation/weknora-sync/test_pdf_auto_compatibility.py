import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import fitz
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from docflow.model import ModelClient
from docflow.runner import Workflow
from failure_alerts import FailureAlerts
from parse_policy import ParseFailure
from pdf_compatibility import automatic_compatible_pdf
from pdf_compatibility_worker import RepairError, repair
from reconcile import hashes
from transform_local import render
from test_worker import Remote
from worker import Sync


class AutoPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source=self.root/'fixture.pdf'
        doc=fitz.open();page=doc.new_page();page.insert_text((72,72),'PDF compatibility acceptance sample');doc.save(self.source);doc.close()
        self.digest=hashes(self.source)['sha256'];self.work=self.root/'job';self.work.mkdir()
        self.cfg={'state_dir':str(self.root/'state')}

    def trigger(self,cfg=None):
        with patch('pdf_compatibility._pdf_readable',side_effect=PdfReadError('Could not read Boolean object')):
            return automatic_compatible_pdf(cfg or self.cfg,self.source,self.digest,self.work)

    def test_healthy_other_error_disabled_and_encrypted_paths_never_rewrite(self):
        with patch('pdf_compatibility.subprocess.run') as execute:
            self.assertEqual(automatic_compatible_pdf(self.cfg,self.source,self.digest,self.work),(None,None))
            with patch('pdf_compatibility._pdf_readable',side_effect=PdfReadError('Invalid PDF header')):
                self.assertEqual(automatic_compatible_pdf(self.cfg,self.source,self.digest,self.work),(None,None))
            self.assertEqual(automatic_compatible_pdf({**self.cfg,'pdf_auto_compatibility_enabled':False},self.source,self.digest,self.work),(None,None))
            writer=fitz.open(self.source);encrypted=self.root/'encrypted.pdf';writer.save(encrypted,encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw='secret');writer.close()
            self.assertEqual(automatic_compatible_pdf(self.cfg,encrypted,hashes(encrypted)['sha256'],self.work),(None,None))
            execute.assert_not_called()

    def test_automatic_repair_is_verified_atomic_and_reused_without_touching_original(self):
        original=self.source.read_bytes();stat=self.source.stat()
        result,evidence=self.trigger()
        self.assertEqual(len(PdfReader(result).pages),1)
        self.assertTrue(evidence['pages'][0]['same_pixels_108dpi'])
        self.assertEqual(self.source.read_bytes(),original)
        self.assertEqual(self.source.stat().st_mtime_ns,stat.st_mtime_ns)
        self.assertFalse(list(result.parent.parent.glob('.*-*')))
        with patch('pdf_compatibility.subprocess.run') as execute:
            self.assertEqual(automatic_compatible_pdf(self.cfg,self.source,self.digest,self.work)[0],result)
            execute.assert_not_called()

    def test_changed_bytes_get_new_registry_identity(self):
        first,_=self.trigger()
        with self.source.open('ab') as output:output.write(b'\n% harmless changed-version marker\n')
        self.digest=hashes(self.source)['sha256'];second,_=self.trigger()
        self.assertNotEqual(first,second)

    def test_limits_timeout_dependency_and_mismatch_are_terminal_and_leave_no_registry(self):
        cases=[({'pdf_compatibility_max_bytes':1},None,'PDF_COMPATIBILITY_LIMIT'),
               ({},subprocess.TimeoutExpired('fixture',1),'PDF_COMPATIBILITY_LIMIT'),
               ({},FileNotFoundError(),'PDF_COMPATIBILITY_ENVIRONMENT')]
        for options,error,code in cases:
            with self.subTest(code=code,options=options):
                with patch('pdf_compatibility._pdf_readable',side_effect=PdfReadError('Could not read Boolean object')):
                    if error:
                        with patch('pdf_compatibility.subprocess.run',side_effect=error):
                            with self.assertRaises(ParseFailure) as raised:automatic_compatible_pdf({**self.cfg,**options},self.source,self.digest,self.work)
                    else:
                        with self.assertRaises(ParseFailure) as raised:automatic_compatible_pdf({**self.cfg,**options},self.source,self.digest,self.work)
                self.assertEqual(raised.exception.code,code);self.assertFalse(raised.exception.retryable)
                self.assertFalse((Path(self.cfg['state_dir'])/'pdf-compatibility'/self.digest).exists())
                self.assertEqual(json.loads((self.work/'pdf-compatibility.json').read_text())['status'],'failed')
        def rejected(command,**kwargs):
            dest=Path(command[command.index('--evidence')+1]);dest.write_text(json.dumps({'status':'failed','reason_code':'PAGE_VALIDATION_CHANGED'}))
            return subprocess.CompletedProcess(command,1)
        with patch('pdf_compatibility.subprocess.run',side_effect=rejected):
            with self.assertRaises(ParseFailure) as raised:self.trigger()
        self.assertEqual(raised.exception.code,'PDF_COMPATIBILITY_VALIDATION_FAILED')

    def test_subprocess_receives_no_model_secret(self):
        real=subprocess.run;environments=[]
        def execute(*args,**kwargs):environments.append(kwargs['env']);return real(*args,**kwargs)
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'never-forward-fixture','OPENAI_API_KEY':'never-forward-fixture'}),patch('pdf_compatibility.subprocess.run',side_effect=execute):self.trigger()
        self.assertNotIn('DASHSCOPE_API_KEY',environments[0]);self.assertNotIn('OPENAI_API_KEY',environments[0])

    def test_storage_failure_is_logged_without_raw_exception_or_retry_loop(self):
        with patch('pdf_compatibility.tempfile.mkdtemp',side_effect=OSError('private_secret_fixture')):
            with self.assertRaises(ParseFailure) as raised:self.trigger()
        self.assertEqual(raised.exception.code,'PDF_COMPATIBILITY_ENVIRONMENT')
        self.assertFalse(raised.exception.retryable)
        text=(self.work/'pdf-compatibility.json').read_text()
        self.assertNotIn('private_secret_fixture',text)
        self.assertEqual(json.loads(text)['reason_code'],'LOCAL_STORAGE_UNAVAILABLE')

    def test_worker_child_rejects_render_or_text_change(self):
        original_save=fitz.Document.save
        def altered(document,path,**kwargs):
            document[0].insert_text((72,100),'unacceptable added content')
            return original_save(document,path,**kwargs)
        with patch.object(fitz.Document,'save',altered):
            with self.assertRaisesRegex(RepairError,'PAGE_VALIDATION_CHANGED'):
                repair(self.source,self.root/'altered.pdf',self.digest,100,16000000)
        with self.assertRaisesRegex(RepairError,'PIXEL_LIMIT'):
            repair(self.source,self.root/'oversized.pdf',self.digest,100,10)

    def test_isolated_worker_repairs_parses_uploads_indexes_and_recovers_after_restart(self):
        source_root=self.root/'sources';source_root.mkdir();source=source_root/'sample.pdf';source.write_bytes(self.source.read_bytes())
        md=self.root/'md';md.mkdir();baseline=self.root/'baseline.json';baseline.write_text('[]')
        secret=self.root/'secret.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'isolated-fixture'}));secret.chmod(0o600)
        cfg={**self.cfg,'source_root':str(source_root),'markdown_root':str(md),'knowledge_base_id':'kb',
             'weknora_base_url':'http://fake','folder_root':'项目to咨询','baseline_mapping':str(baseline),
             'stable_seconds':0,'llm_enabled':True,'allow_model_calls':True,'reuse_robot1_artifacts':False,
             'model_secrets_file':str(secret),'parser_config':{'model_review':False,'parser_workers':1}}
        api=Remote();now=[0];model_calls=[]
        def client(config):
            def transport(payload):
                model_calls.append(payload)
                return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'# 正文\n自动兼容验收标识。'}}]}).encode(),200,'fixture'
            return ModelClient(config,transport,sleep=lambda _:None)
        def workflow(root):return Workflow(root,client_factory=client)
        worker=Sync(cfg,api=api,renderer=render,clock=lambda:now[0])
        with patch('pdf_compatibility._pdf_readable',side_effect=PdfReadError('Could not read Boolean object')),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=client):
            now[0]+=1;worker.cycle();now[0]+=1;worker.cycle()
        self.assertEqual(len(model_calls),1);self.assertEqual(api.upload_calls,1)
        api.complete('1');now[0]+=1;worker.cycle()
        record=next(iter(worker.state['records'].values()));self.assertEqual(record['state'],'active');self.assertTrue(record['keyword_retrieval_verified'])
        self.assertTrue(record['provenance']['pdf_compatibility']['original_preserved'])
        self.assertTrue(any(event['kind']=='pdf_compatibility_verified' for event in worker.state['events']))
        restarted=Sync(cfg,api=api,renderer=render,clock=lambda:now[0]);now[0]+=1;restarted.cycle()
        self.assertEqual(api.upload_calls,1)

    def test_validation_failure_keeps_old_version_and_notifies_once(self):
        source=self.root/'sources';source.mkdir();path=source/'a.txt';path.write_text('version1')
        baseline=self.root/'baseline.json';baseline.write_text('[]');md=self.root/'md';md.mkdir();now=[0];notices=[];api=Remote()
        cfg={**self.cfg,'source_root':str(source),'markdown_root':str(md),'knowledge_base_id':'kb','weknora_base_url':'http://fake',
             'folder_root':'项目to咨询','baseline_mapping':str(baseline),'stable_seconds':0,'failure_notifications':{'enabled':True,'desktop':True,'desktop_cooldown_seconds':10}}
        failed=[False]
        def renderer(cfg,source,relative,digest,work):
            if failed[0]:raise ParseFailure('PDF_COMPATIBILITY_VALIDATION_FAILED','document',False,'保留原件，查看PDF验证报告')
            work.mkdir(parents=True,exist_ok=True);p=work/'publication.md';p.write_text(source.read_text());return p,{'method':'fixture'}
        w=Sync(cfg,api=api,renderer=renderer,clock=lambda:now[0]);w.failure_alerts.notifier=lambda title,message:notices.append(message) or 'submitted'
        for _ in range(2):now[0]+=1;w.cycle()
        api.complete('1');now[0]+=1;w.cycle()
        failed[0]=True;path.write_text('version2');now[0]+=11;w.cycle();now[0]+=1;w.cycle();now[0]+=1;w.cycle()
        r=next(iter(w.state['records'].values()));self.assertEqual(r['state'],'parse_blocked_hold');self.assertEqual(r['remote_ids'],['1']);self.assertEqual(api.deleted,[])
        self.assertEqual((md/'a.txt.md').read_text(),'version1');self.assertEqual(len(notices),1)
        current=json.loads((Path(cfg['state_dir'])/'failure-alerts/current.json').read_text());self.assertEqual(current['issues'][0]['code'],'PDF_COMPATIBILITY_VALIDATION_FAILED')
        self.assertIn('逐页一致性验证',current['issues'][0]['reason'])

    def test_renderer_records_compatibility_failure_before_model_call(self):
        secret=self.root/'secret.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'isolated-fixture'}));secret.chmod(0o600)
        cfg={**self.cfg,'reuse_robot1_artifacts':False,'llm_enabled':True,'allow_model_calls':True,
             'model_secrets_file':str(secret),'pdf_compatibility_max_bytes':1}
        with patch('pdf_compatibility._pdf_readable',side_effect=PdfReadError('Could not read Boolean object')),patch('docflow.model.ModelClient.call') as model:
            with self.assertRaises(ParseFailure) as raised:render(cfg,self.source,self.source.name,self.digest,self.work)
            model.assert_not_called()
        self.assertEqual(raised.exception.code,'PDF_COMPATIBILITY_LIMIT')
        report=json.loads((self.work/'parse-report.json').read_text())
        self.assertEqual(report['pdf_compatibility']['reason_code'],'SOURCE_SIZE_LIMIT')
        self.assertEqual(report['failure']['code'],'PDF_COMPATIBILITY_LIMIT')


if __name__=='__main__':unittest.main()
