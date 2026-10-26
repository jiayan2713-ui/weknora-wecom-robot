import unittest

from reconcile import reconcile


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{'relative': 'A/x.pdf', 'status': 'readable', 'sha256': 'source'}]
        self.markdowns = [{'relative': 'A/x.pdf.md', 'status': 'readable', 'sha256': 'parsed',
                          'md5': 'remote', 'size': 10, 'path': '/tmp/x.md'}]
        self.state = {'records': {'rid': {'paths': ['A/x.pdf'], 'hash': 'source'}}}
        self.marker = {'files': {'项目to咨询/Markdown/A/x.pdf.md': {'record_id': 'rid', 'sha256': 'parsed'}}}
        self.doc = {'id': 'existing', 'file_hash': 'remote', 'file_type': 'md', 'file_size': 10,
                    'folder_path': '项目to咨询/A', 'parse_status': 'completed'}

    def run_mapping(self, docs=None, recovered=None):
        return reconcile(self.sources, self.markdowns, docs or [], self.state, self.marker, recovered)[0]

    def test_existing_hash_links_remote_id(self):
        row = self.run_mapping([self.doc])[0]
        self.assertEqual(row['weknora_ids'], ['existing'])
        self.assertEqual(row['action'], 'mapped')
        self.assertEqual(row['folder_status'], 'matched')

    def test_missing_requires_current_source_and_artifact(self):
        self.assertEqual(self.run_mapping()[0]['action'], 'upload_missing')
        self.sources[0]['sha256'] = 'changed'
        self.assertEqual(self.run_mapping()[0]['action'], 'provenance_hold')

    def test_missing_source_does_not_trigger_delete(self):
        self.sources = []
        row = self.run_mapping([self.doc])[0]
        self.assertEqual(row['source_status'], 'source_missing_hold')
        self.assertEqual(row['weknora_ids'], ['existing'])

    def test_remote_size_mismatch_is_not_same_content(self):
        self.doc['file_size'] = 9
        self.assertEqual(self.run_mapping([self.doc])[0]['action'], 'upload_missing')

    def test_duplicate_content_retains_both_source_aliases(self):
        self.sources.append({'relative': 'B/y.pdf', 'status': 'readable', 'sha256': 'source'})
        self.markdowns.append(dict(self.markdowns[0], relative='B/y.pdf.md'))
        self.state['records']['rid']['paths'].append('B/y.pdf')
        self.marker['files']['项目to咨询/Markdown/B/y.pdf.md'] = self.marker['files']['项目to咨询/Markdown/A/x.pdf.md']
        rows = self.run_mapping([self.doc])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['weknora_ids'], ['existing'])
        self.assertEqual(rows[1]['folder_status'], 'shared_content_alias')

    def test_recovery_requires_matching_source_and_markdown_hashes(self):
        self.sources[0]['sha256'] = 'new'
        recovered = [{'source_relative': 'A/x.pdf', 'status': 'recovered', 'source_sha256': 'new',
                      'markdown_sha256': 'parsed', 'record_id': 'new-rid'}]
        self.assertEqual(self.run_mapping(recovered=recovered)[0]['action'], 'upload_missing')
        recovered[0]['markdown_sha256'] = 'different'
        self.assertEqual(self.run_mapping(recovered=recovered)[0]['action'], 'provenance_hold')

    def test_empty_transcription_is_not_uploaded_as_knowledge(self):
        recovered = [{'source_relative': 'A/x.pdf', 'status': 'recovered', 'source_sha256': 'source',
                      'markdown_sha256': 'parsed', 'record_id': 'rid', 'content_status': 'no_text_hold'}]
        self.assertEqual(self.run_mapping(recovered=recovered)[0]['action'], 'transcription_hold')


if __name__ == '__main__':
    unittest.main()
