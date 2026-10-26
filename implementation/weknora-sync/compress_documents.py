"""Compress images while preserving document text, geometry and other parts."""
import copy
import hashlib
import io
from pathlib import Path
import posixpath
import zipfile

from PIL import Image, ImageChops, ImageStat
from lxml import etree

LIMIT = 30_000_000
QUALITIES = (85, 75, 65, 55)
RASTER = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.gif'}
SUPPORTED = {'.pdf', '.docx', '.pptx', '.xlsx'}


def algorithm_version():
    # Code changes automatically release failures held by an earlier algorithm.
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def jpeg(raw, quality):
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if (getattr(image, 'n_frames', 1) > 1 or image.getexif().get(274, 1) != 1
                or 'transparency' in image.info
                or ('A' in image.getbands() and image.getchannel('A').getextrema()[0] < 255)):
            return raw
        buffer = io.BytesIO()
        rgb = image.convert('RGB')
        options = {'format': 'JPEG', 'quality': quality, 'subsampling': 0,
                   'icc_profile': image.info.get('icc_profile')}
        try:
            rgb.save(buffer, optimize=True, **options)
        except OSError as error:
            # Some high-entropy images exceed Pillow's optimized encoder buffer.
            # A fresh ordinary encoding keeps quality/dimensions/profile unchanged.
            if str(error) != 'broken data stream when writing image file':
                raise
            buffer = io.BytesIO()
            rgb.save(buffer, optimize=False, **options)
        result = buffer.getvalue()
        with Image.open(io.BytesIO(result)) as checked:
            checked.load()
            if checked.size != image.size:
                raise ValueError('image_dimensions_changed')
        return result if len(result) < len(raw) else raw


