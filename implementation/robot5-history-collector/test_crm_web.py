import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector
import crm_sync as crm
import crm_web
from answer_format import AnswerRenderer,render_answer
from test_crm_sync import FakeClient, record, schema


class WebSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=collector.connect(self.root);self.client=FakeClient()
        self.config={'base_url':'https://api.mingdao.com','worksheet_id':'target'}
        self.qa=record(answer='# 标题\n\n**原文**\n下一行\n\n| 项目 | 费用 |\n| --- | --- |\n| A | 123 |')
        crm.run(self.db,[self.qa],self.config,self.client,limit=10,budget=30)
        self.original=copy.deepcopy(self.client.rows['remote1'])
        self.schema=schema();self.schema['controls'].append({'controlName':crm_web.FIELD_NAME,'controlId':'web','type':41})

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def sync(self):
        with patch.object(crm,'Client',return_value=self.client),patch.object(self.client,'list_access',return_value=self.schema):
            return crm_web.run(self.db,[self.qa],self.config,limit=10,budget=30)

    def test_formats_graded_answer_without_writing_source_and_repeats_safely(self):
        row=self.client.rows['remote1'];row['human0']='已评分';row['human1']='保留人工备注'
        original=copy.deepcopy(row)
        result=self.sync();self.assertEqual(result['verified'],1)
        html=row['web']
        for fragment in ['<h1>标题</h1>','<strong>原文</strong>','<table ', '>123</td>','<br />']:
            self.assertIn(fragment,html)
        self.assertEqual({k:v for k,v in row.items() if k!='web'},original)
        self.assertEqual(self.sync()['attempted'],0)
        self.assertEqual(self.client.creates,1)

    def test_preserves_existing_and_manual_web_changes(self):
        row=self.client.rows['remote1'];row['web']='<p>人工排版</p>'
        result=self.sync();self.assertEqual(result['counts'],{'preserved':1})
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(row['web'],'<p>人工排版</p>')
        self.assertEqual(self.client.updates,0)

    def test_updates_owned_html_when_source_changes(self):
        self.sync();self.qa['answer']='新答案 **加粗**'
        crm.run(self.db,[self.qa],self.config,self.client,limit=10,budget=30)
        self.assertEqual(self.sync()['verified'],1)
        self.assertIn('<strong>加粗</strong>',self.client.rows['remote1']['web'])
        self.client.rows['remote1']['web']='手动改动'
        self.qa['answer']='再次修改'
        crm.run(self.db,[self.qa],self.config,self.client,limit=10,budget=30)
        self.assertEqual(self.sync()['counts'],{'preserved':1})
        self.assertEqual(self.client.rows['remote1']['web'],'手动改动')

    def test_wrong_type_and_stale_answer_block_before_writing(self):
        self.schema['controls'][-1]['type']=2
        with self.assertRaisesRegex(crm.SyncError,'requires_rich_text'):self.sync()
        self.schema['controls'][-1]['type']=41
        self.client.rows['remote1']['answer']='远端已经变更'
        result=self.sync();self.assertEqual(result['counts'],{'blocked':1})
        self.assertEqual(self.client.updates,0)

    def test_invalid_presentation_blocks_only_affected_record(self):
        from answer_format import PresentationError,render_answer
        other=record('valid',answer='正常答案')
        crm.run(self.db,[other],self.config,self.client,limit=10,budget=30)
        def render(source):
            if source==self.qa['answer']:raise PresentationError('source_text_missing')
            return render_answer(source)
        with patch.object(crm,'Client',return_value=self.client),patch.object(self.client,'list_access',return_value=self.schema),patch.object(crm_web,'render_answer',side_effect=render):
            result=crm_web.run(self.db,[self.qa,other],self.config,limit=10,budget=30)
        self.assertEqual(result['counts'],{'blocked':1,'synced':1})
        self.assertNotIn('web',self.client.rows['remote1'])
        self.assertIn('正常答案',self.client.rows['remote2']['web'])
        self.assertEqual(self.db.execute("SELECT error_code FROM crm_web_records WHERE status='blocked'").fetchone()[0],'presentation_source_text_missing')

    def test_write_success_timeout_recovers_without_overwrite_or_duplicate(self):
        update=self.client.update
        def timeout(row,values):
            update(row,values);raise crm.SyncError('transport_outcome_unknown',True)
        with patch.object(self.client,'update',side_effect=timeout):
            self.assertEqual(self.sync()['counts'],{'retry':1})
        self.db.execute('UPDATE crm_web_records SET next_retry=0');self.db.commit()
        self.assertEqual(self.sync()['verified'],1)
        self.assertEqual(self.client.updates,1)
        self.assertEqual(self.client.creates,1)

    def test_html_and_unsafe_links_are_not_executable(self):
        rendered=render_answer('<script>alert(1)</script>\n\n[x](javascript:alert)\n\n[官网](https://example.com/a?a=1&b=2)')
        self.assertNotIn('<script>',rendered);self.assertNotIn('href="javascript:',rendered)
        self.assertIn('&lt;script&gt;',rendered);self.assertIn('href="https://example.com/',rendered)
        self.assertEqual(render_answer(''), '')
        self.assertEqual(AnswerRenderer().safe_url('http://[invalid'),'#blocked-link')
        long='原文🙂'*20000
        self.assertIn(long,render_answer(long))

    def test_web_failure_does_not_hide_successful_original_sync(self):
        private=self.root/'private';private.mkdir()
        crm.atomic_json(private/'crm-config.json',{**self.config,'app_key':'fake','app_sign':'fake','enabled':True})
        with patch.object(crm,'Client',return_value=self.client),patch.object(crm_web,'run',side_effect=ValueError('format issue')):
            result=crm.tick(self.db,[self.qa],self.root)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['web_format']['status'],'failed')
        self.assertEqual(self.client.rows['remote1']['answer'],self.qa['answer'])

    def test_normal_tick_formats_new_answers_without_duplicate_rows(self):
        private=self.root/'private';private.mkdir()
        crm.atomic_json(private/'crm-config.json',{**self.config,'app_key':'fake','app_sign':'fake','enabled':True})
        new=record('new',answer='## 新问题的答案\n\n- 要点')
        incomplete=record('incomplete');incomplete['status']='未完成'
        records=[self.qa,new,incomplete]
        with patch.object(crm,'Client',return_value=self.client),patch.object(self.client,'list_access',return_value=self.schema):
            first=crm.tick(self.db,records,self.root)
            second=crm.tick(self.db,records,self.root)
        self.assertEqual(first['created'],1)
        self.assertEqual(first['web_format']['verified'],2)
        self.assertEqual(second['attempted'],0)
        self.assertEqual(second['web_format']['attempted'],0)
        self.assertEqual(len(self.client.rows),2)
        self.assertIn('<h2>新问题的答案</h2>',self.client.rows['remote2']['web'])

    def test_snapshot_preserves_web_queue_for_migration(self):
        import migration,sqlite3
        self.sync()
        binding={'channel_id':'channel','agent_id':'agent'}
        collector.apply_snapshot(self.db,{'source':{'tenant_id':1},'upper':'2026-10-19T00:00:00Z','rows':[]},binding)
        binding_path=self.root/'binding.json';binding_path.write_text(json.dumps(binding))
        destination=self.root.parent/(self.root.name+'-snapshot')
        try:
            migration.snapshot(self.root,binding_path,destination)
            with sqlite3.connect(destination/'history.sqlite3') as copied:
                self.assertEqual(copied.execute('SELECT status,row_id FROM crm_web_records').fetchone(),('synced','remote1'))
        finally:
            import shutil
            if destination.exists():shutil.rmtree(destination)


if __name__=='__main__':unittest.main()
