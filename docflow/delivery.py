"""Versioned delivery helpers; never overwrite a human-edited copy or provenance."""
import re
from pathlib import Path
from .common import WorkflowError, atomic_text, digest, fingerprint, read_json, write_json


def publish_draft(directory, name, text):
    directory = Path(directory)
    if Path(name).name != name or not name:
        raise WorkflowError('UNSAFE_PATH', '交付名称必须是单个文件名')
    version = fingerprint({'text': text})
    auto = directory/'versions'/version/name
    if not auto.exists():
        atomic_text(auto, text, replace=False)
    elif auto.read_text() != text:
        raise WorkflowError('INTEGRITY_ERROR', '不可变交付版本损坏')
    record = directory/'delivery.json'
    previous = read_json(record) if record.exists() else {}
    working = directory/name
    edited = working.exists() and digest(working) != previous.get('published_sha256')
    if not working.exists() or not edited:
        atomic_text(working, text)
    result = {'version': version, 'latest_auto': str(auto.resolve()),
              'working': str(working.resolve()), 'human_edits_preserved': edited,
              'published_sha256': previous.get('published_sha256') if edited else digest(working)}
    write_json(record,result)
    return result


def merge_format_notes(previous, additions):
    result = list(previous)
    seen = {fingerprint(n) for n in result}
    for note in additions:
        key = fingerprint(note)
        if key not in seen:
            result.append(note);seen.add(key)
    return result


def wps_link(target, output_dir):
    """WPS expects a native path; long native paths get a stable short alias."""
    import os
    target = Path(target).resolve()
    if not target.is_file():
        raise WorkflowError('SOURCE_MISSING', '超链接目标不存在', scope=str(target))
    address = str(target)
    if len(address) <= 230 and '#' not in address and '%' not in address:
        return address
    alias = Path(output_dir).resolve()/'L'/(fingerprint(address)[:24]+target.suffix)
    alias.parent.mkdir(parents=True,exist_ok=True)
    if alias.is_symlink() or alias.exists():
        if alias.resolve()!=target:
            raise WorkflowError('INTEGRITY_ERROR','短链接对应关系冲突')
    else:
        alias.symlink_to(os.path.relpath(target,alias.parent))
    return str(alias)


def render_reading(document, units, source):
    """Render only display labels; stable source/unit identities live in the sidecar."""
    lines=['# '+document['name'],'','状态：待人工审核；修改本阅读稿后须经 import-review 导入，原批次不会自动采用修改。','',f'[打开原件](<{Path(source).resolve().as_uri()}>)','']
    provenance=[]
    for i,unit in enumerate(units,1):
        label=unit['label'];format_notes=[]
        def style(match):
            format_notes.append({'style':match[1],'text':match[2]})
            return match[2]
        body=re.sub(r'\[((?:红字|蓝字|绿字|黄字|橙字|紫字|黄底|高亮))开始\](.*?)\[\1结束\]', style, unit['body'], flags=re.S)
        body=re.sub(r'(?m)^(#{1,5}) ', lambda m:'#'*min(6,len(m[1])+2)+' ',body)
        lines+=['---','',f'## {label}；阅读段{i}','',body or '**本单元未取得解析正文，请对照原件核查。**','']
        if unit.get('issues'):lines+=['> 解析提示：'+'；'.join(unit['issues']),'']
        if format_notes:
            lines+=['> 原文格式说明：'+'；'.join(n['style']+'：'+n['text'].replace('\n',' / ') for n in format_notes),'']
        provenance.append({'id':unit['id'],'label':label,'body_sha256':fingerprint(body),'format_notes':format_notes})
    return '\n'.join(lines),provenance
