"""Verify managed original-source checkpoints before reusing parse artifacts."""
from pathlib import Path
from docflow.common import WorkflowError, read_json, digest


def checkpoint(cfg, source, source_hash):
    path=cfg.get('_source_checkpoint_work') or cfg.get('_pdf_checkpoint_work') or cfg.get('_xlsx_checkpoint_work')
    if not path:return None
    path=Path(path).resolve()
    jobs=(Path(cfg['state_dir'])/'jobs').resolve()
    seen=set()
    for _ in range(4):
        if not path.is_relative_to(jobs) or path in seen:
            raise WorkflowError('CONTENT_CHANGED','原有解析检查点位置不合法')
        seen.add(path)
        prior=read_json(path/'parse-report.json')
        if (prior.get('source_hash')!=source_hash and not (prior.get('source_hash')==cfg.get('_logical_source_hash') and prior.get('original_source_hash')==source_hash)) or prior.get('status')!='incomplete':return None
        if not prior.get('checkpoint_work'):break
        path=Path(prior['checkpoint_work']).resolve()
    else:
        raise WorkflowError('LOCATION_MISMATCH','解析检查点链条过长')
    if not (path/source.name).is_file():return None
    if digest(path/source.name)!=source_hash:
        raise WorkflowError('CONTENT_CHANGED','原有解析检查点与当前原件不匹配')
    from docflow.runner import Workflow
    candidates=list((path/'docflow/batches').glob('*/batch.json'))
    if len(candidates)!=1:
        raise WorkflowError('LOCATION_MISMATCH','旧解析任务无法唯一定位')
    batch=read_json(candidates[0])
    workflow=Workflow(path/'docflow')
    status=workflow.status(batch['id'])
    if status['phase']!='awaiting_review':return None
    return workflow,status,batch

