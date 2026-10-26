"""Recognize active image syntax while preserving escaped and code-span source text."""
import re


def _prose(text):
    lines=[]; fence=None
    for line in text.splitlines(keepends=True):
        token=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
        if token:
            mark=token.group(1)
            if fence is None:fence=mark
            elif mark[0]==fence[0] and len(mark)>=len(fence):fence=None
            lines.append('\n');continue
        lines.append('\n' if fence is not None else line)
    text=''.join(lines)
    # Backtick delimiters must have equal length; an unmatched backtick stays source text.
    pattern=r'(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)'
    return re.sub(pattern,lambda m:' '*(len(m.group())),text)


def _active(text, start):
    before=text[:start];slashes=len(before)-len(before.rstrip('\\'))
    return slashes%2==0


def has_image_reference(text):
    text=_prose(text)
    if re.search(r'<img\s',text,re.I):return True
    for match in re.finditer(r'!\[(?:[^\[\]\n]|\[[^\]\n]*\])*\]\s*\(', text):
        if _active(text,match.start()):return True
    definitions={m.group(1).strip().casefold() for m in re.finditer(r'(?m)^ {0,3}\[([^\]\n]+)\]:\s*\S+',text)}
    for match in re.finditer(r'!\[([^\]\n]+)\](?:[ \t]*\[([^\]\n]*)\])?',text):
        if _active(text,match.start()) and (match.group(2) or match.group(1)).strip().casefold() in definitions:return True
    return False
