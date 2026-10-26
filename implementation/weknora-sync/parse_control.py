"""Same evidence-driven recovery controller for first parse and checkpoint replay."""
import copy
import hashlib
from pathlib import Path
import re

from docflow.common import WorkflowError, read_json, verify, digest, write_json
from docflow.runtime import current_session, register_plan, check_stop, recovery_action
from parse_artifacts import Artifacts
from parse_quality import summarize_units
from source_recovery import recover_units, NO_TEXT, MISSING
from response_adapter import response_parts


VERSION = 'robot5-parse-control-v1'


def assessment(unit):
    from markdown_assets import has_image_reference
    if unit.get('method') in ('native_text','native_cells') and unit.get('body','').strip():return 'text_present'
    body, _ = response_parts(unit.get('body', ''))
    if unit.get('label') == '转换处理记录（非原文）':
        return 'processing_record'
    if unit.get('content_status') == 'blank_proven' or '已检测空白单元，待人工确认' in unit.get('issues', []):
        return 'blank_proven'
    if has_image_reference(body):
        return 'format_invalid'
    if not body or body == '[[DOCFLOW_NO_TEXT]]':
        return 'no_text_claimed' if NO_TEXT in unit.get('issues', []) or body == '[[DOCFLOW_NO_TEXT]]' else 'missing_body'
    if re.sub(r'[\s\[\]【】#*：:。.,，!?！？-]', '', body) in ('无法辨认','待核对','看不清'):
        return 'unreadable_text'
    return 'text_present'


def numeric_conflicts(unit):
    """Only compare explicit same-label, same-unit numeric fields, without guessing conversions."""
    from decimal import Decimal, InvalidOperation
    def fields(text):
        result={}
        for line in text.replace('**','').splitlines():
            match=re.match(r'^\s*([^:：\n]{1,64})[:：]\s*([-+]?\d[\d,.]*)([^\d\n]*)$',line)
            if not match:continue
            raw=match[2].rstrip('.')
            if ',' in raw and not re.fullmatch(r'[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?',raw):continue
            try:value=Decimal(raw.replace(',',''))
            except InvalidOperation:continue
            label=re.sub(r'\s+','',match[1]).casefold()
            units=re.sub(r'\s+','',match[3]).rstrip('.。').casefold()
            result[(label,units)]=value
        return result
    native=fields(unit.get('native_text',''));body=fields(unit.get('body',''))
    return [key[0] for key,value in native.items() if key in body and value!=body[key]]


