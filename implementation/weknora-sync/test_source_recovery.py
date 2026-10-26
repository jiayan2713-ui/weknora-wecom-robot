import copy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from docflow.model import ModelClient
from parse_quality import summarize_units
from source_recovery import recover_units, NO_TEXT
from reconcile import hashes
from xlsx_style_compatibility import compatible_copy


def legacy_control(cfg, workflow, doc, cache, expected, units, source_hash, parser_input_hash, work, config, client, prompt):
    from parse_quality import summarize_units
    return expected, units, summarize_units(expected, units, source_hash)




class SourceRecoveryTests(unittest.TestCase):
    def test_only_bad_units_are_reread_and_no_text_keeps_all_empty_hold(self):
        from docflow.config import configuration
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'prepared').mkdir();Image.new('RGB',(10,10),'gray').save(root/'prepared/a.png')
            cfg=configuration({'model_review':False,'parser_workers':1})
            calls=[]
            def transport(payload):
                calls.append(payload)
                return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'# 正文\n[[DOCFLOW_NO_TEXT]]'}}]}).encode(),200,'test'
            client=ModelClient(cfg,transport,sleep=lambda _:None)
            good={'id':'u1','label':'text','body':'真实业务原文','issues':[]}
            bad={'id':'u2','label':'photo','image':'a.png','body':'','issues':[NO_TEXT]}
            first=root/'u2/draft';first.mkdir(parents=True)
            response=first/'response.json';response.write_text(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'# 正文\n[[DOCFLOW_NO_TEXT]]'}}]}))
            (first/'call.json').write_text(json.dumps({'response_sha256':hashes(response)['sha256']}))
            original=copy.deepcopy([good,bad])
            result=recover_units(original,root/'parse-manifest.json',root/'new',cfg,client,Path(cfg['parser_prompt']).read_text())
            self.assertEqual(len(calls),1);self.assertEqual(original,[good,bad]);self.assertEqual(result[0],good)
            report=summarize_units([good,bad],result,'sha')
            self.assertEqual(report['completed_units'],2);self.assertEqual(report['status'],'complete_with_warnings')
            self.assertEqual(summarize_units([bad],[result[1]],'sha')['status'],'no_readable_text')
            self.assertFalse(result[1]['no_text_verification']['human_review'])
            self.assertNotIn('no_text_verification',original[1])

    def test_empty_fill_copy_preserves_cells_and_original(self):
        import openpyxl,zipfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'test.xlsx';book=openpyxl.Workbook();book.active['A1']='原文';book.active['B2']='=1+2';book.save(source)
            with zipfile.ZipFile(source) as archive:entries=[(i,archive.read(i.filename)) for i in archive.infolist()]
            with zipfile.ZipFile(source,'w') as archive:
                for entry,body in entries:
                    if entry.filename=='xl/styles.xml':body=body.replace(b'<patternFill/>',b'',1)
                    archive.writestr(entry,body)
            before=hashes(source)['sha256'];target,receipt=compatible_copy(source,root/'work')
            self.assertTrue(receipt['all_other_zip_entries_unchanged']);self.assertEqual(hashes(source)['sha256'],before)
            with zipfile.ZipFile(source) as a,zipfile.ZipFile(target) as b:self.assertEqual(a.read('xl/worksheets/sheet1.xml'),b.read('xl/worksheets/sheet1.xml'))
            parsed=openpyxl.load_workbook(target);self.assertEqual(parsed.active['B2'].value,'=1+2');parsed.close()


class SourceCheckpointIntegrationTests(unittest.TestCase):
    def test_checkpoint_reuses_good_unit_and_preserves_old_cache(self):
        import os
        from unittest.mock import patch
        import fitz
        from docflow.runner import Workflow
        from transform_local import render
        from parse_policy import ParseFailure
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'file.pdf';doc=fitz.open()
            for text in ('readable page one','readable page two'):
                page=doc.new_page();page.insert_text((72,72),text)
            doc.save(source);doc.close()
            secret=root/'model.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'fixture'}));secret.chmod(0o600)
            cfg={'state_dir':str(root),'reuse_robot1_artifacts':False,'llm_enabled':True,'allow_model_calls':True,
                 'model_secrets_file':str(secret),'parser_config':{'model_review':False,'parser_workers':1,'empty_body_policy':'review'}}
            calls=[]
            def factory(config):
                def transport(payload):
                    calls.append(payload)
                    body='# 正文\n原文费用十万元。' if len(calls)!=2 else '# 正文\n![图示](invalid_asset)'
                    return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':body}}]}).encode(),200,'fixture'
                return ModelClient(config,transport,sleep=lambda _:None)
            def workflow(directory):return Workflow(directory,client_factory=factory)
            old=root/'jobs/old'
            with patch.dict(os.environ),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=factory):
                with patch('parse_control.run',side_effect=legacy_control), self.assertRaises(ParseFailure):render(cfg,source,source.name,hashes(source)['sha256'],old)
                before={str(p):hashes(p)['sha256'] for p in (old/'docflow').rglob('*') if p.is_file()}
                output,provenance=render({**cfg,'_source_checkpoint_work':str(old)},source,source.name,hashes(source)['sha256'],root/'jobs/new')
            self.assertEqual(len(calls),3);self.assertNotIn('![',output.read_text())
            self.assertEqual(provenance['parse_report']['completed_units'],2)
            self.assertEqual(before,{str(p):hashes(p)['sha256'] for p in (old/'docflow').rglob('*') if p.is_file()})


