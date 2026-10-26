import unittest
from html.parser import HTMLParser

from answer_format import presentation_issues,render_answer


class TextView(HTMLParser):
    def __init__(self):
        super().__init__();self.text=[];self.tags=[]
    def handle_starttag(self,tag,attrs):self.tags.append(tag)
    def handle_data(self,data):self.text.append(data)


class AnswerStandardTests(unittest.TestCase):
    def test_citation_metadata_becomes_readable_references_without_changing_facts(self):
        source='## 费用\n\n**金额 1200–1600 欧元**。<kb doc="费用表.pdf.md" chunk_id="chunk-123" kb_id="kb-456" />\n\n再次引用<kb doc="费用表.pdf.md" chunk_id="chunk-789" kb_id="kb-456" />。'
        html=render_answer(source)
        self.assertNotIn('chunk_id',html)
        self.assertNotIn('&lt;kb ',html)
        self.assertNotIn('kb-456',html)
        self.assertEqual(html.count('<sup>[1]</sup>'),2)
        self.assertEqual(html.count('费用表.pdf.md'),1)
        self.assertIn('金额 1200–1600 欧元',html)
        self.assertIn('引用资料',html)
        self.assertIn('citation_missing',presentation_issues(html.replace('费用表.pdf.md',''),source))

    def test_uniform_fragment_has_no_conversion_wrapper_or_executable_document(self):
        source='# 西班牙生活成本\n\n| 城市 | 月租 |\n|---|---|\n| 马德里 | 1200–1600 欧元 |'
        html=render_answer(source)
        self.assertIn('data-format-version="2"',html)
        self.assertIn('line-height:1.7',html)
        self.assertIn('border-collapse:collapse',html)
        view=TextView();view.feed(html)
        self.assertNotIn('html',view.tags);self.assertNotIn('head',view.tags);self.assertNotIn('style',view.tags)
        self.assertIn('table',view.tags)
        self.assertNotIn('```',html)
        self.assertNotIn('以下是上述内容转化',html)

    def test_code_samples_and_unknown_tags_remain_visible_without_execution(self):
        source='```xml\n<kb doc="示例" chunk_id="example" />\n```\n\n未知标签 <b>示例</b>\n\n<kb doc="正式资料.md" chunk_id="real" kb_id="real-kb" />'
        html=render_answer(source)
        self.assertIn('&lt;kb doc=',html)
        self.assertIn('example',html)
        self.assertIn('&lt;b&gt;',html)
        self.assertIn('正式资料.md',html)
        self.assertNotIn('chunk_id="real"',html)

    def test_legacy_model_conversion_is_rejected_and_regenerated_from_source(self):
        source='# 答案\n\n办理周期 **6个月**。'
        legacy='以下是上述内容转化为 HTML 格式的版本：\n```html\n<html><head><style>p{color:red}</style></head><body><p>另一个答案</p></body></html>\n```'
        issues=presentation_issues(legacy,source)
        self.assertIn('full_document_or_unsafe_html',issues)
        self.assertIn('source_text_missing',issues)
        fixed=render_answer(source)
        self.assertEqual(presentation_issues(fixed,source),[])
        self.assertNotIn('以下是上述内容',fixed)
        self.assertIn('6个月',fixed)


if __name__=='__main__':unittest.main()
