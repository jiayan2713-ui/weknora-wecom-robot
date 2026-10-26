"""Remove only bounded, empty worksheet tails in an independent parsing copy."""
import hashlib
import copy
import json
from pathlib import Path
import re
import zipfile

from lxml import etree as E
from reconcile import hashes, save

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def meaningful(cell):
    for node in cell:
        tag = E.QName(node).localname
        if tag == 'f' or tag not in ('v', 'is'):
            return True
        if any(text not in (None, '') for text in node.itertext()):
            return True
    return False


def content_signature(cell):
    return json.dumps([(node.tag, dict(node.attrib), node.text) for node in cell.iter()],
                      sort_keys=True, ensure_ascii=False).encode()


def scan_sheet(stream, prefix_limit=512):
    prefix, digest, rows, cells, nonempty, last = [], hashlib.sha256(), 0, 0, 0, 0
    context = E.iterparse(stream, events=('end',), tag='{'+S+'}row',
                          resolve_entities=False, no_network=True)
    for _, row in context:
        number = int(row.attrib['r'])
        rows += 1
        for cell in row:
            if E.QName(cell).localname != 'c':
                raise ValueError('unsupported_worksheet_row_child')
            cells += 1
            if meaningful(cell):
                last = max(last, number)
                nonempty += 1
                digest.update(content_signature(cell))
                if number > prefix_limit:
                    raise ValueError('worksheet_has_real_content_beyond_compaction_bound')
        if number <= prefix_limit:
            prefix.append((number, E.tostring(row)))
        row.clear()
        while row.getprevious() is not None:
            del row.getparent()[0]
    root = context.root
    data = root.find('{'+S+'}sheetData')
    if data is None:
        raise ValueError('worksheet_sheet_data_missing')
    for row in list(data):
        data.remove(row)
    for element in root.iter():
        if E.QName(element).localname in ('mergeCell', 'hyperlink', 'tablePart'):
            for number in re.findall(r'[A-Za-z]+(\d+)', element.get('ref', '')):
                last = max(last, int(number))
    return root, prefix, {'rows': rows, 'cells': cells, 'nonempty_cells': nonempty,
                          'last_content_row': last, 'content_sha256': digest.hexdigest()}


def compact(source, destination, report_path, *, threshold_bytes=32 * 1024 * 1024):
    source, destination = Path(source), Path(destination)
    before = hashes(source)
    changed, summaries = {}, []
    with zipfile.ZipFile(source) as incoming:
        entries = incoming.infolist()
        if len({i.filename for i in entries}) != len(entries):
            raise ValueError('xlsx_duplicate_zip_members')
        if sum(i.file_size for i in entries) > 512 * 1024 * 1024:
            raise ValueError('xlsx_expanded_budget_exceeded')
        drawings_bound = 0
        for entry in entries:
            if entry.filename.startswith('xl/drawings/') and entry.filename.endswith('.xml'):
                if entry.file_size > 1024 * 1024:
                    raise ValueError('xlsx_drawing_metadata_too_large')
                root = E.fromstring(incoming.read(entry), E.XMLParser(resolve_entities=False, no_network=True))
                for node in root.iter():
                    if E.QName(node).localname == 'row' and node.text and node.text.isdigit():
                        drawings_bound = max(drawings_bound, int(node.text) + 1)
        for entry in entries:
            if not entry.filename.startswith('xl/worksheets/') or not entry.filename.endswith('.xml') or entry.file_size <= threshold_bytes:
                continue
            if entry.file_size > 128 * 1024 * 1024:
                raise ValueError('xlsx_sheet_streaming_budget_exceeded')
            with incoming.open(entry) as stream:
                root, prefix, summary = scan_sheet(stream)
            keep_until = max(summary['last_content_row'] + 1, drawings_bound)
            if keep_until > 512:
                raise ValueError('xlsx_visible_extent_exceeds_compaction_bound')
            data = root.find('{'+S+'}sheetData')
            kept = 0
            for number, raw in prefix:
                if number <= keep_until:
                    data.append(E.fromstring(raw, E.XMLParser(resolve_entities=False, no_network=True)))
                    kept += 1
            dimension = root.find('{'+S+'}dimension')
            if dimension is not None:
                coordinates = [c.get('r') for row in data for c in row]
                from openpyxl.utils.cell import coordinate_from_string, column_index_from_string, get_column_letter
                maximum_column = max((column_index_from_string(coordinate_from_string(c)[0]) for c in coordinates), default=1)
                dimension.set('ref', 'A1:' + get_column_letter(maximum_column) + str(keep_until))
            changed[entry.filename] = E.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
            summaries.append({'part': entry.filename, **summary, 'kept_rows': kept,
                              'removed_empty_tail_rows': summary['rows'] - kept,
                              'before_bytes': entry.file_size, 'after_bytes': len(changed[entry.filename])})
        if not changed:
            raise ValueError('xlsx_has_no_oversized_empty_tail')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, 'w') as outgoing:
            for entry in entries:
                outgoing.writestr(copy.copy(entry), changed[entry.filename] if entry.filename in changed else incoming.read(entry))
        with zipfile.ZipFile(destination) as outgoing:
            for entry in entries:
                if entry.filename not in changed and incoming.read(entry) != outgoing.read(entry.filename):
                    raise ValueError('xlsx_unchanged_part_mismatch')
            import io
            for summary in summaries:
                _, _, check = scan_sheet(io.BytesIO(outgoing.read(summary['part'])))
                if check['content_sha256'] != summary['content_sha256'] or check['nonempty_cells'] != summary['nonempty_cells']:
                    raise ValueError('xlsx_cell_formula_or_cache_mismatch')
    if hashes(source)['sha256'] != before['sha256']:
        raise ValueError('xlsx_source_changed_during_compaction')
    proof = {'source': str(source), 'source_sha256': before['sha256'], 'output': str(destination),
             'output_sha256': hashes(destination)['sha256'], 'sheets': summaries,
             'all_value_formula_cache_records_equal': True, 'unchanged_zip_parts_byte_equal': True,
             'human_review': False}
    save(report_path, proof)
    return proof
