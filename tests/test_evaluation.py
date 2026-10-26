import copy
import json
import tempfile
import unittest
from pathlib import Path

from docflow.evaluation import validate,excel_error,fact_quotes,LEGACY_XLSX_FIELDS,XLSX_FIELDS
from docflow.export import export_workbook,verify_workbook
from docflow.generation import load_generation
from docflow.common import digest
from tests.fixtures import workflow,start,approve,candidate_files,SMALL


class EvaluationTests(unittest.TestCase):
    def test_main_quotes_mapping_capacity_and_legacy_readback(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,_=workflow(root/'state');s=approve(w,start(w,root));args=candidate_files(s)
            candidate=json.loads(Path(args['candidate_file']).read_text())
            facts=json.loads(Path(args['facts_file']).read_text())
            gen,table=load_generation(w.store,w.store.batch(s['batch_id'])['generation'])
            key=candidate[0]['evidence'][0]
            candidate[0]['required_facts'].append('办理周期为3个月。')
            facts.append({'question_id':'QA-001','fact_index':2,'fact':candidate[0]['required_facts'][1],
                          'sources':[{'evidence':key,'quote':table[key]['body']}]})
            facts[0]['sources'][0]['quote']=table[key]['body']
            self.assertEqual(validate(candidate,facts,gen,table),[])
            text=fact_quotes(facts)['QA-001']
            self.assertIn('事实1、2',text)
            self.assertEqual(text.count(table[key]['body']),1)
            path=root/'new.xlsx';export_workbook(path,candidate,facts,gen,table)
            book=openpyxl.load_workbook(path)
            self.assertEqual([c.value for c in book['评测集'][1]],XLSX_FIELDS)
            self.assertEqual(book['评测集']['G2'].value,text)
            book['评测集']['G2']='篡改摘录';bad=root/'tampered.xlsx';book.save(bad)
            from docflow.common import WorkflowError
            with self.assertRaises(WorkflowError):verify_workbook(bad,candidate,facts,gen,table)
            book['评测集'].delete_cols(7);legacy=root/'legacy.xlsx';book.save(legacy);book.close()
            with self.assertRaises(WorkflowError):verify_workbook(legacy,candidate,facts,gen,table)
            self.assertEqual(verify_workbook(legacy,candidate,facts,gen,table,columns=LEGACY_XLSX_FIELDS)['columns'],LEGACY_XLSX_FIELDS)
            table[key]['body']='甲'*17000+'乙'*17000
            facts[0]['sources'][0]['quote']='甲'*17000;facts[1]['sources'][0]['quote']='乙'*17000
            self.assertTrue(any(e['code']=='EXCEL_CAPACITY' and e['field']=='审核正文摘录'
                                for e in validate(candidate,facts,gen,table)))
            table[key]['body']='丙'*17000+'乙'*17000
            errors=validate(candidate,facts,gen,table)
            self.assertIn('QUOTE_NOT_FOUND',{e['code'] for e in errors})
            self.assertTrue(any(e['code']=='EXCEL_CAPACITY' and e['field']=='审核正文摘录' for e in errors))

    def test_ambiguous_facts_require_complete_source_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,_=workflow(root/'state');s=approve(w,start(w,root));args=candidate_files(s)
            candidate=json.loads(Path(args['candidate_file']).read_text())
            facts=json.loads(Path(args['facts_file']).read_text())
            gen,table=load_generation(w.store,w.store.batch(s['batch_id'])['generation'])
            gen['config'].update(single_step_questions=0,in_kb_questions=0,ambiguous_questions=1)
            item=candidate[0];item.update(category='模糊地带',推理步数='不适用',question='客户预算是否足够？',
                                      answer='项目A费用为10万元。请确认客户预算，目前无法确定是否足够。')
            self.assertEqual(gen['candidate_schema'],'candidate_v3')
            self.assertEqual(validate(candidate,facts,gen,table),[])
            changed=copy.deepcopy(candidate);changed[0]['required_facts']=[]
            self.assertIn('AMBIGUOUS_FACTS',{e['code'] for e in validate(changed,[],gen,table)})
            self.assertIn('FACT_MISSING',{e['code'] for e in validate(candidate,[],gen,table)})
            changed=copy.deepcopy(facts);changed[0]['sources'][0]['quote']='不存在的模糊题依据'
            self.assertIn('QUOTE_NOT_FOUND',{e['code'] for e in validate(candidate,changed,gen,table)})
            changed=copy.deepcopy(facts);changed[0]['fact']='错误对应的事实'
            self.assertIn('FACT_MISMATCH',{e['code'] for e in validate(candidate,changed,gen,table)})
            changed=copy.deepcopy(candidate);key=item['evidence'][0]
            extra=key+'-unused';table[extra]=copy.deepcopy(table[key]);changed[0]['evidence'].append(extra)
            self.assertIn('FACT_COVERAGE',{e['code'] for e in validate(changed,facts,gen,table)})

            legacy=copy.deepcopy(gen);legacy['candidate_schema']='candidate_v2'
            empty=copy.deepcopy(candidate);empty[0]['required_facts']=[]
            self.assertEqual(validate(empty,[],legacy,table),[])
            self.assertIn('AMBIGUOUS_FACTS',{e['code'] for e in validate(candidate,facts,legacy,table)})
            unknown=copy.deepcopy(gen);unknown['candidate_schema']='candidate_future'
            self.assertEqual(validate(candidate,facts,unknown,table)[0]['code'],'CANDIDATE_VERSION')

            outside=copy.deepcopy(gen);outside['config'].update(ambiguous_questions=0,out_of_kb_questions=1)
            outside['required_documents']=[]
            changed=copy.deepcopy(candidate);changed[0].update(category='知识库外',evidence=[])
            self.assertIn('BOUNDARY_REQUIREMENTS',{e['code'] for e in validate(changed,facts,outside,table)})
            changed[0]['required_facts']=[]
            self.assertEqual(validate(changed,[],outside,table),[])

    def test_generation_uses_frozen_template_rule_version(self):
        variables=['total_questions','in_kb_questions','single_step_questions','multi_step_questions',
                   'out_of_kb_questions','ambiguous_questions','min_analytical_multistep','id_prefix',
                   'review_root','required_documents','document_count','evidence_table_path','excluded_scope']
        body='## 模型提示词\n'+'\n'.join('{{'+name+'}}' for name in variables)
        for declaration,expected in [('', 'candidate_v2'),('候选规则：candidate_v3\n','candidate_v3'),
                                     ('候选规则：unknown\n',None),('候选规则：candidate_v3\n候选规则：candidate_v3\n',None)]:
            with self.subTest(declaration=declaration),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();w,_=workflow(root/'state')
                source=root/'source.csv';source.write_text('source\n')
                prompt=root/'prompt.md';prompt.write_text(declaration+body)
                s=w.execute('start',{'request_id':'schema-start','project':'版本测试','sources':[str(source)],
                                    'config':{**SMALL,'single_step_questions':0,'ambiguous_questions':1,
                                              'evaluation_prompt':str(prompt)}})
                # Editing the public template after start must not alter its frozen copy.
                prompt.write_text('候选规则：unknown\n'+body)
                s=approve(w,s)
                if expected is None:
                    self.assertIn('PROMPT_INVALID',{e['code'] for e in s['errors']})
                else:
                    gen,_=load_generation(w.store,w.store.batch(s['batch_id'])['generation'])
                    self.assertEqual(gen['candidate_schema'],expected)
                    if expected == 'candidate_v2':
                        args=candidate_files(s)
                        candidate=json.loads(Path(args['candidate_file']).read_text())
                        candidate[0].update(category='模糊地带',推理步数='不适用',required_facts=[])
                        Path(args['candidate_file']).write_text(json.dumps(candidate,ensure_ascii=False))
                        Path(args['facts_file']).write_text('[]')
                        s=w.execute('submit',args)
                        self.assertEqual(s['phase'],'completed',s)
                        original=Path(s['artifacts']['original']);original_hash=digest(original)
                        s=w.execute('resume',{'request_id':'legacy-reexport','batch_id':s['batch_id'],
                                            'expected_revision':s['revision'],'mode':'reexport'})
                        self.assertEqual(s['phase'],'completed',s)
                        prompt.write_text('候选规则：candidate_v3\n'+body)
                        s=w.execute('resume',{'request_id':'adopt-v3','batch_id':s['batch_id'],
                                            'expected_revision':s['revision'],'mode':'regenerate',
                                            'config':{'evaluation_prompt':str(prompt)},'codex_model':'fixture-codex'})
                        new_gen,table=load_generation(w.store,w.store.batch(s['batch_id'])['generation'])
                        self.assertEqual(new_gen['candidate_schema'],'candidate_v3')
                        self.assertIn('AMBIGUOUS_FACTS',{e['code'] for e in validate(candidate,[],new_gen,table)})
                        self.assertEqual(digest(original),original_hash)

    def test_pair_and_quote_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,_=workflow(root/'state');s=approve(w,start(w,root));args=candidate_files(s)
            c=json.loads(Path(args['candidate_file']).read_text());f=json.loads(Path(args['facts_file']).read_text())
            from docflow.generation import load_generation
            gen,table=load_generation(w.store,w.store.batch(s['batch_id'])['generation'])
            self.assertEqual(validate(c,f,gen,table),[])
            changed=copy.deepcopy(f);changed[0]['sources'][0]['quote']='来源：source.csv'
            self.assertIn('QUOTE_NOT_FOUND',[x['code'] for x in validate(c,changed,gen,table)])
            changed=copy.deepcopy(c);changed[0]['original_evidence']='不允许模型生成'
            self.assertIn('CANDIDATE_SCHEMA',[x['code'] for x in validate(changed,f,gen,table)])
            self.assertIn('FACT_MISSING',[x['code'] for x in validate(c,[],gen,table)])
            self.assertIsNone(excel_error('字'*32767))
            self.assertIsNotNone(excel_error('字'*32768))
            self.assertIsNotNone(excel_error('a\x00b'))

    def test_export_roundtrip_and_two_repair_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,calls=workflow(root/'state');s=approve(w,start(w,root))
            for _ in range(3):
                s=w.execute('submit',candidate_files(s,bad=True))
            self.assertEqual(s['outcome'],'needs_user')
            self.assertNotIn('submission',s)
            self.assertEqual(len(calls),2)
