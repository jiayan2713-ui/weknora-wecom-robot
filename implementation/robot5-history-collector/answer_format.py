"""Versioned HTML presentation generated from unchanged source answers."""
from functools import lru_cache
from html import escape,unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).with_name('_vendor')))
import mistune
from mistune.plugins.table import table,table_in_list,table_in_quote
from mistune.plugins.formatting import strikethrough

FORMAT_VERSION=2
PLUGINS=[table,table_in_list,table_in_quote,strikethrough]
KB_TAG=re.compile(r'<kb\b(?:"[^"]*"|\x27[^\x27]*\x27|[^\x27">])*/?>',re.I)


class PresentationError(ValueError):
    pass


class CitationTag(HTMLParser):
    def __init__(self):
        super().__init__();self.document=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='kb' and attrs.get('doc') and (attrs.get('chunk_id') or attrs.get('kb_id')):
            self.document=attrs['doc']


def citation_document(raw):
    if not KB_TAG.fullmatch(raw.strip()):return None
    parser=CitationTag();parser.feed(raw.strip())
    return parser.document


class AnswerRenderer(mistune.HTMLRenderer):
    def __init__(self,**kwargs):
        super().__init__(**kwargs);self.references=[]

    def safe_url(self,url):
        cleaned=''.join(char for char in url if ord(char)>32)
        try:scheme=urlsplit(cleaned).scheme.lower()
        except ValueError:return '#blocked-link'
        if scheme not in ['', 'http', 'https', 'mailto']:return '#blocked-link'
        return escape(url,quote=True)

    def citation(self,raw):
        name=citation_document(raw)
        if not name:return None
        if name not in self.references:self.references.append(name)
        return '<sup>['+str(self.references.index(name)+1)+']</sup>'

    def inline_html(self,raw):
        return self.citation(raw) or super().inline_html(raw)

    def block_html(self,raw):
        tags=KB_TAG.findall(raw)
        if tags and not KB_TAG.sub('',raw).strip() and all(citation_document(t) for t in tags):
            return '<p>'+''.join(self.citation(t) for t in tags)+'</p>\n'
        return super().block_html(raw)

    def table(self,text):
        return '<table style="width:100%;border-collapse:collapse;margin:12px 0;">\n'+text+'</table>\n'

    def table_cell(self,text,align=None,head=False):
        tag='th' if head else 'td'
        style='border:1px solid #d9d9d9;padding:8px 10px;vertical-align:top;text-align:'+(align or 'left')+';'
        if head:style+='background-color:#f5f5f5;font-weight:600;'
        return '<'+tag+' style="'+style+'">'+text+'</'+tag+'>\n'


class PresentationView(HTMLParser):
    def __init__(self):
        super().__init__();self.text=[];self.tags=[];self.version=None
    def handle_starttag(self,tag,attrs):
        self.tags.append(tag)
        if tag=='div' and dict(attrs).get('data-format-version'):self.version=dict(attrs)['data-format-version']
    def handle_data(self,data):self.text.append(data)


def normalized_text(value):
    return re.sub(r'\s+',' ',value).strip()


def presentation_issues(html,source):
    """Validate structure and source text, not just API transport equality."""
    view=PresentationView();view.feed(html)
    issues=[]
    if view.version!=str(FORMAT_VERSION):issues.append('format_version_missing')
    if any(tag in view.tags for tag in ['html','head','body','style','script','iframe']):issues.append('full_document_or_unsafe_html')
    visible=normalized_text(' '.join(view.text))
    ast=mistune.create_markdown(renderer='ast',plugins=PLUGINS)(source)
    def check(tokens):
        for token in tokens:
            raw=token.get('raw')
            kind=token['type']
            if raw and kind in ['text','codespan','block_code','inline_html','block_html']:
                if kind in ['inline_html','block_html'] and citation_document(raw):
                    if normalized_text(citation_document(raw)) not in visible:issues.append('citation_missing')
                    continue
                if kind=='block_html' and KB_TAG.findall(raw) and not KB_TAG.sub('',raw).strip():
                    if any(normalized_text(citation_document(t) or '') not in visible for t in KB_TAG.findall(raw)):
                        issues.append('citation_missing')
                    continue
                expected=normalized_text(unescape(raw))
                if expected and expected not in unescape(visible):issues.append('source_text_missing')
            if token.get('children'):check(token['children'])
    check(ast)
    return sorted(set(issues))


@lru_cache(maxsize=1024)
def render_answer(source):
    if not source:return ''
    renderer=AnswerRenderer(escape=True)
    markdown=mistune.create_markdown(renderer=renderer,hard_wrap=True,plugins=PLUGINS)
    body=markdown(source)
    if renderer.references:
        body+='<hr /><p><strong>引用资料</strong></p><ol>'+''.join('<li>'+escape(name)+'</li>' for name in renderer.references)+'</ol>\n'
    html='<div data-format-version="2" style="font-size:14px;line-height:1.7;color:#222;">\n'+body+'</div>'
    issues=presentation_issues(html,source)
    if issues:raise PresentationError(issues[0])
    return html
