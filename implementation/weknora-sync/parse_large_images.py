"""Isolated full-resolution image partitioning for the existing docflow parser."""
import hashlib
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys

from reconcile import PROJECT, SOURCE, MARKDOWN, hashes, save

sys.path.insert(0, str(PROJECT))
from PIL import Image, ImageOps
from docflow import parser as parser_module
from docflow.common import digest, write_json
from docflow.common import WorkflowError
from docflow.config import DEFAULTS
from docflow.runner import Workflow

ROOT = PROJECT / 'outputs/机器人5三方对账_20261010'


def prepare_regions(source, work, config, **kwargs):
    if source.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise ValueError('image_only_partition_adapter')
    work.mkdir(parents=True, exist_ok=True)
    units = []
    with Image.open(source) as raw:
        if raw.width*raw.height>40000000:
            raise WorkflowError('CAPACITY_EXCEEDED','原图像素超过4000万，保留原件并暂停')
        image = ImageOps.exif_transpose(raw).convert('RGB')
        # Preserve pixels, with 32px overlap and deterministic reading order.
        for y in range(0, image.height, 2168):
            for x in range(0, image.width, 2168):
                from docflow.runtime import check_stop
                check_stop()
                box = (x, y, min(x + 2200, image.width), min(y + 2200, image.height))
                path = work / ('region-' + '-'.join(map(str, box)) + '.png')
                image.crop(box).save(path)
                if path.stat().st_size > 8 * 1024 * 1024:
                    from docflow.documents import page_image_units
                    parts = page_image_units(path, '图片1；像素区域' + str(box), '')
                else:
                    parts = [{'label': '图片1；像素区域' + str(box),
                              'text': '', 'source_location': {'pixel_box': list(box), 'source_format': source.suffix.lower()},
                              'image': path.name}]
                units.extend(parts)
        dimensions = [image.width, image.height]
    for number, unit in enumerate(units, 1):
        unit['id'] = f'u{number:04}'
    write_json(work / 'prepared.json', {'source_hash': digest(source), 'version': 'robot5-image-regions-v1',
        'units': units, 'native': {'pixel_dimensions': dimensions, 'overlap': 32}})
    return units


