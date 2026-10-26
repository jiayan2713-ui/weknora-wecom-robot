"""Produce independent Markdown from the configured local source, never approve drafts."""
import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import sys

from reconcile import PROJECT, hashes, save
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from parse_policy import ParseFailure, classify, report_summary, VERSION
from markdown_assets import has_image_reference
from parse_quality import summarize_units


def artifact_report(source_hash, method, *, reused=False):
    return {'schema_version': 1, 'policy_version': VERSION, 'source_hash': source_hash,
            'method': method, 'status': 'complete_with_warnings' if reused else 'complete',
            'coverage_scope': 'artifact_only', 'expected_units': None, 'completed_units': None,
            'warnings': ['复用已核验产物，未重新检查原件页、表或分区覆盖'] if reused else [],
            'human_review': False}


def publish_report(work, report):
    report['policy_version'] = VERSION
    path = work / 'parse-report.json'
    save(path, report)
    return str(path)


def workflow_failure(status, batch, *, allow_processing=False):
    errors = [issue for doc in batch.get('documents', []) for issue in doc.get('issues', [])
              if isinstance(issue, dict) and issue.get('code')]
    errors += status.get('errors', [])
    if errors:
        return classify(errors[0])
    if status['phase'] != 'awaiting_review' and not (allow_processing and status['phase'] == 'processing'):
        return classify({'code': 'PARSE_INCOMPLETE'})
    return None


def prepared_units(workflow, doc, manifest=None):
    from docflow.common import read_json, verify
    reference = doc.get('prepared_ref')
    if not reference and manifest:
        reference = next((r for r in manifest.get('artifacts', [])
                          if r.get('path', '').endswith('/prepared-index.json')), None)
    if not reference:
        raise ParseFailure('UNIT_COVERAGE_INCOMPLETE', 'quality', False, '准备单元清单缺失，检查缓存后定向重试')
    return read_json(verify(workflow.store.root, reference))['units']


def checkpoint_report(workflow, batch, source_hash):
    from docflow.common import read_json, verify
    doc = batch.get('documents', [{}])[0]
    if not doc.get('prepared_ref'):
        return {'schema_version': 1, 'source_hash': source_hash, 'method': 'docflow_llm',
                'status': 'incomplete', 'coverage_scope': 'not_prepared',
                'expected_units': None, 'completed_units': 0, 'warnings': [], 'human_review': False}
    expected = prepared_units(workflow, doc)
    results = [read_json(verify(workflow.store.root, ref)) for ref in doc.get('unit_refs', {}).values()]
    return summarize_units(expected, results, source_hash)


def candidate_markdown(relative, source_hash):
    marker_path = PROJECT / 'outputs/当前解析结果/.managed-results.json'
    state_path = PROJECT / '.local/wecom-sync/project-consultation-local-20260929/live/state.json'
    if not marker_path.exists() or not state_path.exists():
        return None
    marker = json.loads(marker_path.read_text())
    state = json.loads(state_path.read_text())
    record = next((r for r in state['records'].values() if relative in r.get('paths', [])), None)
    if not record:
        return None
    entry = marker['files'].get('项目to咨询/Markdown/' + relative + '.md')
    paths = []
    if entry and record.get('hash') == source_hash:
        paths.append(Path(entry['source']))
    pending = record.get('pending', {})
    if pending.get('hash') == source_hash and pending.get('key'):
        job = state_path.parent / 'jobs' / pending['key']
        paths += list(job.rglob('publication.md'))
    for path in paths:
        trace = path.parent / 'source-trace.json'
        if trace.exists():
            provenance = json.loads(trace.read_text()).get('parser_provenance', {})
            if provenance.get('source_hash') != source_hash:
                continue
        elif not entry or hashes(path)['sha256'] != entry['sha256']:
            continue
        text = path.read_text()
        if text.strip() and not has_image_reference(text):
            return path
    return None


