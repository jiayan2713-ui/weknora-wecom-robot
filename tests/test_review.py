import tempfile
import unittest
from pathlib import Path

from tests.fixtures import workflow,start,approve


class ReviewTests(unittest.TestCase):
    def test_confirmation_and_changed_working(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,_=workflow(root/'state');s=start(w,root)
            denied=w.execute('approve',{'request_id':'denied','batch_id':s['batch_id'],'expected_revision':s['revision'],'codex_model':'fixture'})
            self.assertEqual(denied['errors'][0]['code'],'REVIEW_CONFIRMATION_REQUIRED')
            s=approve(w,denied)
            self.assertEqual(s['phase'],'awaiting_codex')
            Path(s['documents'][0]['working']).write_text(Path(s['documents'][0]['working']).read_text().replace('10万元','12万元'))
            outcome=w.execute('resume',{'request_id':'regen','batch_id':s['batch_id'],'expected_revision':s['revision'],'mode':'regenerate','codex_model':'fixture'})
            self.assertEqual(outcome['errors'][0]['code'],'CONTENT_CHANGED')

    def test_explicit_review_reuse_keeps_human_correction(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,calls=workflow(root/'state');s=start(w,root)
            working=Path(s['documents'][0]['working']);working.write_text(working.read_text().replace('10万元','10万元（仅此方案）'))
            s=approve(w,s)
            first=w.store.batch(s['batch_id']);review_id=Path(first['review']['path']).parent.name
            second=start(w,root,request='start2')
            self.assertEqual(len(calls),2)
            s=w.execute('approve',{'request_id':'reuse','batch_id':second['batch_id'],'expected_revision':second['revision'],
                 'confirmed':True,'statement':'测试明确沿用旧审核版本','codex_model':'fixture',
                 'reuse_review':{'batch_id':first['id'],'review_id':review_id},'reuse_map':{'d0001':'d0001'}})
            self.assertEqual(s['phase'],'awaiting_codex',s)
            body=Path(s['generation']['documents'][0]).read_text()
            self.assertIn('仅此方案',body)
            self.assertNotIn('仅此方案',Path(s['documents'][0]['working']).read_text())
