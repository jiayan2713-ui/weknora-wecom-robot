"""Bounded local PDF rewrite/validation in a separate Python process. No network."""
import argparse
import hashlib
import json
import math
from pathlib import Path


class RepairError(Exception):
    pass


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def repair(source, output, expected_hash, max_pages, max_pixels):
    import fitz
    source,output=Path(source).resolve(),Path(output).resolve()
    if source==output or digest(source)!=expected_hash:
        raise RepairError('SOURCE_IDENTITY_MISMATCH')
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    with fitz.open(source) as original:
        if original.needs_pass or original.is_encrypted:
            raise RepairError('ENCRYPTED_SOURCE')
        if not 0<len(original)<=max_pages:
            raise RepairError('PAGE_LIMIT')
        for page in original:
            rect=page.rect
            if (not all(math.isfinite(v) for v in rect) or rect.width<=0 or rect.height<=0
                    or math.ceil(rect.width*1.5)*math.ceil(rect.height*1.5)>max_pixels):
                raise RepairError('PIXEL_LIMIT')
        original.save(output,garbage=4,deflate=True)
    # Reopen the source: saving may mutate the in-memory MuPDF document.
    with fitz.open(source) as original, fitz.open(output) as compatible:
        if len(original)!=len(compatible):
            raise RepairError('PAGE_COUNT_CHANGED')
        pages=[]
        for number,(before,after) in enumerate(zip(original,compatible),1):
            a=before.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
            b=after.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
            a_hash=hashlib.sha256(a.samples).hexdigest()
            b_hash=hashlib.sha256(b.samples).hexdigest()
            row={'page':number,'same_rect':tuple(before.rect)==tuple(after.rect),
                'same_text':before.get_text()==after.get_text(),
                'same_pixels_108dpi':(a.width,a.height,a.n,a_hash)==(b.width,b.height,b.n,b_hash),
                'original_pixel_sha256':a_hash,'compatible_pixel_sha256':b_hash}
            if not all(row[k] for k in ('same_rect','same_text','same_pixels_108dpi')):
                raise RepairError('PAGE_VALIDATION_CHANGED')
            pages.append(row)
    if digest(source)!=expected_hash:
        raise RepairError('SOURCE_IDENTITY_MISMATCH')
    return {'source_sha256':expected_hash,'compatible_sha256':digest(output),'pages':pages,
            'method':'automatic_lossless_mupdf_rewrite','trigger':'PYPDF_BOOLEAN_READ_ERROR',
            'original_preserved':True,'human_review':False}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--source-hash',required=True)
    parser.add_argument('--max-pages',type=int,required=True)
    parser.add_argument('--max-pixels',type=int,required=True)
    args=parser.parse_args()
    try:
        evidence=repair(args.source,args.output,args.source_hash,args.max_pages,args.max_pixels)
        evidence['status']='passed'
    except ImportError:
        evidence={'status':'failed','reason_code':'DEPENDENCY_UNAVAILABLE'}
    except RepairError as error:
        evidence={'status':'failed','reason_code':str(error)}
    except Exception as error:
        evidence={'status':'failed','reason_code':'REWRITE_ERROR','exception_type':type(error).__name__}
    args.evidence.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    return 0 if evidence['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