def render(cfg, source, relative, source_hash, work):
    try:
        if cfg.get('compression_registry'):
            from compression_versions import parsing_source
            original, original_hash, receipt = parsing_source(cfg, source, relative, source_hash)
            if receipt:
                original_input = work / 'compression-original' / Path(relative).name
                original_input.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, original_input)
                if hashes(original_input)['sha256'] != original_hash:
                    raise ValueError('compression_original_snapshot_mismatch')
                try:
                    output, provenance = render_document({**cfg,'_logical_source_hash':source_hash}, original_input, relative, original_hash, work)
                except Exception as error:
                    error.parsing_original_hash=original_hash
                    raise
                provenance.update(source_hash=source_hash, parsing_original_sha256=original_hash, compression=receipt)
                if provenance.get('parse_report_path'):
                    report=json.loads(Path(provenance['parse_report_path']).read_text())
                    report.update(source_hash=source_hash, original_source_hash=original_hash, logical_source_hash=source_hash)
                    save(Path(provenance['parse_report_path']),report)
                    provenance['parse_report']=report_summary(report)
                return output, provenance
        return render_document(cfg, source, relative, source_hash, work)
    except Exception as error:
        failure = classify(error)
        if failure.code == 'RETRY_AFTER':
            deadlines = []
            for checkpoint in work.rglob('call.json'):
                try:
                    deadline = json.loads(checkpoint.read_text()).get('retry_at')
                    if type(deadline) in (int, float) and math.isfinite(deadline):
                        deadlines.append(deadline)
                except (OSError, ValueError):
                    continue
            failure.retry_at = max(deadlines, default=None)
        path = work / 'parse-report.json'
        report = {
            'schema_version': 1, 'source_hash': source_hash, 'method': 'not_completed',
            'coverage_scope': 'unknown', 'expected_units': None, 'completed_units': None,
            'warnings': [], 'human_review': False}
        if path.exists():
            try:
                previous = json.loads(path.read_text())
                if isinstance(previous, dict) and previous.get('source_hash') in (source_hash,getattr(error,'parsing_original_hash',source_hash)):
                    report = previous
            except (OSError, ValueError):
                pass
        compatibility=work/'pdf-compatibility.json'
        if compatibility.is_file():
            try:report['pdf_compatibility']=json.loads(compatibility.read_text())
            except (OSError,ValueError):pass
        if getattr(error,'parsing_original_hash',None):report.update(source_hash=source_hash,original_source_hash=error.parsing_original_hash)
        report['runtime'] = getattr(error, 'parse_runtime', {})
        report.update(status='no_readable_text' if failure.category == 'no_text' else 'incomplete',
                      failure=failure.record())
        try:
            failure.report_path = publish_report(work, report)
        except OSError:
            raise ParseFailure('PARSE_REPORT_STORAGE_FAILED', 'environment', False,
                               '完整性报告保存失败，检查磁盘和权限后定向重试') from error
        if failure is error:
            raise
        raise failure from error


def clean_pdf_placeholders(units):
    """Only remove the model's literal dummy image URI; retain every caption."""
    replacements=[]
    for unit in units:
        body=unit.get('body','')
        if not isinstance(body,str):continue
        pattern=r'!\[([^\]\n]*)\]\(image_placeholder\)'
        fixed,count=re.subn(pattern,lambda match:match.group(1),body)
        if count:
            unit['body']=fixed
            unit['issues']=[*unit.get('issues',[]),'模型生成的无效图片占位已保留为文字标签；未人工审核']
            replacements.append({'unit_id':unit['id'],'removed_dummy_image_links':count})
    return units,replacements


def render_document(cfg, source, relative, source_hash, work):
    from docflow.runtime import session
    with session(cfg.get('state_dir', Path(work).parent), source_hash, cfg.get('parse_limits')) as context:
        try:
            output, provenance = _render_document(cfg, source, relative, source_hash, work)
        except Exception as error:
            error.parse_runtime = context.summary()
            raise
        provenance['parse_runtime'] = context.summary()
        report_path = provenance.get('parse_report_path')
        if report_path:
            report = json.loads(Path(report_path).read_text())
            report['runtime'] = provenance['parse_runtime']
            save(Path(report_path), report)
        return output, provenance


