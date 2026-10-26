"""Independent original/input structure inventory, without editing either file."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile

from docflow.common import WorkflowError, digest, write_json


def sheet_records(archive, name):
    checksum=hashlib.sha256();coordinates=[];merges=[];data=None
    with archive.open(name) as stream:
        for event,node in ET.iterparse(stream,events=('start','end')):
            tag=node.tag.rsplit('}',1)[-1]
            if event=='start':
                if tag=='sheetData':data=node
                continue
            if tag=='c':
                payload=[]
                for child in node:
                    kind=child.tag.rsplit('}',1)[-1]
                    if kind in ('f','v','is'):
                        payload.append([kind,sorted(child.attrib.items()),''.join(child.itertext())])
                if any(kind=='f' or text for kind,attrs,text in payload):
                    coordinate=node.attrib.get('r')
                    coordinates.append(coordinate)
                    checksum.update(json.dumps([sorted(node.attrib.items()),payload],ensure_ascii=False,separators=(',',':')).encode())
                node.clear()
            elif tag=='row' and data is not None:data.clear()
            elif tag=='mergeCell':merges.append(node.attrib.get('ref'));node.clear()
    from openpyxl.utils.cell import range_boundaries, coordinate_from_string, column_index_from_string
    rectangles=[range_boundaries(value) for value in merges]
    covered=[]
    for coordinate in coordinates:
        letters,row=coordinate_from_string(coordinate);column=column_index_from_string(letters)
        if any(left<=column<=right and top<=row<=bottom and (column,row)!=(left,top)
               for left,top,right,bottom in rectangles):covered.append(coordinate)
    return {'payload_sha256':checksum.hexdigest(),'coordinates':coordinates,'merges':sorted(merges),
            'merged_nonanchor_payloads':covered}


def catalog(path, pdf_proof=None):
    path=Path(path);result={'sha256':digest(path),'format':path.suffix.lower()}
    if path.suffix.lower()=='.pdf':
        if pdf_proof and pdf_proof.get('pages'):
            result['pdf_pages']=len(pdf_proof['pages'])
        else:
            from pypdf import PdfReader
            result['pdf_pages']=len(PdfReader(path).pages)
    elif path.suffix.lower() in ('.xlsx','.docx','.pptx'):
        with zipfile.ZipFile(path) as archive:
            if path.suffix.lower()=='.xlsx':
                root=ET.fromstring(archive.read('xl/workbook.xml'))
                rels=ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
                mapping={r.attrib['Id']:r.attrib['Target'] for r in rels}
                sheets=[]
                for node in root.iter():
                    if node.tag.rsplit('}',1)[-1]!='sheet':continue
                    rid=next(v for k,v in node.attrib.items() if k.rsplit('}',1)[-1]=='id')
                    target=mapping[rid];target=target.lstrip('/') if target.startswith('/') else str(PurePosixPath('xl')/target)
                    if '..' in PurePosixPath(target).parts:raise WorkflowError('INTEGRITY_ERROR','工作表引用越界')
                    sheets.append({'name':node.attrib['name'],**sheet_records(archive,target)})
                result['sheets']=sheets
            elif path.suffix.lower()=='.pptx':
                root=ET.fromstring(archive.read('ppt/presentation.xml'))
                result['slides']=sum(n.tag.rsplit('}',1)[-1]=='sldId' for n in root.iter())
            else:
                parts=[]
                for name in archive.namelist():
                    if re.fullmatch(r'word/(?:document|comments|footnotes|endnotes|header\d*|footer\d*)\.xml',name):
                        parts.append({'part':name,'sha256':hashlib.sha256(archive.read(name)).hexdigest()})
                result['document_parts']=parts
    return result


def check(cfg, original_hash, input_hash, expected, native, work):
    original=Path(cfg['_parse_original']);parsed=Path(cfg['_parse_input'])
    origin=catalog(original,cfg.get('_pdf_proof'));actual=catalog(parsed)
    if origin['sha256']!=original_hash or actual['sha256']!=input_hash:
        raise WorkflowError('CONTENT_CHANGED','结构清单与原件/解析输入不一致')
    if 'sheets' in origin:
        left={s['name']:s for s in origin['sheets']};right={s['name']:s for s in actual.get('sheets',[])}
        extracted={s['name']:s for s in native.get('native',{}).get('sheets',[])}
        if left.keys()!=right.keys() or left.keys()!=extracted.keys():
            raise WorkflowError('INTEGRITY_ERROR','原始工作表与解析输入/原生记录不一致')
        for name,sheet in left.items():
            if sheet['payload_sha256']!=right[name]['payload_sha256'] or sheet['merges']!=right[name]['merges']:
                raise WorkflowError('INTEGRITY_ERROR','解析副本改变单元格数据或合并结构')
            coords={c['coordinate'] for c in extracted[name]['cells']}
            if not (set(sheet['coordinates'])-set(sheet['merged_nonanchor_payloads'])).issubset(coords):
                raise WorkflowError('UNIT_COVERAGE_INCOMPLETE','原生准备记录遗漏原始非空单元格',scope={'sheet':name})
    pages=set()
    for unit in expected:
        index=unit.get('source_location',{}).get('page') or unit.get('region',{}).get('page')
        if index is None:
            match=re.search(r'(?:PDF第|本次固定渲染版第|幻灯片)(\d+)',unit.get('label',''))
            if match:index=int(match[1])
        if index:pages.add(index)
    count=origin.get('pdf_pages',origin.get('slides'))
    if count is not None and pages!=set(range(1,count+1)):
        raise WorkflowError('UNIT_COVERAGE_INCOMPLETE','原始页/幻灯片未完整映射到准备单元')
    proof={'schema_version':1,'original':origin,'parser_input':actual,
           'prepared_page_ids':sorted(pages),'human_review':False}
    write_json(Path(work)/'source-catalog.json',proof)
    return proof
