"""Freeze bounded WPS checkboxes into their embedded pictures and native evidence."""
import hashlib
import json
import posixpath
from pathlib import PurePosixPath

from lxml import etree as E

from .common import WorkflowError

WPS = 'http://www.wps.cn/officeDocument/2020/jdeControlExtension'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def fail(message):
    raise WorkflowError('UNSAFE_OFFICE', 'WPS复选框无法完整静态化：' + message)


def xml(raw):
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        fail('XML实体')
    return E.fromstring(raw, E.XMLParser(resolve_entities=False, no_network=True))


def resolve(owner, target):
    if not target or ':' in target or target.startswith(('/', '\\')) or '\\' in target:
        fail('非法关系路径')
    result = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
    if result.startswith('../'):
        fail('关系路径越界')
    return result


def _freeze(archive):
    """Return byte patches, removed parts and native notes; never alter cell payloads."""
    names = set(archive.namelist())
    parts = {n for n in names if n.startswith('xl/jdecontrols/') and n.endswith('.xml')}
    if not parts:
        return {}, set(), []
    if len(parts) > 1000:
        fail('控件数超限')
    controls = {}
    allowed = {'jdecontrol', 'control', 'property', 'enabled', 'word_wrap', 'auto_size',
               'control_source', 'caption', 'name', 'back_color', 'fore_color', 'visible',
               'back_style', 'value', 'left', 'top', 'width', 'height', 'font', 'family',
               'pointsize', 'weight', 'italic', 'underline', 'strickout'}
    for part in sorted(parts):
        root = xml(archive.read(part))
        if root.tag != '{' + WPS + '}jdecontrol':
            fail('未知控件命名空间')
        for node in root.iter():
            if not isinstance(node.tag, str) or E.QName(node).localname not in allowed:
                fail('未知事件或控件字段')
        nodes = root.findall('control')
        if len(nodes) != 1 or nodes[0].get('type') != 'checkbox':
            fail('只支持独立checkbox')
        control = nodes[0]
        if any(p.get('type') not in {'actions', 'data', 'facade', 'geometry', 'font'} for p in control.findall('property')):
            fail('未知属性组')
        if any((n.text or '').strip() for n in control.findall('.//control_source')):
            fail('存在数据绑定')
        values = control.findall("property[@type='facade']/value")
        if len(values) != 1 or values[0].text not in ('true', 'false'):
            fail('勾选状态缺失或不明确')
        controls[part] = {'part': part, 'checked': values[0].text == 'true',
                          'caption': control.findtext("property[@type='facade']/caption", ''),
                          'name': control.findtext("property[@type='facade']/name", ''),
                          'control_sha256': hashlib.sha256(archive.read(part)).hexdigest()}
    # Resolve workbook -> worksheet -> drawing, rather than guessing from part numbers.
    book = xml(archive.read('xl/workbook.xml'))
    bookrels = {n.get('Id'): n for n in xml(archive.read('xl/_rels/workbook.xml.rels'))}
    drawings = {}
    for sheet in book.findall('.//{*}sheet'):
        rel = bookrels[sheet.get('{' + R + '}id')]
        if rel.get('TargetMode') == 'External':
            fail('外部工作表')
        part = resolve('xl/workbook.xml', rel.get('Target'))
        sheetxml = xml(archive.read(part))
        relpart = str(PurePosixPath(part).parent / '_rels' / (PurePosixPath(part).name + '.rels'))
        if relpart not in names:
            continue
        mapping = {n.get('Id'): n for n in xml(archive.read(relpart))}
        for drawing in sheetxml.findall('.//{*}drawing'):
            rel = mapping[drawing.get('{' + R + '}id')]
            if rel.get('TargetMode') == 'External':
                fail('外部绘图')
            dp = resolve(part, rel.get('Target'))
            if dp in drawings:
                fail('绘图被多张表共用')
            drawings[dp] = sheet.get('name')
    patches, seen, evidence = {}, set(), []
    for drawing, sheet in drawings.items():
        tree = xml(archive.read(drawing))
        relpart = str(PurePosixPath(drawing).parent / '_rels' / (PurePosixPath(drawing).name + '.rels'))
        if relpart not in names:
            continue
        rels = xml(archive.read(relpart))
        mapping = {n.get('Id'): n for n in rels}
        changed = False
        for anchor in tree:
            refs = anchor.findall('.//{' + WPS + '}jdecontrolRef')
            if not refs:
                continue
            if len(refs) != 1:
                fail('锚点关联多个控件')
            ref = refs[0]
            rel = mapping[ref.get('{' + R + '}id')]
            if rel.get('TargetMode') == 'External' or rel.get('Type') != WPS:
                fail('控件关系类型异常')
            cp = resolve(drawing, rel.get('Target'))
            if cp not in controls or cp in seen:
                fail('控件引用缺失或重复')
            pics = anchor.findall('{*}pic')
            blips = anchor.findall('.//{*}blip')
            if len(pics) != 1 or len(blips) != 1:
                fail('缺少唯一静态预览')
            image = mapping[blips[0].get('{' + R + '}embed')]
            if image.get('TargetMode') == 'External' or not image.get('Type', '').endswith('/image'):
                fail('预览不是内嵌图片')
            ip = resolve(drawing, image.get('Target'))
            image_hash = hashlib.sha256(archive.read(ip)).hexdigest()
            location = {}
            for edge in ('from', 'to'):
                node = anchor.find('{*}' + edge)
                if node is None:
                    fail('缺少单元格锚点')
                location[edge] = {E.QName(n).localname: int(n.text) for n in node}
                if set(location[edge]) != {'row', 'col', 'rowOff', 'colOff'}:
                    fail('锚点不完整')
            evidence.append({**controls[cp], 'sheet': sheet, 'anchor': location,
                             'preview_part': ip, 'preview_sha256': image_hash})
            ref.getparent().remove(ref)
            rels.remove(rel)
            seen.add(cp)
            changed = True
        if changed:
            patches[drawing] = E.tostring(tree, encoding='utf-8', xml_declaration=True)
            patches[relpart] = E.tostring(rels, encoding='utf-8', xml_declaration=True)
    if seen != parts:
        fail('存在未完整定位的控件')
    types = xml(archive.read('[Content_Types].xml'))
    for node in list(types):
        if node.get('PartName', '').lstrip('/') in parts:
            types.remove(node)
    patches['[Content_Types].xml'] = E.tostring(types, encoding='utf-8', xml_declaration=True)
    lines = ['WPS复选框原生记录：状态为原件保存值，未执行控件；行列为零基锚点，偏移单位EMU。']
    lines.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in evidence)
    notes = []
    for offset in range(1, len(lines), 12):
        text = lines[0] + '\n' + '\n'.join(lines[offset:offset + 12])
        notes.append({'label': '原件复选框状态记录 ' + str(len(notes) + 1),
                      'text': text, 'native_body': text, 'method': 'native_text'})
    return patches, parts, notes


def freeze(archive):
    try:
        return _freeze(archive)
    except (KeyError, ValueError, E.XMLSyntaxError) as exc:
        fail('关系或字段不完整：' + type(exc).__name__)


def verify_publication(source, text):
    import zipfile
    with zipfile.ZipFile(source) as archive:
        _, removed, notes = freeze(archive)
    if not removed:
        return None
    for note in notes:
        for line in note['native_body'].splitlines()[1:]:
            if text.count(line) != 1:
                raise WorkflowError('UNIT_COVERAGE_INCOMPLETE', '最终正文遗漏或重复复选框原生状态')
    records = [json.loads(line) for note in notes for line in note['native_body'].splitlines()[1:]]
    return {'controls': len(records), 'checked': sum(r['checked'] for r in records),
            'unchecked': sum(not r['checked'] for r in records), 'all_native_states_present': True}