def _render_document(cfg, source, relative, source_hash, work):
    work.mkdir(parents=True, exist_ok=True)
    output = work / 'publication.md'
    candidate = candidate_markdown(relative, source_hash) if cfg.get('reuse_robot1_artifacts', True) and not cfg.get('_xlsx_checkpoint_work') and not cfg.get('_pdf_checkpoint_work') and not cfg.get('_source_checkpoint_work') else None
    if candidate:
        shutil.copy2(candidate, output)
        report = artifact_report(source_hash, 'reuse_verified_source_artifact', reused=True)
        return output, {'method': 'reuse_verified_source_artifact', 'source_hash': source_hash,
                        'reused_path': str(candidate), 'human_review': False,
                        'parse_report': report_summary(report), 'parse_report_path': publish_report(work, report)}
    if source.suffix.lower() in ('.md', '.markdown') or not cfg.get('llm_enabled', True):
        if source.suffix.lower() not in ('.txt', '.md', '.markdown', '.csv', '.tsv'):
            raise ValueError('direct_local_text_format_unsupported')
        text = source.read_text(encoding='utf-8-sig')
        if not text.strip() or '\x00' in text:
            raise ValueError('empty_or_binary_text')
        output.write_text('# ' + source.name + '\n\n' + text)
        report = artifact_report(source_hash, 'local_text')
        return output, {'method': 'local_text', 'source_hash': source_hash, 'human_review': False,
                        'parse_report': report_summary(report), 'parse_report_path': publish_report(work, report)}
    if not cfg.get('allow_model_calls', False):
        raise ValueError('model_authorization_required_hold')
    from docflow.common import WorkflowError
    from docflow.config import configuration, DEFAULTS
    from docflow.runner import Workflow
    secret = Path(cfg['model_secrets_file'])
    if secret.stat().st_mode & 0o077:
        raise ValueError('model_secret_file_permissions')
    secrets = json.loads(secret.read_text())
    if set(secrets) != {'DASHSCOPE_API_KEY'}:
        raise ValueError('unexpected_model_secret_fields')
    os.environ.update(secrets)
    values = dict(cfg.get('parser_config', {}))
    config = configuration(values)
    snapshot = work / source.name
    shutil.copy2(source, snapshot)
    if hashes(snapshot)['sha256'] != source_hash:
        raise ValueError('snapshot_fingerprint_mismatch')
    original_snapshot = snapshot
    pdf_compatibility = None
    if snapshot.suffix.lower() == '.pdf':
        from pdf_compatibility import automatic_compatible_pdf
        compatible, pdf_compatibility = automatic_compatible_pdf(cfg, snapshot, source_hash, work)
        if compatible:
            snapshot = work / 'pdf-compatible' / source.name
            snapshot.parent.mkdir(exist_ok=True)
            shutil.copy2(compatible, snapshot)
            if hashes(snapshot)['sha256'] != pdf_compatibility['compatible_sha256']:
                raise ValueError('pdf_compatibility_snapshot_mismatch')
    compaction = None
    if snapshot.suffix.lower() == '.xlsx':
        import zipfile
        with zipfile.ZipFile(snapshot) as archive:
            entries = archive.infolist()
            oversized = (sum(e.file_size for e in entries if e.filename.endswith(('.xml', '.rels'))) > 64 * 1024 * 1024
                or sum(e.file_size for e in entries if e.filename.startswith('xl/worksheets/')) > 32 * 1024 * 1024)
        if oversized:
            from compact_xlsx import compact
            compacted = work / 'compacted' / source.name
            compaction = compact(snapshot, compacted, work / 'xlsx-compaction.json')
            snapshot = compacted
    xlsx_style_compatibility = None
    if snapshot.suffix.lower() == '.xlsx':
        from xlsx_style_compatibility import compatible_copy
        compatible, xlsx_style_compatibility = compatible_copy(snapshot, work)
        if compatible:
            snapshot = compatible
    if snapshot.suffix.lower() in ('.doc', '.ppt', '.xls'):
        from docflow.documents import office_profile, run
        converted = work / 'converted'
        converted.mkdir(exist_ok=True)
        extension = {'.doc': 'docx', '.ppt': 'pptx', '.xls': 'xlsx'}[snapshot.suffix.lower()]
        profile, env = office_profile(converted)
        run([config['soffice'], '-env:UserInstallation=' + profile.as_uri(), '--headless',
             '--convert-to', extension, '--outdir', str(converted), str(snapshot)],
            config['conversion_timeout_seconds'], env=env, network_denied=True)
        snapshot = converted / (snapshot.stem + '.' + extension)
        if not snapshot.exists():
            raise ValueError('legacy_office_conversion_missing')
    from docflow import parser as parser_module
    from parse_large_images import prepare_regions
    original_prepare = parser_module.prepare

    def safe_prepare(path, directory, options, **kwargs):
        if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
            # Preserve full image pixels for consistent region evidence.
            return prepare_regions(path, directory, options, **kwargs)
        return original_prepare(path, directory, options, **kwargs)

    parser_module.VERSION = 'robot5-unified-controller-v1'
    workflow = Workflow(work / 'docflow')
    prior_checkpoint = None
    if source.suffix.lower() == '.xlsx':
        from xlsx_recovery import checkpoint
        prior_checkpoint = checkpoint(cfg, source, source_hash)
    elif cfg.get('_source_checkpoint_work') or source.suffix.lower()=='.pdf' and cfg.get('_pdf_checkpoint_work'):
        from parse_checkpoint import checkpoint
        prior_checkpoint=checkpoint(cfg,source,source_hash)
        if not prior_checkpoint:
            raise ValueError('pdf_parse_checkpoint_unavailable')
        prior_work=prior_checkpoint[0].store.root.parent
        if (prior_work/'body-fixes').is_dir() and not (work/'body-fixes').exists():
            shutil.copytree(prior_work/'body-fixes',work/'body-fixes')
        requested_work = Path(cfg.get('_source_checkpoint_work') or prior_work).resolve()
        if (cfg.get('_source_checkpoint_work') and requested_work != prior_work
                and (requested_work/'source-recovery').is_dir() and not (work/'source-recovery').exists()):
            shutil.copytree(requested_work/'source-recovery', work/'source-recovery')
    identity = hashlib.sha256((relative + source_hash + json.dumps(values, sort_keys=True)).encode()).hexdigest()
    if compaction:
        identity = hashlib.sha256((identity + compaction['output_sha256']).encode()).hexdigest()
    request = {'request_id': 'robot5-' + identity, 'project': '机器人5本地原件同步',
               'sources': [str(snapshot)], 'config': {k: v for k, v in config.items() if k in DEFAULTS}}
    parser_module.prepare = safe_prepare
    try:
        if prior_checkpoint:
            workflow, prior_status, _ = prior_checkpoint
            response = {'batch_id': prior_status['batch_id']}
        else:
            response = workflow.execute('start', request)
    except WorkflowError as error:
        if error.code != 'REQUEST_INTERRUPTED':
            raise
        previous = workflow.store.read('requests/' + request['request_id'] + '.json')
        response = {'batch_id': previous['batch_id']}
    finally:
        parser_module.prepare = original_prepare
    status = workflow.status(response['batch_id'])
    batch = workflow.store.batch(status['batch_id'])
    failure = workflow_failure(status, batch, allow_processing=True)
    if failure and not failure.retryable:
        publish_report(work, checkpoint_report(workflow, batch, source_hash))
        raise failure
    if status['phase'] == 'processing':
        parser_module.prepare = safe_prepare
        try:
            workflow.execute('resume', {'request_id': request['request_id'] + '-r' + str(status['revision']),
                'batch_id': status['batch_id'], 'expected_revision': status['revision'],
                'mode': 'continue', 'retry_failed': True})
        finally:
            parser_module.prepare = original_prepare
        status = workflow.status(status['batch_id'])
    batch = workflow.store.batch(status['batch_id'])
    failure = workflow_failure(status, batch)
    if failure:
        publish_report(work, checkpoint_report(workflow, batch, source_hash))
        raise failure
    document_id = status['documents'][0]['id']
    batch = workflow.store.batch(status['batch_id'])
    doc = next(d for d in batch['documents'] if d['id'] == document_id)
    from docflow.parser import verify_cache
    cache = workflow.store.path(doc['cache_ref']['path'])
    manifest = verify_cache(workflow.store, doc['cache_ref'])
    units = manifest['units']
    expected = prepared_units(workflow, doc, manifest)
    initial_report = summarize_units(expected, units, source_hash)
    if prior_checkpoint:
        initial_report['checkpoint_work'] = str(workflow.store.root.parent)
    publish_report(work, initial_report)
    from docflow.model import ModelClient
    from docflow.parser import prompt_blocks, split_notes, markdown
    client = ModelClient(config)
    prompt = Path(config['parser_prompt']).read_text()
    import copy
    units = copy.deepcopy(units)
    placeholder_changes=[]
    if source.suffix.lower()=='.pdf':
        units, placeholder_changes=clean_pdf_placeholders(units)
    from parse_control import run as control_units
    expected, units, report = control_units({**cfg,'_parse_original':str(original_snapshot), '_parse_input':str(snapshot), '_pdf_proof':pdf_compatibility}, workflow, doc, cache, expected, units,
        source_hash, hashes(snapshot)['sha256'], work, config, client, prompt)
    if placeholder_changes:
        report['image_placeholder_cleanup']=placeholder_changes
    if prior_checkpoint:
        report['checkpoint_work'] = str(workflow.store.root.parent)
    # Old report/result names remain readable by existing trace and checkpoint tools.
    save(work/'source-recovery-results.json', {'source_hash': source_hash, 'units': units, 'human_review': False})
    publish_report(work, report)
    if report['status'] == 'no_readable_text':
        raise classify({'code': 'NO_READABLE_TEXT'})
    if report['status'] == 'incomplete':
        raise classify({'code': 'UNIT_COVERAGE_INCOMPLETE'})
    text = markdown(doc, {'units': units})
    if source.suffix.lower() == '.xlsx':
        from docflow.wps_checkboxes import verify_publication
        controls = verify_publication(original_snapshot, text)
        if controls:
            report['wps_checkbox_validation'] = controls
            publish_report(work, report)
    if has_image_reference(text):
        raise ValueError('image_assets_hold')
    output.write_text(text)
    return output, {'method': 'docflow_llm', 'model': config['model'], 'batch_id': status['batch_id'],
        'source_hash': source_hash, 'parser_input_hash': hashes(snapshot)['sha256'],
        'original_snapshot': str(original_snapshot), 'xlsx_compaction': compaction,
        'pdf_compatibility': pdf_compatibility, 'xlsx_style_compatibility': xlsx_style_compatibility, 'human_review': False,
        'parse_report': report_summary(report), 'parse_report_path': str(work / 'parse-report.json')}
