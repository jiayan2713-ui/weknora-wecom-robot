import json,tempfile,unittest,contextlib,io,hashlib
from pathlib import Path
from unittest.mock import patch
from email.parser import BytesParser
from email.policy import default
import test_worker as fakes
from original_ingest import wants_original
from table_native import inventory,coverage

class Remote(fakes.Remote):
 def __init__(self):super().__init__();self.truncate=False
 def request(self,route,data=None,headers=None,method=None):
  if route.startswith('/knowledge/') and method=='PUT':
   kid=route.rsplit('/',1)[-1];self.docs[kid].update(json.loads(data));return 200,{'success':True,'data':self.docs[kid]}
  if route.endswith('/knowledge/file'):
   message=BytesParser(policy=default).parsebytes(('Content-Type: '+headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+data)
   parts={p.get_param('name',header='content-disposition'):p for p in message.iter_parts()}
   self.last_metadata=json.loads(parts['metadata'].get_payload(decode=True));self.last_mime=parts['file'].get_content_type();self.last_process=json.loads(parts['process_config'].get_payload(decode=True)) if 'process_config' in parts else None
   try:result=super().request(route,data,headers,method)
   except ConnectionError:
    if self.last_metadata.get('sync_ingest_mode')=='original':next(reversed(self.docs.values()))['file_type']='csv'
    raise
   if self.last_metadata.get('sync_ingest_mode')=='original':result[1]['data']['file_type']='csv'
   return result
  if route.startswith('/chunks/') and self.truncate:
   return 200,{'data':[{'chunk_type':'text','is_enabled':True,'content':'missing'}],'total':1}
  return super().request(route,data,headers,method)

class OriginalTests(unittest.TestCase):
 def setUp(self):
  self.f=fakes.WorkerTests();self.f.setUp();self.addCleanup(self.f.doCleanups);f=self.f;f.api=Remote()
  def md(cfg,source,relative,digest,work):
   f.parse_calls+=1;work.mkdir(parents=True,exist_ok=True);p=work/'publication.md';p.write_text('# 旧版解析\n\n'+source.read_text());return p,{'method':'test'}
  f.renderer=md;f.worker=f.create_worker()
  self.preflight=patch('original_ingest.platform_preflight',side_effect=lambda cfg,path:path.read_text());self.preflight.start();self.addCleanup(self.preflight.stop)
 def enable(self):
  f=self.f;f.cfg['parser_skip_extensions']=['.csv'];f.worker.cfg=f.cfg;f.worker.renderer=lambda *a:self.fail('self parser invoked for CSV')
 def finish(self):
  f=self.f
  for _ in range(3):
   f.cycle()
   for k in f.api.docs:f.api.complete(k)
 def test_existing_markdown_switches_only_after_original_verified(self):
  f=self.f;p=f.initial('name,value\nalpha,10\n',filename='a.csv');r=next(iter(f.worker.state['records'].values()));old=r['remote_ids'][0];md=Path(r['md_path']);source=p.read_bytes();self.enable();f.cycle()
  self.assertTrue(md.exists());self.assertIn(old,f.api.docs);self.assertEqual(r['state'],'learning')
  for k in f.api.docs:f.api.complete(k)
  f.cycle();self.assertEqual(r['ingest_mode'],'original');self.assertFalse(md.exists());self.assertEqual(p.read_bytes(),source);self.assertNotIn(old,f.api.docs)
  self.assertEqual(f.api.last_mime,'text/csv');self.assertNotIn('_process_config',f.api.last_metadata)
  calls=f.api.upload_calls;f.cycle();self.assertEqual(f.api.upload_calls,calls)
 def test_failure_keeps_old_knowledge_and_records_without_md_fallback(self):
  f=self.f;p=f.initial('name,value\nalpha,10\n',filename='a.csv');r=next(iter(f.worker.state['records'].values()));old=r['remote_ids'][0];self.enable();f.cycle()
  for k in f.api.docs:f.api.complete(k)
  f.api.truncate=True;f.cycle();self.assertEqual(r['state'],'original_ingest_hold');self.assertEqual(r['remote_ids'],[old]);self.assertIn(old,f.api.docs);self.assertTrue(Path(r['md_path']).exists())
  calls=f.api.upload_calls;f.cycle();self.assertEqual(f.api.upload_calls,calls)
 def test_lost_upload_response_recovers_original_without_second_upload(self):
  f=self.f;p=f.initial('name,value\nalpha,10\n',filename='a.csv');self.enable();f.api.drop_response_once=True;f.cycle();calls=f.api.upload_calls
  for k in f.api.docs:f.api.complete(k)
  f.now+=31;f.cycle();r=next(iter(f.worker.state['records'].values()));self.assertEqual(r['ingest_mode'],'original');self.assertEqual(f.api.upload_calls,calls)
 def test_shared_old_entry_survives_until_last_source_switches(self):
  f=self.f;f.initial('name,value\nalpha,10\n',filename='a.csv');f.initial('name,value\nalpha,10\n',filename='b.csv');records=list(f.worker.state['records'].values());old=records[0]['remote_ids'][0];self.assertEqual(records[1]['remote_ids'],[old]);self.enable();f.cfg['direct_migration_jobs_per_cycle']=1;f.cfg['direct_migration_allowlist']=['a.csv']
  f.cycle()
  for k in f.api.docs:f.api.complete(k)
  f.cycle();self.assertIn(old,f.api.docs);f.cfg.pop('direct_migration_allowlist')
  for k in f.api.docs:f.api.complete(k)
  f.cycle();self.assertNotIn(old,f.api.docs)
 def test_direct_change_rename_and_delete_keep_source_identity(self):
  f=self.f;p=f.initial('name,value\nalpha,10\n',filename='a.csv');identity=next(iter(f.worker.state['records']));self.enable();self.finish()
  p.write_text('name,value\nbeta,25\n');self.finish();self.assertEqual(len(f.worker.state['records']),1)
  new=p.with_name('renamed.csv');p.rename(new);self.finish();self.assertEqual(f.worker.state['records'][identity]['path'],'renamed.csv')
  f.cfg['automatic_deletion_enabled']=True;new.unlink();f.cycle();f.cycle();f.cycle();self.assertEqual(f.worker.state['records'][identity]['state'],'deleted')
 def test_case_insensitive_config_and_validation(self):
  self.assertTrue(wants_original({'parser_skip_extensions':['.CSV']},'a.CsV'))
  with self.assertRaises(ValueError):wants_original({'parser_skip_extensions':['.pdf']},'a.csv')
 def test_native_csv_leading_zero_and_missing_cell_gate(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'a.csv';p.write_text('id,value\n001,10\n');n=inventory(p)
   self.assertTrue(coverage(n,'id,value\n001,10')['passed']);self.assertFalse(coverage(n,'id,value\n1,10')['passed'])

 def test_sheet_names_and_saved_control_values_are_required(self):
  n={'sheets':['费用','说明'],'cells':[{'value':'alpha','type':'text'}],'controls':[{'part':'xl/jdecontrols/jdecontrol1.xml','checked':'true'}],'uncached_formulas':[]}
  self.assertFalse(coverage(n,'alpha')['passed'])
  text='费用 说明 alpha WPS_CHECKBOX xl/jdecontrols/jdecontrol1.xml saved=true'
  self.assertTrue(coverage(n,text)['passed'])
  self.assertFalse(coverage(n,text.replace('saved=true','saved=false'))['passed'])

 def test_direct_original_paths_rebind_on_mac_migration(self):
  import mac_deploy
  f=self.f;home=Path(f.temp.name)/'install';source=f.source;p=source/'a.csv';p.write_text('name,value\nalpha,10\n');digest=hashlib.sha256(p.read_bytes()).hexdigest()
  old={'state_dir':'/old/state','source_root':'/old/source','markdown_root':'/old/md','knowledge_base_id':'kb','parser_skip_extensions':['.csv']}
  base=home/'private/sync';mac_deploy.write(base/'source-config.json',old)
  mac_deploy.write(base/'state.json',{'records':{'id':{'path':'a.csv','state':'learning','source_hash':digest,'remote_ids':['k1'],'ingest_mode':'original','original_artifact':'/old/state/jobs/j/a.csv','direct_report_path':'/old/state/jobs/j/direct-validation.json','pending':{'artifact':'/old/state/jobs/j/a.csv','native_inventory':'/old/state/jobs/j/native-inventory.json','validation':'/old/state/jobs/j/direct-validation.json'}}}})
  mac_deploy.write(base/'source-fingerprints.json',{'a.csv':digest});(base/'markdown').mkdir()
  office=Path(f.temp.name)/'soffice';office.touch();poppler=Path(f.temp.name)/'pdftoppm';poppler.touch()
  with patch.object(mac_deploy.shutil,'which',side_effect=lambda n:str(office if n=='soffice' else poppler)),patch.object(mac_deploy,'container',return_value='new-reader'):
   mac_deploy.bind(home,source)
  restored=mac_deploy.read(home/'state/state.json')['records']['id'];cfg=mac_deploy.read(home/'config.json')
  self.assertEqual(restored['original_artifact'],str(home/'state/jobs/j/a.csv'))
  self.assertEqual(restored['pending']['native_inventory'],str(home/'state/jobs/j/native-inventory.json'))
  self.assertEqual(cfg['table_docreader_container'],'new-reader');self.assertEqual(restored['remote_ids'],['k1'])

class ChunkCoverageTests(unittest.TestCase):
 def test_overlapping_paged_chunks_preserve_complete_cell(self):
  from original_ingest import chunk_text
  class API:
   def request(self,route):return 200,{'data':[{'chunk_type':'text','is_enabled':True,'content':'abcdef','start_at':0,'end_at':6},{'chunk_type':'text','is_enabled':True,'content':'defghi','start_at':3,'end_at':9}], 'total':2}
  class Worker:api=API()
  self.assertIn('abcdefghi',chunk_text(Worker(),'k'))
 def test_conflicting_offsets_do_not_hide_data_loss(self):
  from original_ingest import chunk_text
  class API:
   def request(self,route):return 200,{'data':[{'chunk_type':'text','is_enabled':True,'content':'abcdef','start_at':0,'end_at':6},{'chunk_type':'text','is_enabled':True,'content':'XXXghi','start_at':3,'end_at':9}], 'total':2}
  class Worker:api=API()
  with self.assertRaisesRegex(ValueError,'span_conflict'):chunk_text(Worker(),'k')

class Binary64CoverageTests(unittest.TestCase):
 def test_equivalent_binary64_spelling_without_broad_numeric_tolerance(self):
  native={'cells':[{'value':'7.0000000000000007E-2','type':'n'}],'sheets':['CSV'],'controls':[],'uncached_formulas':[]}
  self.assertTrue(coverage(native,'0.07')['passed'])
  self.assertFalse(coverage(native,'0.0701')['passed'])
  native['cells'][0]['value']='9007199254740993'
  self.assertFalse(coverage(native,'9007199254740992')['passed'])
