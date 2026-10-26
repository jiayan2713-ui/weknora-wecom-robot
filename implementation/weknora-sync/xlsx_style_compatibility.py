"""Repair only empty fill nodes in a private XLSX copy; all other ZIP entries exact."""
from pathlib import Path
import copy
import re
import zipfile
import xml.etree.ElementTree as ET

from reconcile import hashes, save


def compatible_copy(source, work):
    namespace = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(source) as archive:
        if 'xl/styles.xml' not in archive.namelist():
            return None, None
        styles = archive.read('xl/styles.xml')
        root = ET.fromstring(styles)
        fills = root.find(namespace + 'fills')
        if fills is None:
            return None, None
        empty = [i for i, fill in enumerate(fills) if len(fill) == 0 and not fill.attrib and not (fill.text or '').strip()]
        if not empty:
            return None, None
        # Preserve namespace prefixes and every other XML byte; never renumber style IDs.
        pattern = rb'<(?P<p>(?:[A-Za-z_][\w.-]*:)?)fill\s*(?:/\s*>|>\s*</(?P=p)fill\s*>)'
        def replace(match):
            prefix = match.group('p')
            return b'<' + prefix + b'fill><' + prefix + b'patternFill patternType="none"/></' + prefix + b'fill>'
        repaired, count = re.subn(pattern, replace, styles)
        if count != len(empty):
            raise ValueError('xlsx_empty_fill_repair_ambiguous')
        target = Path(work) / 'xlsx-compatible' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, 'w') as output:
            for entry in archive.infolist():
                output.writestr(copy.copy(entry), repaired if entry.filename == 'xl/styles.xml' else archive.read(entry.filename))
        with zipfile.ZipFile(target) as result:
            if archive.namelist() != result.namelist() or any(archive.read(name) != result.read(name)
                    for name in archive.namelist() if name != 'xl/styles.xml'):
                raise ValueError('xlsx_compatibility_nonstyle_content_changed')
    import openpyxl
    book = openpyxl.load_workbook(target, read_only=True, data_only=False)
    book.close()
    receipt = {'method': 'empty_fill_to_default_pattern', 'source_sha256': hashes(source)['sha256'],
               'compatible_sha256': hashes(target)['sha256'], 'empty_fill_indexes': empty,
               'all_other_zip_entries_unchanged': True, 'original_preserved': True, 'human_review': False}
    save(Path(work) / 'xlsx-style-compatibility.json', receipt)
    return target, receipt
