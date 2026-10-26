import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docflow.common import WorkflowError,digest
from tests.fixtures import workflow,start,approve,candidate_files


class WorkflowTests(unittest.TestCase):
    def test_full_flow_replay_and_reexport(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,calls=workflow(root/'state');s=start(w,root,include_mp4=True)
            self.assertEqual(s['phase'],'awaiting_review',s)
            self.assertEqual(s['documents'][1]['state'],'skipped')
            self.assertEqual(len(calls),2)
            s=approve(w,s);args=candidate_files(s);s=w.execute('submit',args)
            self.assertEqual(s['phase'],'completed',s)
            self.assertEqual(w.execute('submit',args),s)
            original=Path(s['artifacts']['original']);saved=digest(original)
            Path(s['artifacts']['deliverable']).write_bytes(b'user edited copy')
            self.assertEqual(w.status(s['batch_id'])['phase'],'completed')
            s=w.execute('resume',{'request_id':'export2','batch_id':s['batch_id'],'expected_revision':s['revision'],'mode':'reexport'})
            self.assertEqual(s['phase'],'completed',s)
            self.assertEqual(digest(original),saved)
            self.assertEqual(len(calls),2)

    def test_export_failure_restores_saved_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,calls=workflow(root/'state');s=approve(w,start(w,root));args=candidate_files(s)
            with patch('docflow.runner.export_workbook',side_effect=OSError('disk unavailable')):
                s=w.execute('submit',args)
            self.assertEqual(s['errors'][0]['code'],'IO_ERROR')
            s=w.execute('resume',{'request_id':'resume1','batch_id':s['batch_id'],'expected_revision':s['revision']})
            self.assertEqual(s['phase'],'completed',s)
            self.assertEqual(len(calls),2)

    def test_revision_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();w,_=workflow(root/'state');s=start(w,root)
            with self.assertRaises(WorkflowError) as error:
                w.execute('resume',{'request_id':'stale','batch_id':s['batch_id'],'expected_revision':0})
            self.assertEqual(error.exception.code,'REVISION_CONFLICT')
