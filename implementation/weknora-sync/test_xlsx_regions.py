import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from docflow.common import artifact, write_json, WorkflowError
from docflow.xlsx_regions import region_boxes, render_regions
from parse_quality import summarize_units
from xlsx_recovery import recover, checkpoint


class XlsxRegionTests(unittest.TestCase):
    def test_boxes_cover_each_row_and_column_without_gaps(self):
        width,height=5403,13687
        boxes=region_boxes(width,height)
        self.assertGreater(len(boxes),1)
        self.assertTrue(all(0<=x<r<=width and 0<=y<b<=height and r-x<=2000 and b-y<=1400 for x,y,r,b in boxes))
        for y in range(height):
            intervals=sorted((x,r) for x,top,r,bottom in boxes if top<=y<bottom)
            covered=0
            for left,right in intervals:
                self.assertLessEqual(left,covered)
                covered=max(covered,right)
            self.assertEqual(covered,width)
        with self.assertRaises(WorkflowError):region_boxes(100000,100000)
        with self.assertRaises(ValueError):region_boxes(1,1,overlap=2000)

    def fixture(self,root):
        from pypdf import PdfWriter
        prepared=root/'cache/prepared';prepared.mkdir(parents=True)
        pdf=prepared/'render-input.pdf'
        writer=PdfWriter();writer.add_blank_page(width=2400,height=4000);writer.write(pdf)
        native=prepared/'prepared.json'
        write_json(native,{'source_hash':'source-hash','native':{'sheets':[{'name':'密集表','range':'A1:R132'}]}})
        index=root/'cache/prepared-index.json'
        write_json(index,{'artifacts':[artifact(root,pdf),artifact(root,native)]})
        doc={'prepared_ref':artifact(root,index)}
        expected=[{'id':'u1','label':'工作表密集表！A1:R132','image':'old.png','text':''},
                  {'id':'u2','label':'原生单元格','image':None,'text':'原始公式与文字'}]
        results=[{**expected[0],'body':'','issues':['模型报告无可辨认文字，待人工确认']},
                 {**expected[1],'body':'原有已完成正文 =SUM(A1:A5)','issues':[]}]
        return SimpleNamespace(store=SimpleNamespace(root=root)),doc,expected,results,pdf

    def test_only_failed_visual_replaced_and_missing_region_still_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();workflow,doc,expected,results,_=self.fixture(root)
            prior=copy.deepcopy(results)
            regions=[{'image':'r.png','label':'分区','text':'','region':{'page':1,'box':[0,0,100,100]}}]
            with patch('xlsx_recovery.render_regions',return_value=regions), \
                    patch('xlsx_recovery.transcribe',return_value={'body':'清晰分区正文','issues':[]}):
                e,r,path=recover(workflow,doc,None,expected,results,'source-hash',root/'job',{'parser_workers':1},None,'prompt')
            self.assertEqual(results,prior)
            self.assertEqual(r[-1],results[-1])
            self.assertEqual(summarize_units(e,r,'source-hash')['status'],'complete')
            r[0]['body']=''
            self.assertEqual(summarize_units(e,r,'source-hash')['status'],'incomplete')
            self.assertEqual(json.loads(Path(path).read_text())['reused_units'],1)

    def test_changed_pdf_is_rejected_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();workflow,doc,expected,results,pdf=self.fixture(root)
            pdf.write_bytes(pdf.read_bytes()+b'changed')
            with patch('xlsx_recovery.transcribe') as model:
                with self.assertRaises(WorkflowError):recover(workflow,doc,None,expected,results,'source-hash',root/'job',{},None,'prompt')
                model.assert_not_called()

    def test_checkpoint_cannot_reference_another_root_or_source_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'original.xlsx';source.write_bytes(b'original')
            with self.assertRaises(WorkflowError):checkpoint({'state_dir':str(root/'state'),
                '_xlsx_checkpoint_work':str(root)},source,'hash')
            job=root/'state/jobs/old';job.mkdir(parents=True)
            write_json(job/'parse-report.json',{'source_hash':'old-version','status':'incomplete'})
            self.assertIsNone(checkpoint({'state_dir':str(root/'state'),
                '_xlsx_checkpoint_work':str(job)},source,'new-version'))

    def test_real_poppler_crop_matches_planned_dimensions(self):
        from docflow.config import configuration
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();_,_,_,_,pdf=self.fixture(root)
            units=render_regions(pdf,0,'密集表',root/'tiles',configuration())
            self.assertGreater(len(units),1)
            self.assertTrue(all((root/'tiles'/u['image']).is_file() for u in units))

    def test_new_dense_workbook_preparation_selects_regions_and_retains_native_formula(self):
        import openpyxl
        from docflow.documents import prepare
        from docflow.config import configuration
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'dense.xlsx'
            book=openpyxl.Workbook();book.active['A1']='=1+1';book.active['A2']='原始文字';book.save(source);book.close()
            original=source.read_bytes()
            page=SimpleNamespace(extract_text=lambda:'字'*17001,get=lambda *a:[])
            reader=SimpleNamespace(is_encrypted=False,pages=[page])
            region={'label':'密集表分区','text':'分区文字','image':'r.png','region':{'page':1,'box':[0,0,100,100]}}
            with patch('docflow.documents.run'),patch('pypdf.PdfReader',return_value=reader), \
                    patch('docflow.xlsx_regions.render_regions',return_value=[region]) as renderer:
                units=prepare(source,root/'prepared',configuration())
            renderer.assert_called_once()
            self.assertTrue(any(unit.get('region') for unit in units))
            self.assertTrue(any(unit.get('method')=='native_cells' and '=1+1' in unit['native_body'] for unit in units))
            self.assertEqual(source.read_bytes(),original)


if __name__=='__main__':unittest.main()