def partition_unreadable(workflow, doc, artifacts, units, expected, work, config, client, prompt):
    from docflow.parser import transcribe
    from docflow.xlsx_regions import render_regions
    from PIL import Image
    from docflow.pipeline import ordered_bounded_map
    replacements={}
    for unit in units:
        state=assessment(unit)
        if not unit.get('image') or state not in ('missing_body','unreadable_text'):continue
        if unit.get('no_text_verification'):continue
        image=artifacts.resolve(unit)
        with Image.open(image) as pixels:
            width,height=pixels.size
        if max(width,height)<1200:continue
        region=unit.get('region',{})
        if region.get('dpi',0)>=360 or region.get('recovery_depth',0)>=1:continue
        parent=unit.get('recovery_parent',unit['id'])
        recovery_action(parent,'partition-source-v1')
        folder=artifacts.roots['derived']/unit['id'];folder.mkdir(parents=True,exist_ok=True)
        source=verify(workflow.store.root,doc['source'])
        fixed=artifacts.roots['base']/'render-input.pdf'
        if source.suffix.lower()=='.pdf':fixed=source
        page=unit.get('source_location',{}).get('page') or region.get('page')
        if page is None:
            match=re.search(r'(?:第|PDF第)(\d+)页',unit.get('label',''))
            if match:page=int(match[1])
        children=[]
        if fixed.is_file() and page and region.get('kind') not in ('pixel_tile','legacy_pixel_tile'):
            bounds=None
            if region.get('box') and region.get('dpi'):
                import math
                from pypdf import PdfReader
                physical=PdfReader(fixed).pages[page-1]
                width=math.ceil(float(physical.mediabox.width)*360/72)
                height=math.ceil(float(physical.mediabox.height)*360/72)
                x,y,right,bottom=region['box'];scale=360/region['dpi']
                bounds=[max(0,math.floor(x*scale)),max(0,math.floor(y*scale)),
                        min(width,math.ceil(right*scale)),min(height,math.ceil(bottom*scale))]
            children=render_regions(fixed,page-1,unit['label'],folder,config,dpi=360,bounds=bounds,native_kind='document')
        else:
            # A raster re-read uses smaller original-pixel crops, never synthetic upscaling.
            from docflow.xlsx_regions import region_boxes
            with Image.open(image) as pixels:
                for number,box in enumerate(region_boxes(width,height,1100,1100,32),1):
                    path=folder/f'crop-{number:03}.png';pixels.crop(box).save(path)
                    children.append({'label':unit['label']+'；原像素子区域'+str(box),
                        'text':'','image':path.name,
                        'region':{'box':list(box),'page_size':[width,height],'recovery_depth':1}})
        for number,child in enumerate(children,1):
            child.update(id=unit['id']+f'-zoom-{number:03}',recovery_parent=parent,
                image=str((folder/child['image']).relative_to(artifacts.roots['derived'])))
        if doc['extension']!='.xlsx' and unit.get('native_text','').strip():
            for offset in range(0,len(unit['native_text']),config.get('max_unit_text_chars',16000)):
                body=unit['native_text'][offset:offset+config.get('max_unit_text_chars',16000)]
                children.append({'id':unit['id']+'-native-'+str(offset),'label':unit['label']+'；原生文字片段'+str(offset),
                    'image':None,'text':body,'native_body':body,'method':'native_text','recovery_parent':parent})
        replacements[unit['id']]=artifacts.bind(children,'derived')
    if not replacements:return expected,units
    write_json(Path(work)/'density-recovery.json',{'source_parents':list(replacements),
        'children':{k:[u['image_ref'] for u in v if u.get('image_ref')] for k,v in replacements.items()},'human_review':False})
    derived=[u for children in replacements.values() for u in children]
    register_plan([u for u in expected if u['id'] not in replacements]+derived)
    def execute(unit):
        check_stop()
        view={**unit,'image':str(artifacts.resolve(unit))} if unit.get('image') else unit
        result={**unit,**transcribe(client,Path(work)/'derived-transcriptions'/unit['id'],view,doc['extension'],prompt)}
        write_json(Path(work)/'derived-transcriptions'/unit['id']/'result.json',result)
        return result
    derived=list(ordered_bounded_map(execute,derived,config.get('parser_workers',4)))
    derived=recover_units(derived,Path(work)/'derived-manifest.json',Path(work),config,client,prompt,
        resolver=artifacts.resolve,strict=False)
    by_id={u['id']:u for u in derived}
    result_by_id={u['id']:u for u in units}
    new_expected=[];new_results=[]
    for unit in expected:
        if unit['id'] in replacements:
            new_expected.extend(replacements[unit['id']]);new_results.extend(by_id[u['id']] for u in replacements[unit['id']])
        else:new_expected.append(unit);new_results.append(result_by_id[unit['id']])
    return new_expected,new_results


