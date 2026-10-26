"""Behavioral regression for automatic recovery, durable budgets and process lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from docflow.common import WorkflowError, write_json, read_json, digest
from docflow.config import configuration
from docflow.model import ModelClient
from docflow.runtime import session, request_stop, reset_stop
from docflow.runner import Workflow
from parse_artifacts import Artifacts
from parse_policy import ParseFailure
from reconcile import hashes
from transform_local import render
from worker import Sync, writer_session


def response(text):
    return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':text}}]}).encode(),200,'isolated'


class DurableRequestTests(unittest.TestCase):
    def tearDown(self):reset_stop()

    def test_new_jobs_reuse_actual_payload_and_do_not_reset_file_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();calls=[];cfg=configuration()
            client=ModelClient(cfg,lambda p:(calls.append(p) or response('# 正文\n原文')))
            with session(root,'a'*64,{'max_file_requests':2}):
                client.call(root/'jobs/one','draft',[{'role':'user','content':'one'}])
            with session(root,'a'*64,{'max_file_requests':2}) as ledger:
                client.call(root/'jobs/two','draft',[{'role':'user','content':'one'}])
                self.assertEqual(ledger.new_attempts,0)
                client.call(root/'jobs/two','draft-2',[{'role':'user','content':'two'}])
            with session(root,'a'*64,{'max_file_requests':2}):
                with self.assertRaises(WorkflowError) as error:
                    client.call(root/'jobs/three','draft',[{'role':'user','content':'three'}])
                self.assertEqual(error.exception.code,'PARSE_BUDGET_EXCEEDED')
            self.assertEqual(len(calls),2)

    def test_unknown_is_not_resent_in_a_new_client_or_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();calls=[];cfg=configuration()
            def interrupted(payload):calls.append(payload);raise SystemExit('simulated exit')
            with self.assertRaises(SystemExit),session(root,'b'*64):
                ModelClient(cfg,interrupted).call(root/'jobs/first','draft',[])
            with session(root,'b'*64):
                with self.assertRaises(WorkflowError) as error:
                    ModelClient(cfg,interrupted).call(root/'jobs/retry','draft',[])
                self.assertEqual(error.exception.code,'MODEL_OUTCOME_UNKNOWN')
            self.assertEqual(len(calls),1)
            self.assertEqual(len(read_json(root/'parse-runtime'/('b'*64)/'budget.json')['attempts']),1)

    def test_durable_response_before_receipt_recovers_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();cfg=configuration();calls=[]
            def interrupted(payload):calls.append(payload);raise SystemExit()
            with self.assertRaises(SystemExit),session(root,'c'*64):
                ModelClient(cfg,interrupted).call(root/'old','draft',[])
            target=next((root/'parse-runtime'/('c'*64)/'requests').glob('*/call.json')).parent
            (target/'response.json').write_bytes(response('# 正文\n已落盘正文')[0])
            with session(root,'c'*64):
                result=ModelClient(cfg,interrupted).call(root/'new','draft',[])
            self.assertIn('已落盘',result);self.assertEqual(len(calls),1)

    def test_stop_gate_preserves_finished_response_and_blocks_new_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();calls=[];cfg=configuration()
            def transport(payload):calls.append(payload);request_stop();return response('received')
            with session(root,'d'*64):
                client=ModelClient(cfg,transport)
                self.assertEqual(client.call(root,'first',[]),'received')
                self.assertEqual(client.call(root,'first',[]),'received')
                with self.assertRaises(WorkflowError) as error:client.call(root,'second',[{'role':'user','content':'new'}])
                self.assertEqual(error.exception.code,'PARSER_STOPPED')
            self.assertEqual(len(calls),1)

    def test_sigterm_drains_inflight_call_without_dispatching_next(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();script=root/'child.py'
            script.write_text('''import json,signal,time,sys
from pathlib import Path
from docflow.model import ModelClient
from docflow.config import configuration
from docflow.runtime import session,request_stop
from docflow.common import WorkflowError
root=Path(sys.argv[1]);signal.signal(signal.SIGTERM,lambda *_:request_stop())
def transport(payload):
 (root/'entered').write_text('1');time.sleep(1)
 return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'saved'}}]}).encode(),200,'fixture'
with session(root,'e'*64):
 client=ModelClient(configuration(),transport)
 client.call(root/'job','first',[])
 try:client.call(root/'job','second',[{'role':'user','content':'next'}])
 except WorkflowError as error:(root/'stopped').write_text(error.code)
''')
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[2])}
            child=subprocess.Popen([sys.executable,str(script),str(root)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not (root/'entered').exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((root/'entered').exists());child.send_signal(signal.SIGTERM)
                _,stderr=child.communicate(timeout=15);self.assertEqual(child.returncode,0,stderr.decode())
                self.assertEqual((root/'stopped').read_text(),'PARSER_STOPPED')
                self.assertEqual(len(read_json(root/'parse-runtime'/('e'*64)/'budget.json')['attempts']),1)
                self.assertTrue((root/'job/first/response.json').is_file())
            finally:
                if child.poll() is None:child.kill();child.wait()


class ArtifactAndWriterTests(unittest.TestCase):
    def test_image_resolver_uses_registered_derived_root_and_checks_sha(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'old').mkdir();a=Artifacts(root/'old',root/'job')
            image=a.roots['derived']/'region.png';Image.new('RGB',(5,5),'white').save(image)
            unit=a.bind([{'id':'child','label':'region','image':'region.png'}], 'derived')[0]
            self.assertEqual(a.resolve(unit),image)
            image.write_bytes(b'changed')
            with self.assertRaises(WorkflowError):a.resolve(unit)

    def test_second_writer_is_rejected_before_loading_state(self):
        import worker
        with tempfile.TemporaryDirectory() as directory:
            cfg={'state_dir':str(Path(directory).resolve())};path=Path(directory)/'config.json';path.write_text(json.dumps(cfg))
            with writer_session(cfg),patch('worker.Sync') as constructor,patch.object(sys,'argv',['worker.py','watch','--config',str(path)]):
                self.assertEqual(worker.main(),2);constructor.assert_not_called()
            self.assertFalse((Path(directory)/'health.json').exists())

    def test_first_discovery_and_parse_intent_survive_exit(self):
        from test_worker import WorkerTests
        fixture=WorkerTests();fixture.setUp()
        try:
            source=fixture.source/'x.txt';source.write_text('original')
            fixture.cycle()
            def stop(*args):raise SystemExit('first parse interrupted')
            fixture.worker.renderer=stop
            with self.assertRaises(SystemExit):fixture.cycle()
            before=read_json(fixture.home/'state.json');identity=next(iter(before['records']))
            self.assertEqual(before['records'][identity]['parse_intent']['phase'],'parsing')
            fixture.worker=fixture.create_worker();fixture.cycle()
            after=read_json(fixture.home/'state.json')
            self.assertEqual(list(after['records']),[identity]);self.assertEqual(fixture.api.upload_calls,1)
            self.assertEqual(source.read_text(),'original')
        finally:fixture.doCleanups()


class AutomaticParseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.secret=self.root/'secret.json'
        self.secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'test-fixture'}));self.secret.chmod(0o600)
        self.cfg={'state_dir':str(self.root),'reuse_robot1_artifacts':False,'llm_enabled':True,'allow_model_calls':True,
                  'model_secrets_file':str(self.secret),'parser_config':{'model_review':False,'parser_workers':1,'empty_body_policy':'review'}}

    def pdf(self,gray=False):
        import fitz
        source=self.root/'file.pdf';book=fitz.open();page=book.new_page();page.insert_text((72,72),'Fee: 100 USD')
        page=book.new_page()
        if gray:page.draw_rect(page.rect,fill=(.5,.5,.5))
        else:page.insert_text((72,72),'Time: 2 months')
        book.save(source);book.close();return source

    def renderer(self,source,transport,work):
        def factory(config):return ModelClient(config,transport,sleep=lambda _:None)
        def workflow(directory):return Workflow(directory,client_factory=factory)
        with patch.dict(os.environ),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=factory):
            return render(self.cfg,source,source.name,hashes(source)['sha256'],work)

    def test_first_parse_auto_recovers_then_zero_call_replay(self):
        source=self.pdf();before=hashes(source)['sha256'];calls=[]
        def transport(payload):
            calls.append(payload)
            text=('# 正文\nFee: 100 USD' if len(calls)==1 else
                  '# 解析记录（非原文）\n缺正文' if len(calls)==2 else '# 正文\nTime: 2 months')
            return response(text)
        output,provenance=self.renderer(source,transport,self.root/'jobs/current')
        self.assertEqual(len(calls),3);self.assertIn('Time: 2 months',output.read_text())
        first=output.read_bytes();self.renderer(source,transport,self.root/'jobs/current')
        self.assertEqual(len(calls),3);self.assertEqual(output.read_bytes(),first)
        self.assertEqual(hashes(source)['sha256'],before)
        self.assertEqual(provenance['parse_report']['completed_units'],2)

    def test_native_numeric_conflict_never_publishes(self):
        source=self.pdf()
        def transport(payload):return response('# 正文\nFee: 200 USD\nTime: 2 months')
        with self.assertRaises(ParseFailure) as error:self.renderer(source,transport,self.root/'jobs/conflict')
        self.assertEqual(error.exception.code,'UNIT_COVERAGE_INCOMPLETE')
        self.assertFalse((self.root/'jobs/conflict/publication.md').exists())

    def test_partial_no_text_requires_actual_two_response_evidence(self):
        source=self.pdf(gray=True);calls=[]
        def transport(payload):
            calls.append(payload);return response('# 正文\nFee: 100 USD' if len(calls)==1 else '# 正文\n[[DOCFLOW_NO_TEXT]]')
        _,p=self.renderer(source,transport,self.root/'jobs/nontext')
        report=read_json(Path(p['parse_report_path']));self.assertEqual(report['completed_units'],2)
        self.assertEqual(report['no_text_units'],1);self.assertEqual(len(calls),3)
        results=read_json(self.root/'jobs/nontext/controlled-results.json')['units'];evidence=results[1]['no_text_verification']
        self.assertTrue(evidence['verified']);self.assertTrue(Path(evidence['first_response']['path']).is_file())
        self.assertTrue(Path(evidence['response_path']).is_file())

    def test_markdown_code_literals_and_escaped_punctuation_are_preserved(self):
        from markdown_assets import has_image_reference
        self.assertFalse(has_image_reference('```markdown\n![example](image.png)\n```'))
        self.assertFalse(has_image_reference('`![example](image.png)` and EB-5![红字结束]'))
        self.assertTrue(has_image_reference('![actual](image.png)'))
        self.assertTrue(has_image_reference('\\\\![actual](image.png)'))
        self.assertFalse(has_image_reference('\\![literal](image.png)'))


class AdaptiveRecoveryTests(unittest.TestCase):
    def test_decimal_format_is_not_a_numeric_conflict(self):
        from parse_control import numeric_conflicts
        self.assertEqual(numeric_conflicts({'native_text':'Fee: 1,000.00 USD','body':'Fee: 1000 USD'}),[])
        self.assertEqual(numeric_conflicts({'native_text':'Fee: 100 USD','body':'Fee: 200 USD'}),['fee'])
        self.assertEqual(numeric_conflicts({'native_text':'Fee: 1 万美元','body':'Fee: 10000 美元'}),[])

    def test_raster_density_upgrade_reuses_good_and_proves_blank_children(self):
        from PIL import Image,ImageDraw
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'fine.png';image=Image.new('RGB',(2200,1700),'white')
            ImageDraw.Draw(image).text((30,30),'VISIBLE SOURCE TEXT',fill='black');image.save(source)
            secret=root/'secret.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'fixture'}));secret.chmod(0o600)
            cfg={'state_dir':str(root),'model_secrets_file':str(secret),'allow_model_calls':True,'reuse_robot1_artifacts':False,
                 'parser_config':{'model_review':False,'parser_workers':1,'empty_body_policy':'review'}}
            calls=[]
            def factory(config):
                def transport(payload):
                    calls.append(payload)
                    return response('# 正文\n[无法辨认]' if len(calls)<=2 else '# 正文\nVISIBLE SOURCE TEXT')
                return ModelClient(config,transport,sleep=lambda _:None)
            def workflow(directory):return Workflow(directory,client_factory=factory)
            with patch.dict(os.environ),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=factory):
                output,provenance=render(cfg,source,source.name,hashes(source)['sha256'],root/'job')
            self.assertIn('VISIBLE SOURCE TEXT',output.read_text());self.assertTrue((root/'job/density-recovery.json').is_file())
            self.assertEqual(provenance['parse_report']['expected_units'],provenance['parse_report']['completed_units'])
            self.assertEqual(len(calls),3)

    def test_compatibility_copy_identity_is_distinct_from_original_in_xlsx_recovery(self):
        from test_xlsx_regions import XlsxRegionTests
        from docflow.common import artifact
        from types import SimpleNamespace
        from xlsx_recovery import recover
        fixture=XlsxRegionTests()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();workflow,doc,expected,results,pdf=fixture.fixture(root)
            native=pdf.parent/'prepared.json';data=read_json(native);data['source_hash']='parser-copy';write_json(native,data)
            index_path=root/doc['prepared_ref']['path']
            index=read_json(index_path)
            index['artifacts']=[artifact(root,native) if r['path'].endswith('/prepared.json') else r for r in index['artifacts']]
            write_json(index_path,index)
            doc['prepared_ref']=artifact(root,index_path)
            region={'id':'temporary','label':'child','text':'original','image':'r.png','region':{'box':[0,0,20,20]}}
            with patch('xlsx_recovery.render_regions',return_value=[region]),patch('xlsx_recovery.transcribe',return_value={'body':'原文','issues':[]}):
                new_expected,new_results,_=recover(workflow,doc,root/'cache/parse-manifest.json',expected,results,
                    'original-source',root/'job',{'parser_workers':1},object(),'prompt',parser_input_hash='parser-copy')
            self.assertEqual(len(new_expected),len(new_results))


class SourceCatalogTests(unittest.TestCase):
    def test_merged_nonanchor_is_archived_but_missing_visible_cell_is_blocked(self):
        import openpyxl,zipfile,xml.etree.ElementTree as ET,shutil
        from source_catalog import check,catalog
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'original.xlsx';book=openpyxl.Workbook()
            book.active['A1']='anchor';book.active['B1']='visible';book.active.merge_cells('A1:A2');book.save(source);book.close()
            with zipfile.ZipFile(source) as archive:entries=[(entry,archive.read(entry.filename)) for entry in archive.infolist()]
            with zipfile.ZipFile(source,'w') as archive:
                for entry,data in entries:
                    if entry.filename=='xl/worksheets/sheet1.xml':
                        node=ET.fromstring(data);ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                        rows=node.find(ns+'sheetData');row=ET.SubElement(rows,ns+'row',{'r':'2'})
                        cell=ET.SubElement(row,ns+'c',{'r':'A2','t':'inlineStr'});inline=ET.SubElement(cell,ns+'is')
                        ET.SubElement(inline,ns+'t').text='hidden original payload'
                        data=ET.tostring(node)
                    archive.writestr(entry,data)
            parsed=root/'input.xlsx';shutil.copy2(source,parsed)
            native={'native':{'sheets':[{'name':'Sheet','cells':[{'coordinate':'A1'},{'coordinate':'B1'}]}]}}
            cfg={'_parse_original':str(source),'_parse_input':str(parsed)}
            proof=check(cfg,digest(source),digest(parsed),[],native,root/'proof')
            self.assertEqual(proof['original']['sheets'][0]['merged_nonanchor_payloads'],['A2'])
            native['native']['sheets'][0]['cells'].pop()
            with self.assertRaises(WorkflowError) as error:check(cfg,digest(source),digest(parsed),[],native,root/'bad')
            self.assertEqual(error.exception.code,'UNIT_COVERAGE_INCOMPLETE')


class ConnectionOutcomeTests(unittest.TestCase):
    def test_proven_connection_refusal_can_resume_after_backoff(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();calls=[]
            def refused(payload):calls.append(1);raise urllib.error.URLError(ConnectionRefusedError('not connected'))
            with self.assertRaises(WorkflowError) as error:ModelClient(configuration(),refused).call(root,'draft',[])
            self.assertEqual(error.exception.code,'RETRY_AFTER')
            self.assertEqual(read_json(root/'draft/call.json')['connection_failures'][0]['outcome_certainty'],'not_sent')
            with patch('docflow.model.time.time',return_value=10**12):
                body=ModelClient(configuration(),lambda p:(calls.append(1) or response('received'))).call(root,'draft',[])
            self.assertEqual(body,'received');self.assertEqual(len(calls),2)


class CompressionAndProtocolTests(unittest.TestCase):
    def test_logical_compressed_hash_and_parsed_original_hash_are_both_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();logical=root/'logical.txt';logical.write_text('packed');original=root/'original.txt';original.write_text('source original')
            cfg={'state_dir':str(root/'state'),'compression_registry':'fixture','llm_enabled':False,'reuse_robot1_artifacts':False}
            with patch('compression_versions.parsing_source',return_value=(original,digest(original),{'verified':True})):
                output,provenance=render(cfg,logical,logical.name,digest(logical),root/'state/jobs/current')
            report=read_json(Path(provenance['parse_report_path']))
            self.assertEqual(report['source_hash'],digest(logical));self.assertEqual(report['original_source_hash'],digest(original))
            self.assertIn('source original',output.read_text());self.assertEqual(logical.read_text(),'packed')

    def test_compressed_parse_failure_keeps_original_coverage_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();logical=root/'logical.txt';logical.write_text('packed');original=root/'original.txt';original.write_bytes(b'\x00')
            cfg={'state_dir':str(root/'state'),'compression_registry':'fixture','llm_enabled':False,'reuse_robot1_artifacts':False}
            with patch('compression_versions.parsing_source',return_value=(original,digest(original),{'verified':True})):
                with self.assertRaises(ParseFailure) as error:render(cfg,logical,logical.name,digest(logical),root/'state/jobs/current')
            report=read_json(Path(error.exception.report_path))
            self.assertEqual(report['source_hash'],digest(logical));self.assertEqual(report['original_source_hash'],digest(original))

    def test_native_control_words_and_images_are_literal_source_data(self):
        from docflow.parser import markdown
        from markdown_assets import has_image_reference
        from parse_quality import summarize_units
        unit={'id':'u1','label':'原文批注','method':'native_text','body':'# 正文\n![literal](never-fetch.png)\n#解析记录（非原文）\n实际原文'}
        publication=markdown({'name':'fixture.docx'},{'units':[unit]})
        self.assertFalse(has_image_reference(publication));self.assertIn('实际原文',publication)
        self.assertEqual(summarize_units([unit],[unit],'sha')['status'],'complete')

    def test_forged_no_text_hashes_do_not_pass_without_real_response_content(self):
        from parse_control import run
        from PIL import Image
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();cache=root/'cache';work=root/'job';(cache/'prepared').mkdir(parents=True);work.mkdir()
            image=cache/'prepared/image.png';Image.new('RGB',(8,8),'gray').save(image)
            first=cache/'u2/draft/response.json';second=work/'source-recovery/u2/stage/response.json'
            first.parent.mkdir(parents=True);second.parent.mkdir(parents=True)
            first.write_bytes(response('# 正文\n实际有文字')[0]);second.write_bytes(response('# 正文\n[[DOCFLOW_NO_TEXT]]')[0])
            write_json(cache/'prepared/prepared.json',{'source_hash':'input','native':{}})
            units=[{'id':'u1','label':'source','text':'','image':None,'body':'有效正文','issues':[]},
             {'id':'u2','label':'photo','text':'','image':'image.png','body':'','issues':['模型报告无可辨认文字，待人工确认'],
              'no_text_verification':{'method':'second_source_image_read','verified':True,'image_sha256':digest(image),
               'response_path':str(second),'response_sha256':digest(second),'first_response':{'path':str(first),'sha256':digest(first)}}}]
            cfg=configuration();client=ModelClient(cfg,lambda p:response('unused'))
            with patch('parse_control.recover_units',side_effect=lambda units,*a,**k:units):
                with self.assertRaises(WorkflowError) as error:
                    run({},SimpleNamespace(store=SimpleNamespace(root=root)),{'extension':'.png'},cache/'parse-manifest.json',
                        units,units,'sha','input',work,cfg,client,Path(cfg['parser_prompt']).read_text())
            self.assertEqual(error.exception.code,'INTEGRITY_ERROR')


class StartupGateTests(unittest.TestCase):
    def test_new_writer_readiness_does_not_touch_state_or_api_before_release(self):
        import worker
        from test_worker import Remote
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir();markdown=root/'md';markdown.mkdir()
            state=root/'state';baseline=root/'baseline.json';baseline.write_text('[]');key=root/'key.txt';key.write_text('isolated')
            cfg={'source_root':str(source),'markdown_root':str(markdown),'state_dir':str(state),'api_key_file':str(key),
                 'baseline_mapping':str(baseline),'knowledge_base_id':'kb','weknora_base_url':'http://127.0.0.1:0','folder_root':'scope'}
            instance=Sync(cfg,api=Remote());instance.persist();before=digest(state/'state.json')
            write_json(state/'deployment-hold.json',{'release':'isolated-test'})
            config=root/'config.json';config.write_text(json.dumps(cfg))
            child=subprocess.Popen([sys.executable,worker.__file__,'watch','--config',str(config)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not (state/'worker-ready.json').is_file() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((state/'worker-ready.json').is_file())
                ready=read_json(state/'worker-ready.json');self.assertEqual(ready['state_sha256'],before)
                self.assertEqual(digest(state/'state.json'),before);self.assertFalse((state/'health.json').exists())
                child.send_signal(signal.SIGTERM);_,error=child.communicate(timeout=15)
                self.assertEqual(child.returncode,0,error.decode());self.assertEqual(read_json(state/'state.json')['records'],{})
            finally:
                if child.poll() is None:child.kill();child.wait()


if __name__=='__main__':unittest.main()
