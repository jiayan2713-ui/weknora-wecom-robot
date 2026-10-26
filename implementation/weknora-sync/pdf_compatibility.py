"""Use an explicitly verified derivative without changing source identity."""
import json
import datetime as dt
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from reconcile import hashes
from parse_policy import ParseFailure


def _pdf_readable(path):
    from pypdf import PdfReader
    reader=PdfReader(path)
    if reader.is_encrypted:
        return True  # Preserve the existing encrypted-document path; never decrypt here.
    len(reader.pages)
    return True


def automatic_compatible_pdf(cfg, source, source_hash, work):
    try:
        return _automatic_compatible_pdf(cfg,source,source_hash,work)
    except ParseFailure:
        raise
    except Exception as error:
        code='PDF_COMPATIBILITY_ENVIRONMENT' if isinstance(error,OSError) else 'PDF_COMPATIBILITY_FAILED'
        reason='LOCAL_STORAGE_UNAVAILABLE' if isinstance(error,OSError) else 'REPAIR_COORDINATION_FAILED'
        path=Path(work)/'pdf-compatibility.json'
        path.write_text(json.dumps({'status':'failed','code':code,'reason_code':reason,'source_hash':source_hash,
            'exception_type':type(error).__name__,'original_preserved':True,'human_review':False},ensure_ascii=False))
        path.chmod(0o600)
        raise ParseFailure(code,'environment' if isinstance(error,OSError) else 'document',False,
                           '保留原件和旧版本；查看PDF兼容验证报告后定向重试') from error


