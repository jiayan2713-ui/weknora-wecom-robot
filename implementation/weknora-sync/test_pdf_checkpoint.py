import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from docflow.model import ModelClient
from docflow.runner import Workflow
from parse_policy import ParseFailure
from reconcile import hashes
from transform_local import clean_pdf_placeholders, render
from worker import Sync


def legacy_control(cfg, workflow, doc, cache, expected, units, source_hash, parser_input_hash, work, config, client, prompt):
    from parse_quality import summarize_units
    return expected, units, summarize_units(expected, units, source_hash)




class PdfCheckpointTests(unittest.TestCase):
    def test_cleanup_preserves_caption_and_all_real_image_references(self):
        units=[{'id':'u0001','body':'前文 ![书籍封面](image_placeholder) 后文 ![真实图片](asset.png)','issues':[]}]
        result,changes=clean_pdf_placeholders(copy.deepcopy(units))
        self.assertEqual(result[0]['body'],'前文 书籍封面 后文 ![真实图片](asset.png)')
        self.assertEqual(changes,[{'unit_id':'u0001','removed_dummy_image_links':1}])
        self.assertEqual(units[0]['issues'],[])

    def test_pdf_checkpoint_reuses_units_without_new_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'fixture.pdf'
            doc=fitz.open()
            for text in ('Source page one','Source page two'):
                page=doc.new_page();page.insert_text((72,72),text)
            doc.save(source);doc.close()
            secret=root/'model.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'isolated-fixture'}));secret.chmod(0o600)
            cfg={'state_dir':str(root),'reuse_robot1_artifacts':False,'llm_enabled':True,'allow_model_calls':True,
                 'model_secrets_file':str(secret),'parser_config':{'model_review':False,'parser_workers':1,'empty_body_policy':'review'}}
            calls=[]
            def factory(config):
                def transport(payload):
                    calls.append(payload)
                    body='# 正文\n项目费用10万元。'
                    if len(calls)==1:body+=' ![书籍封面](image_placeholder)'
                    return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':body}}]}).encode(),200,'fixture'
                return ModelClient(config,transport,sleep=lambda _:None)
            def workflow(directory):return Workflow(directory,client_factory=factory)
            old=root/'jobs/old'
            with patch.dict(os.environ),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=factory):
                with patch('parse_control.run',side_effect=legacy_control), patch('transform_local.clean_pdf_placeholders',side_effect=lambda units:(units,[])):
                    with self.assertRaises(ParseFailure) as error:
                        render(cfg,source,source.name,hashes(source)['sha256'],old)
                self.assertEqual(error.exception.code,'IMAGE_ASSETS_UNSUPPORTED')
                self.assertEqual(len(calls),2)
                before={str(p):hashes(p)['sha256'] for p in (old/'docflow').rglob('*') if p.is_file()}
                target=root/'jobs/new'
                result,provenance=render({**cfg,'_pdf_checkpoint_work':str(old)},source,source.name,hashes(source)['sha256'],target)
            self.assertEqual(len(calls),2)
            self.assertNotIn('![',result.read_text())
            self.assertEqual(result.read_text().count('项目费用10万元。'),2)
            report=json.loads(Path(provenance['parse_report_path']).read_text())
            self.assertEqual(report['completed_units'],2)
            self.assertEqual(report['expected_units'],2)
            self.assertEqual(report['image_placeholder_cleanup'][0]['unit_id'],'u0001')
            self.assertFalse(provenance['human_review'])
            self.assertEqual(before,{str(p):hashes(p)['sha256'] for p in (old/'docflow').rglob('*') if p.is_file()})

    def test_pdf_checkpoint_request_rejects_unrelated_failure_or_wrong_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir();p=source/'x.pdf';p.write_bytes(b'pdf-fixture')
            md=root/'md';md.mkdir();state=root/'state';baseline=root/'baseline.json';baseline.write_text('[]')
            cfg={'source_root':str(source),'markdown_root':str(md),'state_dir':str(state),'knowledge_base_id':'kb',
                 'weknora_base_url':'http://fake','folder_root':'folder','baseline_mapping':str(baseline)}
            worker=Sync(cfg,api=object());job=state/'jobs/old';job.mkdir(parents=True);(job/'x.pdf').write_bytes(p.read_bytes())
            report={'source_hash':hashes(p)['sha256'],'status':'incomplete','expected_units':1,'completed_units':1,
                    'failure':{'code':'IMAGE_ASSETS_UNSUPPORTED'}}
            path=job/'parse-report.json';path.write_text(json.dumps(report));record={'path':'x.pdf'}
            self.assertEqual(worker.checked_parse_checkpoint(record,path)['source_hash'],report['source_hash'])
            report['failure']['code']='INVALID_DOCUMENT';path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'source_or_status_mismatch'):worker.checked_parse_checkpoint(record,path)
            report['failure']['code']='IMAGE_ASSETS_UNSUPPORTED';path.write_text(json.dumps(report));(job/'x.pdf').write_bytes(b'other')
            with self.assertRaisesRegex(ValueError,'original_mismatch'):worker.checked_parse_checkpoint(record,path)


if __name__=='__main__':unittest.main()