def office(source, output, quality):
    prefix = {'.docx': 'word/media/', '.pptx': 'ppt/media/', '.xlsx': 'xl/media/'}[source.suffix.lower()]
    changed, renamed = {}, {}
    with zipfile.ZipFile(source) as incoming:
        if any('/_xmlsignatures/' in '/' + name for name in incoming.namelist()):
            raise ValueError('signed_document_not_supported')
        for item in incoming.infolist():
            if item.filename.startswith(prefix) and Path(item.filename).suffix.lower() in RASTER:
                raw = incoming.read(item)
                result = jpeg(raw, quality)
                if result != raw:
                    changed[item.filename] = result
                    if Path(item.filename).suffix.lower() not in {'.jpg', '.jpeg'}:
                        name = str(Path(item.filename).with_name(Path(item.filename).stem + '_compressed.jpg'))
                        if name in incoming.namelist() or name in renamed.values():
                            raise ValueError('image_name_collision')
                        renamed[item.filename] = name
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        for name in incoming.namelist():
            if not name.endswith('.rels'):
                continue
            root = etree.fromstring(incoming.read(name), parser)
            base = str(Path(name).parent.parent)
            touched = False
            for relationship in root:
                if relationship.get('TargetMode') == 'External':
                    continue
                resolved = posixpath.normpath(posixpath.join(base, relationship.get('Target', ''))).lstrip('/')
                if resolved in renamed:
                    relationship.set('Target', posixpath.relpath(renamed[resolved], base))
                    touched = True
            if touched:
                changed[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        if renamed:
            name = '[Content_Types].xml'
            root = etree.fromstring(incoming.read(name), parser)
            for part in root:
                old = part.get('PartName', '').lstrip('/')
                if old in renamed:
                    part.set('PartName', '/' + renamed[old])
                    part.set('ContentType', 'image/jpeg')
            if not any(p.get('Extension', '').lower() == 'jpg' for p in root):
                etree.SubElement(root, '{http://schemas.openxmlformats.org/package/2006/content-types}Default',
                                 Extension='jpg', ContentType='image/jpeg')
            changed[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            for item in incoming.infolist():
                clone = copy.copy(item)
                clone.filename = renamed.get(item.filename, item.filename)
                clone.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(clone, changed.get(item.filename, incoming.read(item)))
        with zipfile.ZipFile(output) as target:
            if target.testzip() or {renamed.get(n, n) for n in incoming.namelist()} != set(target.namelist()):
                raise ValueError('office_archive_integrity_failed')
            for name in incoming.namelist():
                if name not in changed and incoming.read(name) != target.read(name):
                    raise ValueError('office_non_image_part_changed')
            for name in target.namelist():
                if name.endswith('.xml') or name.endswith('.rels'):
                    root = etree.fromstring(target.read(name), parser)
                    if name.endswith('.rels'):
                        base = str(Path(name).parent.parent)
                        for relation in root:
                            if relation.get('TargetMode') != 'External':
                                resolved = posixpath.normpath(posixpath.join(base, relation.get('Target', ''))).lstrip('/')
                                if resolved not in target.namelist():
                                    raise ValueError('missing_internal_relationship_target')
    return {'validation': 'text_tables_layout_parts_byte_identical',
            'changed_images': sum(name.startswith(prefix) for name in changed),
            'pixel_dimensions_preserved': True, 'visual_review_complete': False}


def pdf(source, output, quality):
    import fitz
    fitz.TOOLS.mupdf_warnings(reset=True)
    with fitz.open(source) as document:
        if document.is_encrypted or document.get_sigflags() > 0:
            raise ValueError('encrypted_or_signed_pdf_not_supported')
        before = [(page.get_text(), tuple(page.rect)) for page in document]
        changed = set()
        for page in document:
            for image in page.get_images(full=True):
                xref = image[0]
                if xref in changed or image[1]:
                    continue
                original = document.extract_image(xref)
                # Flate-encoded PDF images are extracted as PNG. The shared encoder
                # retains pixel dimensions and refuses images with transparency.
                if not original or original.get('ext') not in {'jpeg', 'jpg', 'png'}:
                    continue
                encoded = jpeg(original['image'], quality)
                if encoded != original['image']:
                    page.replace_image(xref, stream=encoded)
                    changed.add(xref)
        document.save(output, garbage=1, deflate=True)
        warnings = fitz.TOOLS.mupdf_warnings(reset=True)
    with fitz.open(source) as original, fitz.open(output) as document:
        after = [(page.get_text(), tuple(page.rect)) for page in document]
        if before != after:
            raise ValueError('pdf_text_or_geometry_changed')
        # Every page must render, including image-only pages where text equality is insufficient.
        differences = []
        fitz.TOOLS.mupdf_warnings(reset=True)
        for first, second in zip(original, document):
            a = first.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            b = second.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            if (a.width, a.height) != (b.width, b.height):
                raise ValueError('pdf_render_dimensions_changed')
            ai = Image.frombytes('RGB', [a.width, a.height], a.samples)
            bi = Image.frombytes('RGB', [b.width, b.height], b.samples)
            difference = sum(ImageStat.Stat(ImageChops.difference(ai, bi)).mean) / 3
            differences.append(difference)
        render_warnings = fitz.TOOLS.mupdf_warnings(reset=True)
        if render_warnings or max(differences, default=0) > 5:
            raise ValueError('pdf_render_validation_failed')
    return {'validation': 'page_text_and_geometry_equal', 'pages': len(before),
            'format_warnings': '', 'compression_warnings': warnings,
            'all_pages_rendered': True, 'max_mean_pixel_difference': max(differences, default=0),
            'changed_images': len(changed), 'visual_review_complete': False}


def compress(source, output):
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError('compression_must_use_separate_output')
    operation = {'.docx': office, '.pptx': office, '.xlsx': office, '.pdf': pdf}.get(source.suffix.lower())
    if operation is None:
        raise ValueError('unsupported_document_format')
    for quality in QUALITIES:
        report = operation(source, output, quality)
        report.update(quality=quality, output_bytes=output.stat().st_size)
        if output.stat().st_size < LIMIT:
            return report
    raise ValueError('target_size_not_reached_without_resizing')
