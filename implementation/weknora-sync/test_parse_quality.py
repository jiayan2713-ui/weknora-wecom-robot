import json
import unittest

from parse_quality import summarize_units


class ParseQualityTests(unittest.TestCase):
    def expected(self, *identities):
        return [{'id': identity, 'label': '页 ' + identity} for identity in identities]

    def result(self, identity, body='项目费用为十万元。', **extra):
        return {'id': identity, 'label': '页 ' + identity, 'body': body, **extra}

    def summary(self, expected, results):
        return summarize_units(expected, results, 'source-sha')

    def test_complete_without_disclosing_body_or_claiming_source_review(self):
        report = self.summary(self.expected('u1', 'u2'), [self.result('u1'), self.result('u2')])
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['completed_units'], 2)
        self.assertEqual(report['text_units'], 2)
        self.assertFalse(report['human_review'])
        self.assertEqual(report['coverage_scope'], 'prepared_units')
        self.assertNotIn('十万元', json.dumps(report, ensure_ascii=False))

    def test_missing_duplicate_and_unexpected_ids_are_precise(self):
        report = self.summary(self.expected('u1', 'u2'),
                              [self.result('u1'), self.result('u1'), self.result('u3')])
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing_ids'], ['u2'])
        self.assertEqual(report['duplicate_ids'], ['u1'])
        self.assertEqual(report['unexpected_ids'], ['u3'])
        self.assertEqual(report['completed_units'], 0)

    def test_notes_and_title_are_not_a_body(self):
        for body in ('', '# 正文\n', '#正文\n', '# 解析记录（非原文）\n费用见原件。',
                     '#解析记录（非原文）\n费用见原件。',
                     '# 正文\n\n# 核对记录（非原文）\n费用待确认。'):
            with self.subTest(body=body):
                report = self.summary(self.expected('u1'), [self.result('u1', body)])
                self.assertEqual(report['status'], 'incomplete')
                self.assertEqual(report['missing_body_units'], 1)
                self.assertEqual(report['text_units'], 0)

    def test_normal_body_heading_and_notes_are_stripped(self):
        report = self.summary(self.expected('u1'), [self.result(
            'u1', '# 正文\n实际正文\n# 解析记录（非原文）\n说明')])
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['text_units'], 1)

    def test_blank_page_is_not_missing_when_other_page_has_text(self):
        report = self.summary(self.expected('u1', 'u2'), [self.result('u1'), self.result(
            'u2', '', notes='本单元无可提取正文且渲染图像全白，记录为空白页/空工作表；原件和单元定位保留，待人工审核。',
            issues=['已检测空白单元，待人工确认'])])
        self.assertEqual(report['status'], 'complete_with_warnings')
        self.assertEqual(report['blank_units'], 1)
        self.assertEqual(report['completed_units'], 2)
        self.assertEqual(report['missing_body_units'], 0)

    def test_unproven_blank_word_is_not_sufficient(self):
        report = self.summary(self.expected('u1'), [self.result('u1', '',
            issues=['空内容'], notes='空白页，待核对', content_status='needs_manual_confirmation')])
        self.assertEqual(report['status'], 'incomplete')

    def test_explicit_all_no_text_holds_but_partial_no_text_is_incomplete(self):
        no_text = self.result('u1', '# 正文\n[[DOCFLOW_NO_TEXT]]')
        report = self.summary(self.expected('u1'), [no_text])
        self.assertEqual(report['status'], 'no_readable_text')
        self.assertEqual(report['no_text_units'], 1)
        self.assertEqual(report['text_units'], 0)
        report = self.summary(self.expected('u1', 'u2'), [no_text, self.result('u2')])
        self.assertEqual(report['status'], 'incomplete')
        report = self.summary(self.expected('u1', 'u2'), [no_text])
        self.assertEqual(report['status'], 'incomplete')

    def test_no_text_after_image_repair_is_recognized(self):
        for extra in ({'notes': '模型报告无可辨认文字；保留原件。'},
                      {'issues': ['模型报告无可辨认文字，待人工确认']}):
            with self.subTest(extra=extra):
                report = self.summary(self.expected('u1'), [self.result('u1', '', **extra)])
                self.assertEqual(report['status'], 'no_readable_text')

    def test_all_confirmed_blank_and_zero_prepared_units(self):
        report = self.summary(self.expected('u1', 'u2'), [self.result('u1', '', empty_render=True),
            self.result('u2', '', content_status='confirmed_blank')])
        self.assertEqual(report['status'], 'no_readable_text')
        self.assertEqual(report['blank_units'], 2)
        self.assertEqual(self.summary([], [])['status'], 'incomplete')

    def test_processing_notes_do_not_count_as_source_text(self):
        expected = self.expected('u1') + [{'id': 'u2', 'label': '转换处理记录（非原文）'}]
        report = self.summary(expected, [self.result('u1'), self.result('u2', '', notes='格式已转换')])
        self.assertEqual(report['status'], 'complete_with_warnings')
        self.assertEqual(report['expected_units'], 2)
        self.assertEqual(report['completed_units'], 2)
        self.assertEqual(report['text_units'], 1)
        self.assertEqual(report['processing_record_units'], 1)
        self.assertEqual(report['missing_body_units'], 0)

    def test_processing_records_alone_cannot_make_a_readable_document(self):
        report = self.summary([{'id': 'u1', 'label': '转换处理记录（非原文）'}],
                              [self.result('u1', '', notes='转换说明')])
        self.assertEqual(report['status'], 'no_readable_text')
        self.assertEqual(report['text_units'], 0)

    def test_issues_preserved_and_bounded_without_blocking_body(self):
        report = self.summary(self.expected('u1'), [self.result('u1', issues=['无法辨认的印章'] +
            [{'code': 'uncertain', 'message': '待核对', 'body': '正文不应进入报告'}] + ['x' * 1000] * 30)])
        self.assertEqual(report['status'], 'complete_with_warnings')
        self.assertEqual(report['issue_units'], 1)
        self.assertLessEqual(len(report['units'][0]['issues']), 21)
        self.assertLessEqual(max(map(len, report['units'][0]['issues'])), 512)
        self.assertNotIn('正文不应进入报告', json.dumps(report, ensure_ascii=False))
        self.assertIn('无法辨认的印章', report['units'][0]['issues'])

    def test_duplicate_prepared_ids_and_malformed_records_fail_closed(self):
        report = self.summary(self.expected('u1', 'u1'), [self.result('u1')])
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['duplicate_ids'], ['u1'])
        report = self.summary(self.expected('u1'), [self.result('u1'), {'body': 'extra'}])
        self.assertEqual(report['status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
