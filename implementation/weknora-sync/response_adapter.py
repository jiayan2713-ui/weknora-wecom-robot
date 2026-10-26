"""One envelope adapter; source characters inside the body remain data."""
import re
from docflow.parser import split_notes


def response_parts(text):
    text = re.sub(r'(?m)^([ \t]*#{1,6})(?=(?:解析|核对)记录[（(]非原文[）)][ \t]*$)', r'\1 ', text)
    body, notes = split_notes(text)
    body = re.sub(r'\A\s*#{1,6}\s*正文\s*\n?', '', body, count=1).strip()
    return body, notes
