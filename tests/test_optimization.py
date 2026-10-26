import io,json,os,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from docflow.common import WorkflowError,digest,read_json
from docflow.config import configuration
from docflow.input_policy import unicode_evidence,native_sheet_units
from docflow.model import ModelClient
from docflow.reliability import quality_summary
from docflow.delivery import publish_draft,merge_format_notes,wps_link
from docflow.runner import Workflow
from tests.fixtures import approve,SMALL

class OptimizationTests(unittest.TestCase):
 def test_error_response_redacted_and_classified(self):
  with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DASHSCOPE_API_KEY':'secret-fixture-key'}):
   root=Path(temp).resolve()
   def transport(p):raise urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"code":"data_inspection_failed","message":"secret-fixture-key"}'))
   client=ModelClient(configuration(),transport,sleep=lambda _:None)
   with self.assertRaises(WorkflowError) as e:client.call(root,'draft',[])
   self.assertEqual(e.exception.scope['category'],'content_rejected')
   error=read_json(root/'draft/error-001.json');self.assertNotIn('secret-fixture-key',str(error));self.assertIn('redacted',str(error))
   with self.assertRaises(WorkflowError) as e:client.call(root,'draft',[])
   self.assertEqual(e.exception.scope['category'],'content_rejected');self.assertEqual(len(read_json(root/'draft/call.json')['attempts']),1)
 def test_unicode_saved_without_silent_deletion(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();s,n=unicode_evidence('价格100\ud800元',12,root)
   self.assertEqual(s,'');self.assertIn('第12页',n['text']);saved=read_json(root/'unicode-page-0012.json');self.assertEqual(saved['invalid'][0]['codepoint'],'U+D800');self.assertEqual(saved['original_unicode_escape'].encode().decode('unicode_escape'),'价格100\ud800元')
 def test_native_large_cell_preserved_and_bounded(self):
  sheet={'name':'原表','state':'hidden','range':'A1:B2','merges':['A1:B1'],'cells':[{'coordinate':'A1','value':'中'*25000,'cached':3,'type':'s','format':'General','comment':'保留批注'}]}
  units=native_sheet_units(sheet,4000);self.assertGreater(len(units),3);self.assertTrue(all(len(u['text'])<=4000 for u in units))
  parts=[u['native_body'].split('：',1)[1] for u in units if '单元格' in u['label']];restored=json.loads(''.join(parts));self.assertEqual(restored,sheet['cells'][0])
 def test_quality_does_not_equate_empty_with_approved(self):
  q=quality_summary([{'body':'','issues':[]},{'body':'100元','issues':[]}]);self.assertEqual(q['empty_units'],1);self.assertEqual(q['content_status'],'needs_confirmation');self.assertFalse(q['human_review_approved'])
 def test_delivery_refresh_and_preserve_human_edit(self):
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp).resolve();a=publish_draft(p,'review.md','未完成');b=publish_draft(p,'review.md','完整内容');self.assertEqual(Path(b['working']).read_text(),'完整内容')
   Path(b['working']).write_text('人工修正');c=publish_draft(p,'review.md','新自动稿');self.assertTrue(c['human_edits_preserved']);self.assertEqual(Path(c['working']).read_text(),'人工修正');self.assertEqual(Path(c['latest_auto']).read_text(),'新自动稿')
 def test_format_provenance_idempotent(self):
  old=[{'section':1,'color':'红字'}];self.assertEqual(merge_format_notes(old,[]),old);self.assertEqual(merge_format_notes(old,old),old)
 def test_wps_paths_unencoded_and_short(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();p=root/('长文件'*25+'.md');p.write_text('原文');s=wps_link(p,root);self.assertNotIn('%E',s);self.assertEqual(Path(s).read_text(),'原文');self.assertLessEqual(len(s),230)
 def fixture_import(self,root,text='## 原PDF · 第1页\n费用12万元。\n'):
  source=root/'original.pdf';source.write_bytes(b'archived-fixture');reading=root/'reading.md';reading.write_text(text);provenance=root/'provenance.json';provenance.write_text('{"source":"fixture"}')
  entry={'name':'原件.pdf',**{k:{'path':str(p),'sha256':digest(p)} for k,p in [('source',source),('reading',reading),('provenance',provenance)]}}
  manifest=root/'manifest.json';manifest.write_text(json.dumps({'schema_version':1,'documents':[entry]}));return {'request_id':'import1','project':'fixture','manifest':str(manifest),'config':SMALL},reading
 def test_import_reading_approve_exact_version_without_model(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();args,reading=self.fixture_import(root);w=Workflow(root/'state',client_factory=lambda _:self.fail('model invoked'))
   s=w.execute('import-review',args);self.assertEqual(s['phase'],'awaiting_review');self.assertEqual(w.execute('import-review',args)['batch_id'],s['batch_id'])
   result=approve(w,s);self.assertEqual(result['phase'],'awaiting_codex',result);table=read_json(result['generation']['evidence_table']);self.assertTrue(any('12万元' in r['body'] for r in table.values()))
 def test_changed_reading_rejected_and_empty_requires_exclusion(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();args,reading=self.fixture_import(root);w=Workflow(root/'state');s=w.execute('import-review',args);reading.write_text('改成13万元');result=approve(w,s);self.assertEqual(result['errors'][0]['code'],'CONTENT_CHANGED')
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();args,reading=self.fixture_import(root,'## 第1页\n**本单元未取得解析正文，请对照原件核查。**\n');w=Workflow(root/'state');s=w.execute('import-review',args);result=approve(w,s);self.assertEqual(result['errors'][0]['code'],'EMPTY_REVIEW_CONTENT')
 def test_duplicate_import_preserves_first_batch_cache(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();args,_=self.fixture_import(root);w=Workflow(root/'state');a=w.execute('import-review',args);b=w.execute('import-review',{**args,'request_id':'import2'});self.assertNotEqual(a['batch_id'],b['batch_id']);self.assertEqual(approve(w,a)['phase'],'awaiting_codex')
 def test_rendered_empty_cannot_be_approved_after_roundtrip(self):
  from docflow.delivery import render_reading
  from docflow.review_import import reading_units
  text,_=render_reading({'name':'test'},[{'id':'u0001','label':'原PDF第1页','body':'','issues':['模型未返回正文，待人工确认']}],Path('/tmp/a.pdf'))
  units=reading_units(text);self.assertEqual(units[-1]['body'],'');self.assertTrue(units[-1]['issues'])
 def test_document_http_failure_does_not_trip_global_stop(self):
  import threading
  from docflow.pipeline import LimitedClient
  class Fake:
   config={}
   def call(self):raise WorkflowError('MODEL_HTTP_ERROR','blocked',scope={'category':'content_rejected','http_status':400})
  stop=threading.Event();client=LimitedClient(Fake(),threading.BoundedSemaphore(1),stop)
  with self.assertRaises(WorkflowError):client.call()
  self.assertFalse(stop.is_set())
 def test_large_sheet_integration_preserves_native_records(self):
  import openpyxl,types
  from PIL import Image
  from docflow.documents import prepare
  for prefix_size in [500,1000]:
   with tempfile.TemporaryDirectory() as temp:
    root=Path(temp).resolve();source=root/'source.xlsx';book=openpyxl.Workbook();book.active['A1']='字'*500;book.save(source);work=root/'prepared';config=configuration({'max_unit_text_chars':1000})
    def fake_run(command,timeout,**kw):
     if '-singlefile' in command:Image.new('RGB',(40,40),'white').save(command[-1]+'.png')
    page=types.SimpleNamespace(extract_text=lambda:'字'*prefix_size,get=lambda *a:[])
    reader=types.SimpleNamespace(is_encrypted=False,pages=[page])
    with patch('docflow.documents.run',side_effect=fake_run),patch('pypdf.PdfReader',return_value=reader):units=prepare(source,work,config)
    self.assertTrue(any(u.get('method')=='native_cells' for u in units));self.assertTrue(all(len(u['text'])<=1000 for u in units));self.assertIn('字'*500,str(units))
 def test_prefix_is_not_discarded(self):
  from docflow.review_import import reading_units
  units=reading_units('# 资料\n最低投资额25万欧元\n## 条件\n年满18岁')
  self.assertIn('25万欧元',units[0]['body'])
 def test_empty_placeholder_with_multiline_note_is_blocked(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();args,_=self.fixture_import(root,'## 第一页\n**本单元未取得解析正文，请对照原件核查。**\n> 解析提示：无正文\n> 需人工补录')
   w=Workflow(root/'state');s=w.execute('import-review',args);self.assertEqual(approve(w,s)['errors'][0]['code'],'EMPTY_REVIEW_CONTENT')
 def test_editing_readable_cannot_approve_old_managed_copy(self):
  from tests.fixtures import workflow,start
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp).resolve();w,_=workflow(root/'state');s=start(w,root);delivery=s['documents'][0]['reading_delivery'];Path(delivery['working']).write_text('人工修改12万元')
   self.assertEqual(approve(w,s)['errors'][0]['code'],'READING_EDITED')
 def test_reading_format_keeps_text_and_style_provenance(self):
  from docflow.delivery import render_reading
  text,records=render_reading({'name':'sample'},[{'id':'u0001','label':'原PPT第8页','body':'# 条件\n[红字开始]30天内付款[红字结束]','issues':[]}],Path('/tmp/source.pptx'))
  self.assertNotIn('[红字开始]',text);self.assertIn('30天内付款',text);self.assertEqual(records[0]['format_notes'][0]['text'],'30天内付款')
if __name__=='__main__':unittest.main()
