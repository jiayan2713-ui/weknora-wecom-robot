"""Read-only table admission checks; no Markdown generation, model calls or recalculation."""
import csv
import io
import json
import posixpath
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as E


def normalize(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value))).replace('\\|','|')


def inventory(path):
    path=Path(path)
    if path.stat().st_size>64*1024*1024:raise ValueError('table_inventory_file_capacity')
    cells=[];sheets=[];controls=[];uncached=[]
    if path.suffix.lower()=='.csv':
        raw=path.read_bytes()
        if raw[:4] in (b'\xff\xfe\x00\x00',b'\x00\x00\xfe\xff'):text=raw.decode('utf-32')
        elif raw[:2] in (b'\xff\xfe',b'\xfe\xff'):text=raw.decode('utf-16')
        else:
            try:text=raw.decode('utf-8-sig')
            except UnicodeError:text=raw.decode('gb18030')
        for r,row in enumerate(csv.reader(io.StringIO(text)),1):
            for c,value in enumerate(row,1):
                if value.strip():cells.append({'sheet':'CSV','row':r,'column':c,'value':value,'type':'text'})
        sheets=['CSV']
    elif path.read_bytes()[:4] == b'PK\x03\x04' and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            entries=z.infolist()
            if len(entries)>20000 or sum(e.file_size for e in entries)>1024**3:raise ValueError('table_inventory_xml_capacity')
            names=set(z.namelist());strings=[]
            if 'xl/workbook.xml' not in names:
                if 'content.xml' not in names:raise ValueError('unrecognized_zip_spreadsheet')
                tree=E.fromstring(z.read('content.xml'));table_ns='urn:oasis:names:tc:opendocument:xmlns:table:1.0';office_ns='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
                for sheet in tree.findall('.//{'+table_ns+'}table'):
                    name=sheet.get('{'+table_ns+'}name');sheets.append(name);row_index=0
                    for row in sheet.findall('{'+table_ns+'}table-row'):
                        repeat=int(row.get('{'+table_ns+'}number-rows-repeated','1'));column=0;row_cells=[]
                        for cell in row:
                            count=int(cell.get('{'+table_ns+'}number-columns-repeated','1'));kind=cell.get('{'+office_ns+'}value-type','string')
                            value=cell.get('{'+office_ns+'}value') or cell.get('{'+office_ns+'}date-value') or cell.get('{'+office_ns+'}boolean-value') or '\n'.join(''.join(n.itertext()) for n in cell.findall('{*}p'))
                            if value and value.strip():
                                if count*repeat>10000:raise ValueError('ods_repetition_capacity')
                                for offset in range(count):row_cells.append({'sheet':name,'column':column+offset+1,'value':value,'type':'n' if kind=='float' else 'text'})
                            column+=count
                        for offset in range(repeat if row_cells else 0):
                            cells.extend({**c,'row':row_index+offset+1} for c in row_cells)
                        row_index+=repeat
                return {'sheets':sheets,'cells':cells,'controls':controls,'uncached_formulas':uncached}
            if 'xl/sharedStrings.xml' in names:
                for event,node in E.iterparse(z.open('xl/sharedStrings.xml'),events=['end']):
                    if node.tag.rsplit('}',1)[-1]=='si':strings.append(''.join(n.text or '' for n in node.iter() if n.tag.rsplit('}',1)[-1]=='t'));node.clear()
            date_styles=set();epoch_1904=False
            if 'xl/styles.xml' in names:
                from openpyxl.styles.numbers import BUILTIN_FORMATS,is_date_format
                styles=E.fromstring(z.read('xl/styles.xml'));formats={n.attrib['numFmtId']:n.attrib['formatCode'] for n in styles.findall('.//{*}numFmt')}
                xfs=styles.find('{*}cellXfs')
                if xfs is not None:
                    date_styles={i for i,n in enumerate(xfs) if is_date_format(formats.get(n.get('numFmtId'),BUILTIN_FORMATS.get(int(n.get('numFmtId','0')),'')))}
            book=E.fromstring(z.read('xl/workbook.xml'))
            props=book.find('{*}workbookPr');epoch_1904=props is not None and props.get('date1904') in ('1','true')
            rels={r.attrib['Id']:r.attrib['Target'] for r in E.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
            for sheet in book.iter():
                if sheet.tag.rsplit('}',1)[-1]!='sheet':continue
                name=sheet.attrib['name'];sheets.append(name);rid=next(v for k,v in sheet.attrib.items() if k.rsplit('}',1)[-1]=='id');target=rels[rid];target=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
                if '..' in Path(target).parts:raise ValueError('unsafe_sheet_path')
                for event,node in E.iterparse(z.open(target),events=['end']):
                    if node.tag.rsplit('}',1)[-1]!='c':continue
                    coord=node.attrib.get('r','');typ=node.attrib.get('t','n');v=node.find('{*}v');inline=node.find('{*}is');f=node.find('{*}f');value=v.text if v is not None else None
                    if typ=='s' and value is not None:value=strings[int(value)]
                    elif inline is not None:value=''.join(n.text or '' for n in inline.iter() if n.tag.rsplit('}',1)[-1]=='t')
                    if typ=='n' and value is not None and int(node.get('s','0')) in date_styles:
                        from openpyxl.utils.datetime import from_excel,MAC_EPOCH,WINDOWS_EPOCH
                        try:value=str(from_excel(float(value),epoch=MAC_EPOCH if epoch_1904 else WINDOWS_EPOCH));typ='date'
                        except (ValueError,OverflowError):pass
                    if f is not None and value is None:uncached.append({'sheet':name,'coordinate':coord})
                    if value is not None and str(value).strip():cells.append({'sheet':name,'coordinate':coord,'value':str(value),'type':typ})
                    node.clear()
            for name in names:
                if name.startswith('xl/jdecontrols/') and name.endswith('.xml'):
                    tree=E.fromstring(z.read(name));value=tree.findtext(".//property[@type='facade']/value")
                    controls.append({'part':name,'checked':value})
    else:
        try:import xlrd
        except ModuleNotFoundError:
            from reconcile import PROJECT
            sys.path.insert(0,str(PROJECT/'.local/table-native-deps'));import xlrd
        book=xlrd.open_workbook(path,on_demand=True)
        try:
            for sheet in book.sheets():
                sheets.append(sheet.name)
                for r in range(sheet.nrows):
                    for c in range(sheet.ncols):
                        cell=sheet.cell(r,c)
                        value=cell.value
                        if cell.ctype==xlrd.XL_CELL_DATE:value=xlrd.xldate_as_datetime(value,book.datemode)
                        if cell.ctype not in (xlrd.XL_CELL_EMPTY,xlrd.XL_CELL_BLANK) and str(cell.value).strip():cells.append({'sheet':sheet.name,'row':r+1,'column':c+1,'value':str(value),'type':'n' if cell.ctype==xlrd.XL_CELL_NUMBER else 'b' if cell.ctype==xlrd.XL_CELL_BOOLEAN else 'date' if cell.ctype==xlrd.XL_CELL_DATE else 'text'})
        finally:book.release_resources()
    return {'sheets':sheets,'cells':cells,'controls':controls,'uncached_formulas':uncached}


def coverage(native,text):
    flat=normalize(text);tokens=set()
    for match in re.finditer(r'(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?',text):
        try:tokens.add(Decimal(match.group().replace(',','')))
        except InvalidOperation:pass
    missing=[]
    for cell in native['cells']:
        value=cell['value'];needle=normalize(value)
        if needle and needle in flat:continue
        if cell['type'] in ('n','b'):
            try:
                number=Decimal(value)
                if number in tokens:continue
                # Excel stores numeric cells as binary64. XML may expose more
                # digits than the reader's shortest equivalent representation.
                if number.is_finite() and number!=number.to_integral() and abs(number)<2**53:
                    if any(token.is_finite() and token!=token.to_integral() and float(number)==float(token) for token in tokens):continue
            except InvalidOperation:pass
            if cell['type']=='b' and (('true' if value=='1' else 'false') in flat.lower()):continue
        missing.append({k:v for k,v in cell.items() if k!='value'})
    issues=[]
    missing_sheets=[name for name in native['sheets'] if name!='CSV' and normalize(name) not in flat]
    if missing_sheets:issues.append('worksheet_names_missing')
    missing_controls=[r['part'] for r in native['controls'] if normalize('WPS_CHECKBOX '+r['part']+' saved='+str(r['checked'])) not in flat]
    if missing_controls:issues.append('saved_checkbox_states_missing')
    if not native['cells']:issues.append('no_native_cached_cell_text')
    return {'passed':not missing and not issues,'expected_cells':len(native['cells']),'missing_cells':missing,
            'sheets':native['sheets'],'saved_controls':len(native['controls']),'uncached_formulas':len(native['uncached_formulas']),
            'issues':issues,'missing_sheet_labels':missing_sheets,'missing_controls':missing_controls,'scope':'native_cached_cell_values; originals retain formulas/images; no model transcription'}
