import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector
import crm_sync as crm


def schema():
    fields=[{'controlName':name,'controlId':key,'type':16 if key=='time' else 2}
            for key,name in crm.FIELD_NAMES.items()]
    fields.extend([{'controlName':name,'controlId':'human'+str(i),'type':2,'required':False}
                   for i,name in enumerate(crm.HUMAN_FIELDS)])
    return {'name':'上线后真实问答','controls':fields}


def record(identifier='a',answer='原始答案'):
    return {'status':'已完成','record_id':identifier,'assistant_message_id':identifier,
            'user_message_id':'u'+identifier,'channel_id':'channel','question_at':'2026-10-19T00:00:00Z',
            'user_id':'wecom-user','question':'=原始问题\n第二行','answer':answer,'reasoning_process':'执行记录🙂\n'*10000}


class FakeClient:
    def __init__(self):
        self.rows={}
        self.creates=0
        self.updates=0
        self.timeout_after_create=False
        self.truncate=False
        self.fail=False

    def list_access(self):
        if self.fail:raise crm.SyncError('api_10005')
        return schema()

    def find(self,field,key):
        matches=[(row_id,copy.deepcopy(row)) for row_id,row in self.rows.items() if row[field]==key]
        if len(matches)>1:raise crm.SyncError('duplicate_remote_source_id')
        return matches[0] if matches else None

    def create(self,values):
        self.creates+=1
        row_id='remote'+str(self.creates)
        self.rows[row_id]=copy.deepcopy(values)
        if self.truncate:self.rows[row_id]['reasoning']=values['reasoning'][:100]
        if self.timeout_after_create:
            self.timeout_after_create=False
            raise crm.SyncError('transport_outcome_unknown',True)
        return row_id

    def get(self,row_id):
        return copy.deepcopy(self.rows[row_id])

    def update(self,row_id,values):
        self.updates+=1
        self.rows[row_id].update(copy.deepcopy(values))
        if self.truncate:self.rows[row_id]['reasoning']=values['reasoning'][:100]


class CRMSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=collector.connect(self.root)
        self.client=FakeClient()
        self.config={'base_url':'https://api.mingdao.com','worksheet_id':'target'}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_sync(self,records):
        return crm.run(self.db,records,self.config,self.client,limit=100,budget=30)

    def test_full_text_identity_repeat_and_restart(self):
        qa=record()
        result=self.run_sync([qa])
        self.assertEqual(result['verified'],1)
        self.assertEqual(self.client.rows['remote1']['reasoning'],qa['reasoning_process'])
        self.assertEqual(self.client.rows['remote1']['time'],'2026-10-19 08:00:00')
        self.assertEqual(self.client.rows['remote1']['user'],'wecom-user')
        self.assertEqual(self.run_sync([qa])['attempted'],0)
        self.db.close();self.db=collector.connect(self.root)
        self.assertEqual(self.run_sync([qa])['created'],0)
        self.assertEqual(self.client.creates,1)

    def test_timeout_after_success_recovers_without_duplicate(self):
        self.client.timeout_after_create=True
        first=self.run_sync([record()])
        self.assertEqual(first['counts']['retry'],1)
        self.db.execute('UPDATE crm_records SET next_retry=0');self.db.commit()
        second=self.run_sync([record()])
        self.assertEqual(second['recovered'],1)
        self.assertEqual(second['verified'],1)
        self.assertEqual(self.client.creates,1)

    def test_answer_targets_raw_column_and_preserves_web_column(self):
        live_schema=schema()
        live_schema['controls'].extend([
            {'controlName':'答案（网页格式）','controlId':'web_answer','type':2},
            {'controlName':'答案','controlId':'legacy_answer','type':2},
        ])
        self.assertEqual(crm.FIELD_NAMES['answer'],'答案（原始格式）')
        fields=crm.field_map(live_schema)
        self.assertEqual(fields['answer'],'answer')
        with patch.object(self.client,'list_access',return_value=live_schema):
            qa=record(answer='**完整原文**\n第二行')
            self.run_sync([qa])
            remote=self.client.rows['remote1']
            self.assertEqual(remote['answer'],qa['answer'])
            self.assertNotIn('web_answer',remote)
            self.assertNotIn('legacy_answer',remote)
            remote['web_answer']='<p>网页展示</p>'
            qa['answer']='更新后的完整原文'
            self.run_sync([qa])
            self.assertEqual(remote['answer'],qa['answer'])
            self.assertEqual(remote['web_answer'],'<p>网页展示</p>')

    def test_uploads_follow_question_time_across_batches_and_timezones(self):
        oldest=record('z-oldest');oldest['question_at']='2026-10-19T08:01:00+08:00'
        middle=record('a-middle');middle['question_at']='2026-10-19T00:02:00Z'
        newest=record('b-newest');newest['question_at']='2026-10-19T08:03:00+08:00'
        shuffled=[newest,oldest,middle]
        first=crm.run(self.db,shuffled,self.config,self.client,limit=2,budget=30)
        self.assertEqual(first['created'],2)
        self.assertEqual([r['source'].split(':')[-1] for r in self.client.rows.values()],['z-oldest','a-middle'])
        self.db.close();self.db=collector.connect(self.root)
        second=self.run_sync(shuffled)
        self.assertEqual(second['created'],1)
        self.assertEqual([r['source'].split(':')[-1] for r in self.client.rows.values()],['z-oldest','a-middle','b-newest'])
        self.assertEqual(self.run_sync(shuffled)['attempted'],0)

    def test_due_retries_follow_fresh_uploads_and_recover_without_duplicates(self):
        old=record('old-retry');old['question_at']='2026-10-18T00:00:00Z'
        self.client.timeout_after_create=True
        self.assertEqual(self.run_sync([old])['counts']['retry'],1)
        self.db.execute('UPDATE crm_records SET next_retry=0');self.db.commit()
        fresh=record('fresh');fresh['question_at']='2026-10-19T00:00:00Z'
        first=crm.run(self.db,[old,fresh],self.config,self.client,limit=1,budget=30)
        self.assertEqual(first['created'],1)
        self.assertEqual(first['recovered'],0)
        second=self.run_sync([fresh,old])
        self.assertEqual(second['recovered'],1)
        self.assertEqual(second['created'],0)
        self.assertEqual(self.client.creates,2)
        self.assertEqual(second['status'],'success')

    def test_retry_wait_does_not_block_new_questions(self):
        old=record('old');old['question_at']='2026-10-18T00:00:00Z'
        self.client.timeout_after_create=True
        self.run_sync([old])
        fresh=record('new')
        result=self.run_sync([old,fresh])
        self.assertEqual(result['created'],1)
        self.assertEqual(result['recovered'],0)
        self.assertEqual(result['counts'],{'retry':1,'synced':1})

    def test_update_keeps_human_notes_and_blocks_reviewed_answer(self):
        qa=record()
        self.run_sync([qa])
        self.client.rows['remote1']['human1']='人工备注'
        qa['answer']='新答案'
        self.assertEqual(self.run_sync([qa])['updated'],1)
        self.assertEqual(self.client.rows['remote1']['human1'],'人工备注')
        self.client.rows['remote1']['human0']='评分选项ID'
        qa['answer']='评分后变化的答案'
        self.assertEqual(self.run_sync([qa])['counts']['blocked'],1)
        self.assertEqual(self.client.rows['remote1']['answer'],'新答案')

    def test_truncated_readback_is_never_marked_synced(self):
        self.client.truncate=True
        result=self.run_sync([record()])
        self.assertEqual(result['counts']['blocked'],1)
        row=self.db.execute('SELECT * FROM crm_records').fetchone()
        self.assertEqual(row['error_code'],'remote_content_mismatch')
        self.assertIsNone(row['synced_hash'])

    def test_crm_trailing_answer_space_is_audited_without_changing_archive(self):
        qa=record(answer='正文\n保留换行 ')
        self.run_sync([qa])
        self.client.rows['remote1']['answer']=qa['answer'][:-1]
        self.db.execute("UPDATE crm_records SET status='blocked',next_retry=0")
        self.db.commit()
        result=self.run_sync([qa])
        self.assertEqual(result['verified'],1)
        self.assertEqual(result['updated'],0)
        note=json.loads(self.db.execute('SELECT readback_note FROM crm_records').fetchone()[0])
        self.assertEqual(note[0]['removed_characters'],1)
        self.assertTrue(qa['answer'].endswith(' '))
        fields=crm.field_map(schema())
        values=crm.payload(qa,fields)
        for changed in ['正文\n保留换行\n','正文\n保留换','正文 保留换行']:
            with self.assertRaises(crm.SyncError):
                crm.verify(values,{**values,'answer':changed},fields)
        with self.assertRaises(crm.SyncError):
            crm.verify({**values,'question':'问题 '},{**values,'question':'问题'},fields)

    def test_incomplete_and_deleted_records_do_not_publish_or_delete_remote(self):
        qa=record()
        self.run_sync([qa])
        qa['status']='源记录已删除'
        self.assertEqual(self.run_sync([qa])['created'],0)
        self.assertEqual(len(self.client.rows),1)
        qa['status']='未完成'
        self.assertEqual(self.run_sync([qa])['eligible'],0)

    def test_schema_requires_datetime_unreviewed_score_and_source(self):
        changed=schema()
        changed['controls'][0]['type']=46
        with self.assertRaisesRegex(crm.SyncError,'datetime'):crm.field_map(changed)
        changed=schema();changed['controls'][-4]['required']=True
        self.assertEqual(crm.field_map(changed)['create_defaults'],{})

    def test_required_score_with_explicit_unreviewed_option_is_supported(self):
        updated=schema();score=updated['controls'][-4]
        score.update({'required':True,'options':[{'key':'unreviewed','value':'未评分'},{'key':'zero','value':'0'}]})
        fields=crm.field_map(updated)
        self.assertEqual(fields['create_defaults'],{'human0':'未评分'})
        self.assertFalse(crm.is_reviewed('["unreviewed"]',fields))
        self.assertFalse(crm.is_reviewed('未评分',fields))
        self.assertTrue(crm.is_reviewed('["zero"]',fields))
        self.assertTrue(crm.is_reviewed(0,fields))
        with patch.object(self.client,'list_access',return_value=updated):
            self.run_sync([record()])
            self.assertEqual(self.client.rows['remote1']['human0'],'未评分')
            self.assertEqual(self.run_sync([record(answer='新回答')])['updated'],1)
            self.client.rows['remote1']['human0']='["zero"]'
            self.assertEqual(self.run_sync([record(answer='评分后更改')])['counts']['blocked'],1)

    def test_crm_failure_does_not_fail_collection(self):
        with patch.object(collector,'sync',return_value={'messages':2}),patch.object(crm,'tick',side_effect=OSError('CRM offline')):
            result=collector.run_cycle(self.db,self.root,{})
        self.assertEqual(result['messages'],2)
        self.assertEqual(result['crm_sync']['status'],'failed')

    def test_export_failure_does_not_stop_crm_from_reading_archive(self):
        with patch.object(collector,'sync',side_effect=RuntimeError('export failed')),patch.object(crm,'tick',return_value={'status':'success'}) as tick:
            with self.assertRaisesRegex(RuntimeError,'export failed'):
                collector.run_cycle(self.db,self.root,{})
            tick.assert_called_once()

    def test_error_health_never_contains_credentials_or_original_text(self):
        private=self.root/'private';private.mkdir()
        config={**self.config,'app_key':'secret-key','app_sign':'secret-sign','enabled':True}
        collector.atomic_json(private/'crm-config.json',config)
        with patch.object(crm,'Client',return_value=self.client):
            self.client.fail=True
            result=crm.tick(self.db,[record()],self.root)
        self.assertEqual(result['status'],'blocked')
        health=(self.root/'crm-health.json').read_text()
        for secret in ['secret-key','secret-sign','原始问题','原始答案']:
            self.assertNotIn(secret,health)

    def test_schema_preparation_preserves_score_options_and_adds_unique_source(self):
        old=schema();old['controls'][0]['type']=46
        score=old['controls'][-4];score['required']=True;score['options']=[{'key':'existing','value':'原评分选项'}]
        old['controls']=[f for f in old['controls'] if f['controlName']!='来源记录ID']
        client=crm.Client({**self.config,'app_key':'fake','app_sign':'fake'})
        with patch.object(client,'request',side_effect=[old,{},schema()]) as request:
            client.prepare_schema()
        body=request.call_args_list[1].args[1]
        self.assertEqual(body['editFields'][0]['type'],16)
        self.assertEqual(len(body['editFields']),1)
        self.assertNotIn(score['controlId'],[f['id'] for f in body['editFields']])
        self.assertTrue(body['addFields'][0]['isUnique'])
        self.assertNotIn('removeFields',body)

    def test_api_field_aliases_and_v2_source_lookup(self):
        client=crm.Client({**self.config,'app_key':'fake','app_sign':'fake','view_id':'view'})
        fields=schema()
        for field in fields['controls']:
            if field['controlName']=='来源记录ID':field['alias']='robot5_source_id'
            if field['controlName']=='问题':field['alias']='question_alias'
        row={'rowid':'the-row','robot5_source_id':'the-source','question_alias':'原始问题'}
        with patch.object(client,'request',side_effect=[fields,{'rows':[row]},row]) as request:
            client.schema()
            found=client.find('source','the-source')
            self.assertEqual(found[0],'the-row')
            self.assertEqual(found[1]['source'],'the-source')
            self.assertEqual(client.get('the-row')['question'],'原始问题')
        payload=request.call_args_list[1].args[1]
        self.assertEqual(payload['viewId'],'view')
        self.assertEqual(payload['filters'][0]['filterType'],2)

    def test_migration_snapshot_carries_crm_mapping_and_private_config(self):
        import migration
        binding={'channel_id':'channel','agent_id':'agent'}
        collector.apply_snapshot(self.db,{'source':{'tenant_id':1},'upper':'2026-10-19T00:00:00Z','rows':[]},binding)
        self.run_sync([record()])
        private=self.root/'private';private.mkdir()
        collector.atomic_json(private/'crm-config.json',{**self.config,'enabled':False,'app_key':'fake-key','app_sign':'fake-sign'})
        binding_path=self.root/'binding.json';binding_path.write_text(json.dumps(binding))
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'snapshot'
            migration.snapshot(self.root,binding_path,target)
            self.assertEqual(json.loads((target/'private/crm-config.json').read_text())['app_key'],'fake-key')
            self.assertEqual((target/'private/crm-config.json').stat().st_mode&0o777,0o600)
            import sqlite3
            with sqlite3.connect(target/'history.sqlite3') as copied:
                self.assertEqual(copied.execute('SELECT row_id FROM crm_records').fetchone()[0],'remote1')


if __name__=='__main__':unittest.main()