def _automatic_compatible_pdf(cfg, source, source_hash, work):
    """Only a known Boolean-reader failure may create a verified derivative."""
    source,work=Path(source),Path(work)
    attempt=work/'pdf-compatibility.json'
    def record(value):
        value={**value,'source_hash':source_hash,'time_beijing':dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
               'original_preserved':True,'human_review':False}
        attempt.write_text(json.dumps(value,ensure_ascii=False,indent=2))
        attempt.chmod(0o600)
    def stop(code,reason):
        record({'status':'failed','code':code,'reason_code':reason})
        raise ParseFailure(code,'environment' if code.endswith('ENVIRONMENT') else 'document',False,
            '保留原件和旧版本；查看PDF兼容验证报告，修复环境或原件后定向重试')
    try:
        existing,evidence=compatible_pdf(cfg,source_hash)
    except Exception:
        stop('PDF_COMPATIBILITY_VALIDATION_FAILED','REGISTERED_DERIVATIVE_INVALID')
    if existing:
        record({'status':'reused','compatible_sha256':evidence['compatible_sha256'],'pages_verified':len(evidence['pages'])})
        return existing,evidence
    if cfg.get('pdf_auto_compatibility_enabled',True) is False:
        return None,None
    from pypdf.errors import PdfReadError
    try:
        _pdf_readable(source)
        return None,None
    except PdfReadError as error:
        if 'could not read boolean object' not in str(error).lower():
            return None,None
    except Exception:
        return None,None
    if not cfg.get('state_dir'):
        stop('PDF_COMPATIBILITY_ENVIRONMENT','STATE_DIRECTORY_REQUIRED')
    limits={'max_bytes':cfg.get('pdf_compatibility_max_bytes',100*1024*1024),
            'max_pages':cfg.get('pdf_compatibility_max_pages',100),
            'max_pixels':cfg.get('pdf_compatibility_max_pixels',16000000),
            'timeout':cfg.get('pdf_compatibility_timeout_seconds',120)}
    if any(type(v) is not int or v<=0 for v in limits.values()):
        stop('PDF_COMPATIBILITY_ENVIRONMENT','INVALID_LIMIT_CONFIGURATION')
    if source.stat().st_size>limits['max_bytes']:
        stop('PDF_COMPATIBILITY_LIMIT','SOURCE_SIZE_LIMIT')
    if hashes(source)['sha256']!=source_hash:
        stop('PDF_COMPATIBILITY_VALIDATION_FAILED','SOURCE_IDENTITY_MISMATCH')
    root=Path(cfg['state_dir'])/'pdf-compatibility'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=root/(source_hash+'.lock')
    with lock_path.open('a') as lock:
        os.chmod(lock_path,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            stop('PDF_COMPATIBILITY_FAILED','REPAIR_ALREADY_RUNNING')
        existing,evidence=compatible_pdf(cfg,source_hash)
        if existing:
            record({'status':'reused','compatible_sha256':evidence['compatible_sha256'],'pages_verified':len(evidence['pages'])})
            return existing,evidence
        target=root/source_hash
        if target.exists():
            stop('PDF_COMPATIBILITY_VALIDATION_FAILED','INCOMPLETE_REGISTRY_ENTRY')
        stage=Path(tempfile.mkdtemp(prefix='.'+source_hash+'-',dir=root))
        try:
            env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG','LC_ALL','SYSTEMROOT') if k in os.environ}
            env['PYTHONDONTWRITEBYTECODE']='1'
            command=[cfg.get('pdf_compatibility_python') or sys.executable,
                str(Path(__file__).with_name('pdf_compatibility_worker.py')),'--source',str(source),
                '--output',str(stage/'compatible.pdf'),'--evidence',str(stage/'worker-result.json'),
                '--source-hash',source_hash,'--max-pages',str(limits['max_pages']),'--max-pixels',str(limits['max_pixels'])]
            record({'status':'repair_started','trigger':'PYPDF_BOOLEAN_READ_ERROR'})
            try:
                result=subprocess.run(command,capture_output=True,timeout=limits['timeout'],env=env)
            except subprocess.TimeoutExpired:
                stop('PDF_COMPATIBILITY_LIMIT','REPAIR_TIMEOUT')
            except OSError:
                stop('PDF_COMPATIBILITY_ENVIRONMENT','REPAIR_RUNTIME_UNAVAILABLE')
            result_path=stage/'worker-result.json'
            try:evidence=json.loads(result_path.read_text())
            except (OSError,ValueError):
                stop('PDF_COMPATIBILITY_FAILED','REPAIR_WORKER_INCOMPLETE')
            if result.returncode or evidence.get('status')!='passed':
                reason=evidence.get('reason_code')
                code=('PDF_COMPATIBILITY_ENVIRONMENT' if reason=='DEPENDENCY_UNAVAILABLE' else
                      'PDF_COMPATIBILITY_LIMIT' if reason in {'PAGE_LIMIT','PIXEL_LIMIT'} else
                      'PDF_COMPATIBILITY_VALIDATION_FAILED' if reason in {'PAGE_VALIDATION_CHANGED','PAGE_COUNT_CHANGED','SOURCE_IDENTITY_MISMATCH'}
                      else 'PDF_COMPATIBILITY_FAILED')
                stop(code,reason if reason in {'DEPENDENCY_UNAVAILABLE','PAGE_LIMIT','PIXEL_LIMIT','PAGE_VALIDATION_CHANGED',
                     'PAGE_COUNT_CHANGED','SOURCE_IDENTITY_MISMATCH','ENCRYPTED_SOURCE','REWRITE_ERROR'} else 'REPAIR_WORKER_FAILED')
            if hashes(source)['sha256']!=source_hash:
                stop('PDF_COMPATIBILITY_VALIDATION_FAILED','SOURCE_IDENTITY_MISMATCH')
            (stage/'manifest.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
            for path in stage.iterdir():path.chmod(0o600)
            # Check pypdf readability before atomically publishing the registry entry.
            from pypdf import PdfReader
            try:
                reader=PdfReader(stage/'compatible.pdf')
                if reader.is_encrypted or len(reader.pages)!=len(evidence.get('pages',[])):
                    raise ValueError('page_count_mismatch')
            except Exception:
                stop('PDF_COMPATIBILITY_VALIDATION_FAILED','REPAIRED_PDF_UNREADABLE')
            if evidence.get('source_sha256')!=source_hash or hashes(stage/'compatible.pdf')['sha256']!=evidence.get('compatible_sha256'):
                stop('PDF_COMPATIBILITY_VALIDATION_FAILED','DERIVATIVE_FINGERPRINT_MISMATCH')
            rows=evidence.get('pages',[])
            if not rows or any(row.get('page')!=i+1 or not all(row.get(k) is True for k in
                    ('same_rect','same_text','same_pixels_108dpi')) for i,row in enumerate(rows)):
                stop('PDF_COMPATIBILITY_VALIDATION_FAILED','PAGE_VALIDATION_CHANGED')
            os.rename(stage,target)
            existing,evidence=compatible_pdf(cfg,source_hash)
            record({'status':'repaired','compatible_sha256':evidence['compatible_sha256'],'pages_verified':len(evidence['pages']),
                    'trigger':'PYPDF_BOOLEAN_READ_ERROR'})
            return existing,evidence
        finally:
            if stage.exists():shutil.rmtree(stage)


def compatible_pdf(cfg, source_hash):
    if not cfg.get('state_dir'):
        return None, None
    root = Path(cfg['state_dir']) / 'pdf-compatibility' / source_hash
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        return None, None
    evidence = json.loads(manifest.read_text())
    pdf = root / 'compatible.pdf'
    if (evidence.get('source_sha256') != source_hash or pdf.is_symlink()
            or hashes(pdf)['sha256'] != evidence.get('compatible_sha256')):
        raise ValueError('pdf_compatibility_fingerprint_mismatch')
    rows = evidence.get('pages', [])
    if not rows or any(row.get('page') != i + 1 or not all(
            row.get(k) is True for k in ('same_rect', 'same_pixels_108dpi', 'same_text'))
            for i, row in enumerate(rows)):
        raise ValueError('pdf_compatibility_coverage_mismatch')
    from pypdf import PdfReader
    reader = PdfReader(pdf)
    if reader.is_encrypted or len(reader.pages) != len(rows):
        raise ValueError('pdf_compatibility_page_mismatch')
    return pdf, evidence


def migration_retries(cfg, records):
    """Validate registered derivatives and request only matching paused PDFs."""
    requests = []
    for identity, record in records.items():
        if record.get('state') == 'deleted' or Path(record['path']).suffix.lower() != '.pdf':
            continue
        digest = record.get('blocked_source_hash') or record.get('source_hash')
        if not digest:
            continue
        pdf, _ = compatible_pdf(cfg, digest)
        if (pdf and record.get('state') == 'parse_blocked_hold'
                and record.get('blocked_reason') == 'INVALID_DOCUMENT'
                and not record.get('pending')):
            requests.append({'source_id': identity, 'source_hash': digest,
                             'reason': 'verified_pdf_compatibility_migration'})
    return requests