class MarkdownAssetsTests(unittest.TestCase):
    def test_punctuation_is_not_an_image_but_real_assets_still_block(self):
        from markdown_assets import has_image_reference
        self.assertFalse(has_image_reference('美国 EB-5![红字结束] [说明](https://example.com)'))
        self.assertFalse(has_image_reference('原文\\![字面内容]'))
        for text in ('![图](image_placeholder)', '![图](https://example.com/a.png)',
                     '![图][r]\n[r]: image.png', '![r]\n[r]: image.png'):
            self.assertTrue(has_image_reference(text))


class NativeSourceRecoveryTests(unittest.TestCase):
    def test_native_comment_is_preserved_as_data_without_model_or_executing_instruction(self):
        from unittest.mock import Mock
        from source_recovery import MISSING
        unit={'id':'u1','label':'原文附属内容 word/comments.xml','image':None,
              'text':'更新pdf，删除CIIP部分','body':'','issues':[MISSING]}
        client=Mock()
        results=recover_units([unit],Path('/unused/cache'),Path('/unused/job'),{'parser_workers':1},client,'')
        self.assertEqual(results[0]['body'],unit['text']);self.assertEqual(unit['body'],'')
        client.call.assert_not_called()


class ImageMarkupRecoveryTests(unittest.TestCase):
    def test_one_persisted_correction_then_cached_replay_without_new_calls(self):
        from docflow.config import configuration
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'prepared').mkdir();Image.new('RGB',(10,10),'gray').save(root/'prepared/a.png')
            cfg=configuration({'model_review':False,'parser_workers':1});calls=[]
            def transport(payload):
                calls.append(payload)
                body='# 正文\n![图](FSTB Logo)' if len(calls)==1 else '# 正文\nFSTB\n原文费用十万元。'
                return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':body}}]}).encode(),200,'test'
            client=ModelClient(cfg,transport,sleep=lambda _:None)
            units=[{'id':'u1','label':'page1','image':'a.png','body':'![图](fake.png)','issues':[]}]
            args=(units,root/'parse-manifest.json',root/'new',cfg,client,Path(cfg['parser_prompt']).read_text())
            result=recover_units(*args);self.assertEqual(len(calls),2);self.assertEqual(result[0]['body'],'FSTB\n原文费用十万元。')
            self.assertEqual(recover_units(*args),result);self.assertEqual(len(calls),2)


class RecoveryCheckpointReplayTests(unittest.TestCase):
    def test_failed_checkpoint_replays_first_response_and_only_calls_new_correction(self):
        import os
        from unittest.mock import patch
        import fitz
        from docflow.runner import Workflow
        from transform_local import render
        from parse_policy import ParseFailure
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'file.pdf';doc=fitz.open()
            for text in ('page one','page two'):
                page=doc.new_page();page.insert_text((72,72),text)
            doc.save(source);doc.close()
            secret=root/'model.json';secret.write_text(json.dumps({'DASHSCOPE_API_KEY':'fixture'}));secret.chmod(0o600)
            cfg={'state_dir':str(root),'reuse_robot1_artifacts':False,'llm_enabled':True,'allow_model_calls':True,
                 'model_secrets_file':str(secret),'parser_config':{'model_review':False,'parser_workers':1,'empty_body_policy':'review'}}
            calls=[];legacy=[False]
            def factory(config):
                def transport(payload):
                    calls.append(payload)
                    body='# 正文\n原文费用十万元。' if len(calls) in (1,4) else '# 正文\n![图示](invalid_asset)'
                    return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':body}}]}).encode(),200,'fixture'
                client=ModelClient(config,transport,sleep=lambda _:None);original=client.call
                def call(directory,stage,messages):
                    if legacy[0] and stage=='source-text-without-image-markup-v1':raise ValueError('source_recovery_body_invalid')
                    return original(directory,stage,messages)
                client.call=call;return client
            def workflow(directory):return Workflow(directory,client_factory=factory)
            old=root/'jobs/old';failed=root/'jobs/failed'
            with patch.dict(os.environ),patch('docflow.runner.Workflow',side_effect=workflow),patch('docflow.model.ModelClient',side_effect=factory):
                with patch('parse_control.run',side_effect=legacy_control), self.assertRaises(ParseFailure):render(cfg,source,source.name,hashes(source)['sha256'],old)
                legacy[0]=True
                with self.assertRaises(ParseFailure):render({**cfg,'_source_checkpoint_work':str(old)},source,source.name,hashes(source)['sha256'],failed)
                self.assertEqual(len(calls),3);legacy[0]=False
                before={str(p):hashes(p)['sha256'] for p in failed.rglob('*') if p.is_file()}
                output,provenance=render({**cfg,'_source_checkpoint_work':str(failed)},source,source.name,hashes(source)['sha256'],root/'jobs/recovered')
            self.assertEqual(len(calls),4);self.assertEqual(provenance['parse_report']['completed_units'],2)
            self.assertNotIn('![',output.read_text())
            self.assertEqual(before,{str(p):hashes(p)['sha256'] for p in failed.rglob('*') if p.is_file()})


if __name__ == "__main__":
    unittest.main()
