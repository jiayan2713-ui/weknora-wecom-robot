"""Recover only failed visual XLSX units while preserving verified cache evidence."""
import copy
from pathlib import Path

from docflow.common import WorkflowError, read_json, verify, write_json, digest
from docflow.parser import transcribe
from docflow.pipeline import ordered_bounded_map
from docflow.xlsx_regions import render_regions
from parse_quality import summarize_units
from docflow.runtime import recovery_action


from parse_checkpoint import checkpoint


def recover(workflow, doc, cache, expected, results, source_hash, work, config, client, prompt, *, parser_input_hash=None):
    report=summarize_units(expected,results,source_hash)
    failed={unit['id'] for unit in report['units'] if unit['status'] in ('missing_body','no_readable_text')}
    if not failed:return expected,results,None
    originals={unit['id']:unit for unit in expected}
    # Native extraction and structural errors are never disguised as a visual failure.
    if report['missing_ids'] or report['duplicate_ids'] or report['unexpected_ids'] or any(
            not originals[identity].get('image') or originals[identity].get('region') for identity in failed):
        return expected,results,None
    prepared_index=read_json(verify(workflow.store.root,doc['prepared_ref']))
    refs=prepared_index['artifacts']
    pdf_ref=next((ref for ref in refs if ref['path'].endswith('/prepared/render-input.pdf')),None)
    native_ref=next((ref for ref in refs if ref['path'].endswith('/prepared/prepared.json')),None)
    if not pdf_ref or not native_ref:
        raise WorkflowError('LOCATION_MISMATCH','未找到经过指纹校验的PDF及原生记录')
    pdf=verify(workflow.store.root,pdf_ref)
    native=read_json(verify(workflow.store.root,native_ref))
    if native['source_hash']!=(parser_input_hash or source_hash):
        raise WorkflowError('CONTENT_CHANGED','准备文件不属于当前原件版本')
    sheets=native['native']['sheets']
    from pypdf import PdfReader
    reader=PdfReader(pdf)
    if len(reader.pages)!=len(sheets):
        raise WorkflowError('LOCATION_MISMATCH','工作表与PDF页不对应')
    work=Path(work)/'xlsx-region-recovery'
    prepared=work/'prepared'
    replacements={}
    mapping=[]
    for identity in sorted(failed):
        old=originals[identity]
        page_indexes=[i for i,sheet in enumerate(sheets) if old['label']==f'工作表{sheet["name"]}！{sheet["range"]}']
        if len(page_indexes)!=1:
            raise WorkflowError('LOCATION_MISMATCH','失败视图无法唯一关联工作表')
        page_index=page_indexes[0]
        recovery_action(old.get('recovery_parent', identity), 'partition-120dpi')
        regions=render_regions(pdf,page_index,old['label'],prepared,config,page=reader.pages[page_index])
        for index,unit in enumerate(regions,1):
            unit['id']=f'{identity}-region-{index:03}'
            unit['recovery_parent']=old.get('recovery_parent', identity)
        replacements[identity]=regions
        mapping.append({'original_unit':identity,'label':old['label'],'regions':[{'id':u['id'],**u['region']} for u in regions]})
    write_json(work/'coverage.json',{'source_hash':source_hash,'replaced_visual_units':mapping,
                                   'reused_units':len(expected)-len(failed),'human_review':False})
    all_regions=[unit for group in replacements.values() for unit in group]
    def parse(unit):
        directory=work/unit['id'];directory.mkdir(parents=True,exist_ok=True)
        result=transcribe(client,directory,unit,'.xlsx',prompt)
        result={**unit,**result}
        write_json(directory/'result.json',result)
        return result
    recovered=list(ordered_bounded_map(parse,all_regions,config.get('parser_workers',4)))
    prior_results={unit['id']:unit for unit in results}
    recovered_by_id={unit['id']:unit for unit in recovered}
    new_expected=[];new_results=[]
    for unit in expected:
        if unit['id'] in replacements:
            new_expected.extend(replacements[unit['id']])
            new_results.extend(recovered_by_id[x['id']] for x in replacements[unit['id']])
        else:
            new_expected.append(copy.deepcopy(unit))
            new_results.append(copy.deepcopy(prior_results[unit['id']]))
    write_json(work/'prepared-index.json',{'units':new_expected})
    write_json(work/'results.json',{'units':new_results})
    return new_expected,new_results,str(work/'coverage.json')
