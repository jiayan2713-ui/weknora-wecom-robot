"""Bounded, reversible preparation policies. No source file is changed."""
import json
import subprocess
from pathlib import Path
from .common import WorkflowError, write_json


def unicode_evidence(text, page, work):
    invalid = [{'offset': i, 'codepoint': f'U+{ord(c):04X}'} for i, c in enumerate(text)
               if 0xD800 <= ord(c) <= 0xDFFF]
    if not invalid:
        return text, None
    path = Path(work) / f'unicode-page-{page:04d}.json'
    write_json(path, {'page': page, 'invalid': invalid,
                     'original_unicode_escape': text.encode('unicode_escape').decode('ascii')})
    # The image remains authoritative; don't feed corrupted native text as source text.
    return '', {'label': '转换处理记录（非原文）',
                'text': f'第{page}页文本层含非法Unicode字符，本页按完整图像转写；原码点保存在{path.name}，需对照原页核查。'}


def native_sheet_units(sheet, char_budget=12000):
    """Preserve every cell/merge without asking a model to rewrite machine-readable data."""
    def cell_text(cell):
        return json.dumps(cell, ensure_ascii=False, default=str)
    result, chunk, size = [], [], 0
    def flush():
        nonlocal chunk, size
        if chunk:
            result.append({'label': f'工作表{sheet["name"]}；单元格{chunk[0][0]}–{chunk[-1][0]}',
                           'text': '\n'.join(x[1] for x in chunk), 'image': None,
                           'native_body': '\n'.join(x[1] for x in chunk),
                           'method': 'native_cells'})
            chunk, size = [], 0
    for cell in sheet['cells']:
        line = cell_text(cell)
        # A long cell is split reversibly, with its coordinate and sequence retained.
        for offset in range(0, max(len(line), 1), char_budget - 200):
            part = line[offset:offset + char_budget - 200]
            if chunk and size + len(part) + 80 > char_budget:
                flush()
            chunk.append((cell['coordinate'], f'{cell["coordinate"]} 字符段{offset+1}：{part}'))
            size += len(part) + 80
    flush()
    meta = {k: v for k, v in sheet.items() if k != 'cells'}
    raw = json.dumps(meta, ensure_ascii=False, default=str)
    for offset in range(0, len(raw), char_budget):
        text = raw[offset:offset+char_budget]
        result.append({'label': f'工作表{sheet["name"]}；表结构记录{offset//char_budget+1}',
                       'text': text, 'native_body': text, 'image': None, 'method': 'native_cells'})
    return result