def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument('--prepare-only', action='store_true')
    arguments.add_argument('--retry-errors', action='store_true')
    args = arguments.parse_args()
    if args.prepare_only:
        gaps = json.loads((ROOT / 'remaining-source-gaps.json').read_text())
        source_index = {r['relative']: r for r in json.loads((ROOT / 'sources.json').read_text())}
        plans = []
        for gap in gaps:
            if gap['category'] != '解析未完整':
                continue
            relative = gap['source_relative']
            source = SOURCE / relative
            expected = source_index[relative]['sha256']
            if hashes(source)['sha256'] != expected:
                plans.append({'source_relative': relative, 'status': 'source_changed_hold'})
                continue
            identity = hashlib.sha256((relative + expected + 'regions-v1').encode()).hexdigest()
            units = prepare_regions(source, ROOT / 'image-regions' / identity, DEFAULTS)
            plans.append({'source_relative': relative, 'source_sha256': expected,
                          'regions': len(units), 'status': 'prepared_local_only'})
            save(ROOT / 'image-region-plan.json', plans)
        print('Local image partitions prepared:', len(plans), 'files,', sum(p.get('regions', 0) for p in plans), 'regions')
        return
    for package in ('Pillow', 'pypdf', 'openpyxl', 'python-pptx'):
        importlib.metadata.version(package)
    config = json.loads((PROJECT / '.local/wecom-sync/project-consultation-local-20260929/local-watch.json').read_text())
    secret_path = Path(config['secrets_file'])
    if secret_path.stat().st_mode & 0o077:
        raise ValueError('secret_file_permissions')
    credentials = json.loads(secret_path.read_text())
    if set(credentials) != {'DASHSCOPE_API_KEY'}:
        raise ValueError('unexpected_secret_fields')
    os.environ.update(credentials)
    parser_module.prepare = prepare_regions
    parser_module.VERSION += '-robot5-image-regions-v1'
    workflow = Workflow(ROOT / 'image-docflow')
    gaps = json.loads((ROOT / 'remaining-source-gaps.json').read_text())
    source_index = {r['relative']: r for r in json.loads((ROOT / 'sources.json').read_text())}
    log_path = ROOT / 'image-parse-results.json'
    results = json.loads(log_path.read_text()) if log_path.exists() else []
    if args.retry_errors:
        history_path = ROOT / 'image-parse-attempt-history.json'
        history = json.loads(history_path.read_text()) if history_path.exists() else []
        history.extend(r for r in results if r['status'] != 'recovered')
        save(history_path, history)
        results = [r for r in results if r['status'] == 'recovered']
    done = {r['source_relative'] for r in results}
    for gap in gaps:
        if gap['category'] != '解析未完整' or gap['source_relative'] in done:
            continue
        relative = gap['source_relative']
        source = SOURCE / relative
        expected_hash = source_index[relative]['sha256']
        if hashes(source)['sha256'] != expected_hash:
            results.append({'source_relative': relative, 'status': 'source_changed_hold'})
            save(log_path, results)
            continue
        identity = hashlib.sha256((relative + expected_hash + 'regions-v1').encode()).hexdigest()
        snapshot = ROOT / 'image-snapshots' / identity / source.name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            shutil.copy2(source, snapshot)
        if hashes(snapshot)['sha256'] != expected_hash:
            raise ValueError('snapshot_hash_mismatch')
        print('Parsing image', len(results) + 1, relative, flush=True)
        request = {'request_id': 'robot5-regions-' + identity, 'project': '机器人5来源分区解析',
                   'sources': [str(snapshot)], 'config': {k: v for k, v in DEFAULTS.items()}}
        try:
            try:
                response = workflow.execute('start', request)
            except WorkflowError as error:
                if error.code != 'REQUEST_INTERRUPTED':
                    raise
                previous = workflow.store.read('requests/' + request['request_id'] + '.json')
                status = workflow.status(previous['batch_id'])
                response = workflow.execute('resume', {'request_id': request['request_id'] + '-resume-' + str(status['revision']),
                    'batch_id': status['batch_id'], 'expected_revision': status['revision'], 'mode': 'continue', 'retry_failed': True})
            status = workflow.status(response['batch_id'])
            if args.retry_errors and status['phase'] == 'processing' and status.get('errors'):
                response = workflow.execute('resume', {'request_id': request['request_id'] + '-retry-' + str(status['revision']),
                    'batch_id': status['batch_id'], 'expected_revision': status['revision'], 'mode': 'continue', 'retry_failed': True})
                status = workflow.status(response['batch_id'])
            if status['phase'] != 'awaiting_review' or status.get('errors'):
                results.append({'source_relative': relative, 'status': 'parse_failed',
                                'batch_id': status.get('batch_id'), 'errors': status.get('errors')})
            else:
                doc = status['documents'][0]
                working = Path(doc['working'])
                text = working.read_text()
                destination = MARKDOWN / (relative + '.md')
                if hashes(source)['sha256'] != expected_hash:
                    raise ValueError('source_changed_during_parse')
                if destination.exists() and destination.read_text() != text:
                    raise ValueError('destination_created_by_another_process')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text)
                row = {'source_relative': relative, 'source_sha256': expected_hash,
                    'record_id': 'robot5-' + identity[:16], 'status': 'recovered',
                    'markdown_sha256': hashes(destination)['sha256'], 'destination': str(destination),
                    'batch_id': status['batch_id'], 'parser_model': DEFAULTS['model'],
                    'human_review': False, 'partition_version': 'robot5-image-regions-v1'}
                results.append(row)
                recovered_path = ROOT / 'recovered-markdown.json'
                recovered = json.loads(recovered_path.read_text())
                recovered.append(row)
                save(recovered_path, recovered)
                stage = ROOT / '补齐上传包/项目to咨询' / (relative + '.md')
                stage.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, stage)
        except Exception as error:
            results.append({'source_relative': relative, 'status': 'parse_error', 'error': type(error).__name__})
        save(log_path, results)
    print('Image results', len(results), flush=True)


if __name__ == '__main__':
    main()