def run(cfg, workflow, doc, cache, expected, results, source_hash, parser_input_hash, work, config, client, prompt):
    from xlsx_recovery import recover as recover_xlsx
    artifacts = Artifacts(cache.parent/'prepared', work)
    expected = artifacts.bind(expected)
    results = artifacts.bind(results)
    session = current_session()
    if session is not None:
        session.adopt_calls(cache.parent)
    native_path = cache.parent/'prepared/prepared.json'
    native = read_json(native_path) if native_path.is_file() else {}
    if native.get('source_hash') and native['source_hash'] != parser_input_hash:
        raise WorkflowError('CONTENT_CHANGED', '固定准备文件与实际解析输入指纹不一致')
    for unit in results:
        match=re.search(r'像素区域\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)',unit.get('label',''))
        if match and not unit.get('region'):
            unit['region']={'kind':'legacy_pixel_tile','box':[int(v) for v in match.groups()],'recovery_depth':0}
    if doc['extension'] in ('.pdf','.docx','.pptx'):
        for unit in results:
            if unit.get('image') and not unit.get('region') and '本图为原页分区' not in unit.get('text',''):
                unit.setdefault('native_text', unit.get('text',''))
    from parse_quality import _confirmed_blank
    from docflow.parser import confirmed_blank_unit
    for unit in results:
        if unit.get('image') and _confirmed_blank(unit, unit):
            probe={**unit,'image':str(artifacts.resolve(unit))}
            if not confirmed_blank_unit(Path(work)/'blank-check'/unit['id'],probe,doc['extension']):
                raise WorkflowError('INTEGRITY_ERROR','空白标记与原生内容或渲染像素不一致')
    from source_catalog import check as check_source_catalog
    if cfg.get('_parse_original'):check_source_catalog(cfg,source_hash,parser_input_hash,expected,native,work)
    before = {u['id']: assessment(u) for u in results}
    region_coverage = None
    automatic=cfg.get('automatic_recovery_enabled',True)
    if type(automatic) is not bool:raise WorkflowError('CONFIG_ERROR','automatic_recovery_enabled必须为布尔值')
    if doc['extension'] == '.xlsx' and automatic:
        expected, results, region_coverage = recover_xlsx(workflow, doc, cache, expected, results,
            source_hash, work, config, client, prompt, parser_input_hash=parser_input_hash)
        if region_coverage:
            artifacts.add_store('xlsx-regions', Path(region_coverage).parent/'prepared')
            expected = artifacts.bind(expected, 'xlsx-regions')
            results = artifacts.bind(results, 'xlsx-regions')
    register_plan(expected)
    normalized = []
    for original in results:
        unit = copy.deepcopy(original)
        state = assessment(unit)
        if numeric_conflicts(unit):
            state='native_visual_conflict'
            unit.setdefault('issues',[]).append(MISSING)
        unit['content_state'] = state
        if state == 'unreadable_text':
            unit['body'] = ''
            unit['issues'] = list(unit.get('issues', [])) + [MISSING]
        elif state == 'missing_body' and unit.get('label') != '转换处理记录（非原文）':
            if MISSING not in unit.get('issues', []):unit.setdefault('issues', []).append(MISSING)
        normalized.append(unit)
    # Entire non-text documents are held without repeatedly rereading every region.
    report_before = summarize_units(expected, normalized, source_hash)
    has_native = any(u.get('native_text','').strip() for u in normalized)
    if automatic and (report_before['status'] != 'no_readable_text' or has_native):
        check_stop()
        normalized = recover_units(normalized, cache, work, config, client, prompt, resolver=artifacts.resolve, strict=False)
        expected, normalized = partition_unreadable(workflow, doc, artifacts, normalized, expected, work, config, client, prompt)
    # Validate every reference again before accepting a no-text claim.
    for unit in normalized:
        if unit.get('body','').strip():
            unit['content_state'] = assessment(unit)
        if numeric_conflicts(unit):
            unit['content_state']='native_visual_conflict'
            unit.setdefault('issues',[]).append('同一原生字段数值冲突，禁止自动发布')
        if unit.get('image_ref'):
            image = artifacts.resolve(unit)
            evidence = unit.get('no_text_verification')
            if evidence:
                response = Path(evidence.get('response_path', ''))
                if (digest(image) != evidence.get('image_sha256') or not response.is_file()
                        or digest(response) != evidence.get('response_sha256')):
                    raise WorkflowError('INTEGRITY_ERROR', '无文字证据文件或图像指纹不一致')
                if not response.resolve().is_relative_to(Path(work).resolve()):
                    raise WorkflowError('INTEGRITY_ERROR', '无文字响应越出当前任务')
                first = evidence.get('first_response')
                if not first or not Path(first['path']).is_file() or digest(Path(first['path'])) != first['sha256']:
                    raise WorkflowError('INTEGRITY_ERROR', '首次无文字响应证据缺失或不一致')
                origin = Path(first['path']).resolve()
                if not (origin.is_relative_to(cache.parent.resolve()) or origin.is_relative_to(Path(work).resolve())):
                    raise WorkflowError('INTEGRITY_ERROR', '首次读取证据越出受管理任务')
                request_ids=[]
                import base64
                uri='data:image/png;base64,'+base64.b64encode(image.read_bytes()).decode()
                image_key=hashlib.sha256(uri.encode()).hexdigest()
                for saved in (origin,response):
                    body,_=response_parts(client._extract(saved.read_bytes())[0])
                    receipt=saved.with_name('call.json');request=saved.with_name('request.json')
                    if body!='[[DOCFLOW_NO_TEXT]]' or not receipt.is_file() or not request.is_file():
                        raise WorkflowError('INTEGRITY_ERROR','无文字结论没有真实的响应/请求证据')
                    call=read_json(receipt);payload=read_json(request)
                    if call.get('response_sha256')!=digest(saved) or payload.get('request_hash')!=call.get('request_hash'):
                        raise WorkflowError('INTEGRITY_ERROR','无文字请求/响应指纹关联不一致')
                    images=[part.get('image_url',{}).get('url',{}) for msg in payload.get('payload',{}).get('messages',[])
                            for part in (msg.get('content',[]) if isinstance(msg.get('content'),list) else []) if part.get('type')=='image_url']
                    if not any(isinstance(value,dict) and value.get('data_uri_sha256')==image_key for value in images):
                        raise WorkflowError('INTEGRITY_ERROR','无文字结论不属于当前原图输入')
                    request_ids.append(call['request_hash'])
                if len(set(request_ids))!=2:
                    raise WorkflowError('INTEGRITY_ERROR','同一请求重放不能充当两次独立读取')
                unit['no_text_verification']['verified'] = True
    for unit in normalized:
        # Preserve the exact available native layer as a separately labelled source channel.
        native_text=unit.get('native_text','')
        if (native_text.strip() and unit.get('body','').strip()
                and unit.get('content_state')!='native_visual_conflict'):
            clean=lambda value: re.sub(r'\s+','',value)
            rendered=re.sub(r'\[(?:红字|黄字|加粗)(?:开始|结束)\]','',unit['body'])
            rendered=re.sub(r'[`*_#|]','',rendered)
            if clean(native_text) not in clean(rendered):
                unit['native_supplement']=native_text
                unit.setdefault('issues',[]).append('原生文字另行保留，未由模型改写')
    report = summarize_units(expected, normalized, source_hash)
    coverage = {'schema_version': 2, 'original_source_hash': source_hash,
        'parser_input_hash': parser_input_hash, 'controller_version': VERSION,
        'prepared_units': len(expected), 'native_sheet_count': len(native.get('native', {}).get('sheets', [])),
        'native_cells': sum(len(s.get('cells', [])) for s in native.get('native', {}).get('sheets', [])),
        'native_artifact_sha256': digest(native_path) if native_path.is_file() else None,
        'unit_states_before': before, 'human_review': False}
    write_json(Path(work)/'source-coverage.json', coverage)
    write_json(Path(work)/'controlled-results.json', {'source_hash': source_hash,
        'parser_input_hash': parser_input_hash, 'units': normalized, 'human_review': False})
    report.update(original_source_hash=source_hash, parser_input_hash=parser_input_hash,
        controller_version=VERSION, source_coverage_path=str(Path(work)/'source-coverage.json'),
        source_catalog_path=str(Path(work)/'source-catalog.json') if cfg.get('_parse_original') else None,
        repaired_results_path=str(Path(work)/'controlled-results.json'),
        repaired_results_sha256=digest(Path(work)/'controlled-results.json'))
    if session is not None:report['runtime'] = session.summary()
    if region_coverage:report['region_coverage_path'] = region_coverage
    return expected, normalized, report
