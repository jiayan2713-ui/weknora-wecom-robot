import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import collector as c


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = c.connect(self.root)
        self.binding = {'channel_id':'channel','agent_id':'agent'}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def message(self, id, role, request='req', completed=True, content='text'):
        return dict(id=id,session_id='session',request_id=request,role=role,
                    content=content,is_completed=completed,created_at='2026-10-13T00:00:00Z',
                    updated_at='2026-10-13T00:00:00Z',deleted_at=None,im_user_id='user',
                    im_channel_id='channel',platform='wecom',attribution='channel_mapping')

    def apply(self, rows, upper='2026-10-13T01:00:00Z'):
        return c.apply_snapshot(self.db,dict(rows=rows,source={'tenant_id':1},upper=upper),self.binding)

    def test_dedup_completion_recovery_and_original_text(self):
        u = self.message('u','user',content='=SUM(A1:A2)\n同一问题')
        a = self.message('a','assistant',completed=False,content='中途')
        self.assertEqual(self.apply([u,a])['inserted'],2)
        self.assertEqual(self.apply([u,a])['unchanged'],2)
        self.assertEqual(c.qa_records(self.db)[0]['status'],'未完成')
        a['is_completed']=True
        a['content']='最终\n回答'
        self.assertEqual(self.apply([u,a])['updated'],1)
        self.assertEqual(c.qa_records(self.db)[0]['answer'],'最终\n回答')
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0],3)
        self.db.close()
        self.db=c.connect(self.root)
        self.assertEqual(self.apply([u,a])['inserted'],0)
        self.assertEqual(c.qa_records(self.db)[0]['question'],u['content'])

    def test_same_text_distinct_requests_and_out_of_order(self):
        rows=[self.message('a2','assistant','r2'),self.message('u1','user','r1'),
              self.message('a1','assistant','r1'),self.message('u2','user','r2')]
        self.apply(rows)
        self.assertEqual(len(c.qa_records(self.db)),2)
        self.assertEqual({r['user_message_id'] for r in c.qa_records(self.db)},{'u1','u2'})

    def test_missing_id_ambiguous_and_deleted(self):
        self.apply([self.message('u','user',''),self.message('a','assistant','')])
        self.assertEqual({r['status'] for r in c.qa_records(self.db)},{'缺少问题','等待回答'})
        self.apply([self.message('u1','user','x'),self.message('u2','user','x'),self.message('a1','assistant','x')])
        self.assertIn('配对歧义',{r['status'] for r in c.qa_records(self.db)})
        u=self.message('ud','user','d'); a=self.message('ad','assistant','d')
        self.apply([u,a]); a['deleted_at']='2026-10-13T02:00:00Z'; self.apply([a])
        self.assertEqual([r for r in c.qa_records(self.db) if r['request_id']=='d'][0]['status'],'源记录已删除')
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions WHERE message_id='ad'").fetchone()[0],2)

    def test_transaction_failure_does_not_advance_cursor(self):
        self.apply([self.message('u','user')])
        with self.assertRaises(KeyError):
            self.apply([self.message('a','assistant'),{'id':'bad'}],upper='later')
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0],'2026-10-13T01:00:00Z')

    def test_source_failure_keeps_cursor_and_export_failure_retries(self):
        self.apply([self.message('u','user')])
        with patch.object(c,'query_source',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): c.sync(self.db,self.root,self.binding)
        self.assertEqual(self.db.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0],'failed')
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0],'2026-10-13T01:00:00Z')
        snapshot={'rows':[self.message('a','assistant')],'source':{'tenant_id':1},'upper':'2026-10-13T03:00:00Z'}
        with patch.object(c,'query_source',return_value=snapshot),patch.object(c,'export',side_effect=RuntimeError('export offline')):
            with self.assertRaises(RuntimeError): c.sync(self.db,self.root,self.binding)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0],2)
        with patch.object(c,'query_source',return_value=snapshot),patch.object(c,'export',return_value=1) as exporter:
            self.assertEqual(c.sync(self.db,self.root,self.binding)['inserted'],0)
            exporter.assert_called_once()

    def test_scope_change_rejected(self):
        self.apply([self.message('u','user')])
        with self.assertRaises(RuntimeError):
            c.apply_snapshot(self.db,{'rows':[],'source':{'tenant_id':2},'upper':'later'},self.binding)

    def test_operational_history_logs_keep_original_text_in_private_archive_only(self):
        from operational_logs import OperationalLogs
        secret='private-original-question-and-answer'
        rows=[self.message('u','user',content=secret),self.message('a','assistant',content=secret)]
        snapshot={'rows':rows,'source':{'tenant_id':1},'upper':'2026-10-16T01:00:00Z'}
        logs=OperationalLogs({'state_dir':str(self.root),'operational_logging':{'enabled':True}},component='qa_collector')
        logs.bootstrap()
        with patch.object(c,'query_source',return_value=snapshot):
            c.sync(self.db,self.root,{**self.binding,'export_backend':'openpyxl'},logs=logs)
        self.assertEqual(c.qa_records(self.db)[0]['question'],secret)
        for path in logs.root.rglob('*.jsonl'):
            self.assertNotIn(secret,path.read_text())
        audit=logs.query()['events']
        self.assertIn('history_archive_updated',[row['event'] for row in audit])
        self.assertTrue(all(row.get('run_id',1)==1 for row in audit))

    def test_history_archive_still_updates_when_log_directory_is_unavailable(self):
        from operational_logs import OperationalLogs
        (self.root/'logs').write_text('fixture blocking directory creation')
        logs=OperationalLogs({'state_dir':str(self.root),'operational_logging':{'enabled':True}},component='qa_collector')
        snapshot={'rows':[self.message('u','user'),self.message('a','assistant')],
                  'source':{'tenant_id':1},'upper':'2026-10-16T01:00:00Z'}
        with patch.object(c,'query_source',return_value=snapshot):
            summary=c.sync(self.db,self.root,{**self.binding,'export_backend':'openpyxl'},logs=logs)
        self.assertEqual(summary['messages'],2)
        self.assertEqual(json.loads((self.root/'health.json').read_text())['status'],'success')

    def test_real_excel_long_unicode_formula_like_text_and_tamper(self):
        rows=[]
        for index,question in enumerate(['=SUM(A1:A2)',"'=literal",'+123']):
            rows += [self.message(f'u{index}','user',f'r{index}',content=question),
                     self.message(f'a{index}','assistant',f'r{index}',content=('中文🙂\n'*8000 if index==0 else '完整回答'))]
        self.apply(rows)
        records=c.qa_records(self.db)
        path=self.root/'test.xlsx'
        run=subprocess.run([c.NODE,str(Path(c.__file__).with_name('export.mjs')),str(path)],
                           input=json.dumps(records),text=True,capture_output=True,timeout=180)
        self.assertEqual(run.returncode,0,run.stderr[-500:])
        c.restore_literal_quotes(path)
        checked=c.verify_excel(path,records)
        self.assertEqual(checked['records'],3)
        self.assertEqual(checked['excel_rows'],3)
        self.assertEqual(checked['detail_rows'],3)
        self.assertEqual(checked['main_columns'],7)
        self.assertGreater(checked['overflow_fields'],0)
        records[0]['answer']+='tamper'
        with self.assertRaises(RuntimeError): c.verify_excel(path,records)

    def test_portable_excel_original_text_overflow_and_empty_archive(self):
        for index, question in enumerate(['=SUM(A1:A2)', "'=literal", '+123', '普通问题']):
            self.apply([self.message(f'u{index}', 'user', f'r{index}', content=question),
                        self.message(f'a{index}', 'assistant', f'r{index}', content='中文🙂\n' * 9000)])
        c.export(self.db, self.root, backend='openpyxl')
        result = c.verify_excel(self.root/'机器人5问答.xlsx', c.qa_records(self.db))
        self.assertEqual(result['formulas'], 0)
        self.assertEqual(result['overflow_fields'], 4)
        from portable_excel import write_excel
        write_excel([], self.root/'empty.xlsx')
        self.assertEqual(c.verify_excel(self.root/'empty.xlsx', [])['records'], 0)

    def test_execution_steps_backfill_keeps_answers_and_deduplicates(self):
        user=self.message('u','user',content='原始问题')
        answer=self.message('a','assistant',content='原始回答')
        self.apply([user,answer])
        self.assertEqual(c.qa_records(self.db)[0]['reasoning_process'],'尚未补采执行步骤。')
        answer['agent_steps']=[{'iteration':0,'thought':'先查资料','timestamp':'2026-10-19T00:00:00Z',
            'tool_calls':[{'id':'tool-1','name':'read_document','args':{'id':'doc1'},
                           'result':{'success':True,'output':'资料正文','data':{'title':'文件'}}}]}]
        self.assertEqual(self.apply([user,answer])['updated'],1)
        self.assertEqual(self.apply([user,answer])['unchanged'],2)
        record=c.qa_records(self.db)[0]
        self.assertEqual(record['question'],'原始问题')
        self.assertEqual(record['answer'],'原始回答')
        self.assertEqual(record['agent_steps'],answer['agent_steps'])
        for value in ['先查资料','read_document','资料正文','调用参数']:
            self.assertIn(value,record['reasoning_process'])
        self.assertNotIn('模型返回的思考文本',record['reasoning_process'])
        self.assertEqual(c.reasoning_process({'agent_steps':[]}), '源记录未保存执行步骤。')
        self.assertIn('尚无执行步骤',c.reasoning_process(None))

    def test_reasoning_export_full_text_and_cell_limit(self):
        from portable_excel import write_excel
        rows=[]
        for i,thought in enumerate(['保留的步骤原文\n工具说明','长过程🙂\n'*9000]):
            answer=self.message(f'a{i}','assistant',f'r{i}',content='答案原文')
            answer['agent_steps']=[{'iteration':0,'thought':thought,'tool_calls':[]}]
            rows.extend([self.message(f'u{i}','user',f'r{i}'),answer])
        self.apply(rows)
        records=c.qa_records(self.db)
        path=self.root/'reasoning.xlsx'
        run=subprocess.run([c.NODE,str(Path(c.__file__).with_name('export.mjs')),str(path)],
                           input=json.dumps(records),text=True,capture_output=True,timeout=180)
        self.assertEqual(run.returncode,0,run.stderr[-500:])
        c.restore_literal_quotes(path)
        check=c.verify_excel(path,records)
        self.assertEqual(check['main_columns'],7)
        self.assertEqual(check['excel_rows'],2)
        self.assertEqual(check['overflow_fields'],1)
        records[0]['reasoning_process']+='tamper'
        with self.assertRaises(RuntimeError):c.verify_excel(path,records)
        records=c.qa_records(self.db)
        write_excel(records,self.root/'portable.xlsx')
        self.assertEqual(c.verify_excel(self.root/'portable.xlsx',records)['overflow_fields'],1)

    def test_migrated_archive_restarts_full_deduplicated_reconciliation(self):
        import migration
        self.apply([self.message('u','user'), self.message('a','assistant')])
        binding_file = self.root/'binding.json'
        binding_file.write_text(json.dumps(self.binding))
        # Snapshot destination deliberately outside the live archive tree.
        with tempfile.TemporaryDirectory() as temp:
            staged = Path(temp)/'snapshot'
            migration.snapshot(self.root, binding_file, staged)
            restored = Path(temp)/'restored'
            migration.restore(staged, restored, Path(temp)/'compose.json')
            copied = c.connect(restored)
            try:
                self.assertEqual(copied.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
                self.assertIsNone(copied.execute("SELECT value FROM state WHERE key='cursor'").fetchone())
                binding = json.loads((restored/'binding.json').read_text())
                snapshot = dict(rows=[self.message('u','user'),self.message('a','assistant')],
                                source={'tenant_id':1}, upper='2026-10-13T02:00:00Z')
                with patch.object(c, 'query_source', return_value=snapshot) as query:
                    summary = c.sync(copied, restored, binding)
                    self.assertTrue(query.call_args.args[3])
                self.assertEqual(summary['inserted'], 0)
                self.assertEqual(summary['qa_records'], 1)
                with self.assertRaises(ValueError): migration.restore(staged, restored, Path(temp)/'compose.json')
            finally:
                copied.close()

    def test_portable_compose_query_is_read_only_and_scope_bound(self):
        from types import SimpleNamespace
        binding = {'channel_id':'11111111-1111-4111-8111-111111111111',
                   'agent_id':'22222222-2222-4222-8222-222222222222',
                   'compose_file':'/isolated/compose.json', 'docker':'docker'}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(dict(channel_count=1, rows=[], source={}, upper='now')),stderr='')
        with patch.object(c.subprocess, 'run', return_value=result) as execute:
            c.query_source(binding, full=True)
            args = execute.call_args.args[0]
            sql = execute.call_args.kwargs['input']
        self.assertEqual(args[:8], ['docker','compose','-f','/isolated/compose.json','exec','-T','postgres','sh'])
        self.assertIn('REPEATABLE READ READ ONLY', sql)
        self.assertIn(binding['channel_id'], sql)
        self.assertNotIn('UPDATE ', sql)

    def test_failed_restore_leaves_no_partial_archive(self):
        import migration
        self.apply([self.message('u','user'),self.message('a','assistant')])
        binding_file=self.root/'binding.json'
        binding_file.write_text(json.dumps(self.binding))
        with tempfile.TemporaryDirectory() as temp:
            staged=Path(temp)/'snapshot'; target=Path(temp)/'restored'
            migration.snapshot(self.root,binding_file,staged)
            with patch.object(c,'export',side_effect=RuntimeError('export unavailable')):
                with self.assertRaises(RuntimeError): migration.restore(staged,target,Path(temp)/'compose.json')
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(temp).glob('restored.restoring-*')))
            migration.restore(staged,target,Path(temp)/'compose.json')
            self.assertTrue((target/'history.sqlite3').is_file())


if __name__=='__main__': unittest.main()
