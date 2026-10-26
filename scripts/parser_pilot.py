"""Bounded paired experiment. Never promotes a route or touches production caches."""
import argparse,concurrent.futures,json,re,shutil,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docflow.common import digest,read_json,write_json,fingerprint,WorkflowError
from docflow.config import configuration
from docflow.model import ModelClient
from docflow.parser import transcribe

def run_job(case,route,out):
    directory=out/case['id']/route
    directory.mkdir(parents=True,exist_ok=True)
    result_path=directory/'result.json'
    for p,h in case['inputs'].items():
        if digest(Path(p))!=h:raise WorkflowError('CONTENT_CHANGED','试验输入改变')
    cfg=configuration({**case['config'],'input_route':route,'max_attempts':1,'model_review':False,'local_ocr_python':None})
    identity=fingerprint({'case':case,'config':cfg})
    if result_path.exists():
        old=read_json(result_path)
        if old.get('identity')!=identity:raise WorkflowError('INTEGRITY_ERROR','试验结果身份不匹配')
        return old
    prepared=directory/'prepared';prepared.mkdir(exist_ok=True)
    target=prepared/case['unit']['image']
    if not target.exists():shutil.copyfile(case['image'],target)
    if digest(target)!=case['inputs'][case['image']]:raise WorkflowError('INTEGRITY_ERROR','试验页图改变')
    start=time.monotonic()
    try:
        result=transcribe(ModelClient(cfg),directory/case['unit']['id'],case['unit'],case['extension'],Path(case['prompt']).read_text())
        status='success'
    except WorkflowError as e:result={'error':e.record()};status='failed'
    call=directory/case['unit']['id']/'draft/call.json'
    record=read_json(call) if call.exists() else {}
    body=result.get('body','');anchors=case['anchors'];hits=[x for x in anchors if re.sub(r'\s+','',x) in re.sub(r'\s+','',body)]
    output={'identity':identity,'case':case['id'],'route':route,'status':status,'seconds':time.monotonic()-start,
            'usage':record.get('usage'),'anchor_hits':hits,'anchors':anchors,'body':body,'result':result,
            'quality_passed':False,'quality_note':'字符/数字命中仅辅助检查，表格对应及遗漏仍需逐页验证'}
    write_json(result_path,output,replace=False);return output

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--output',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    manifest=read_json(a.manifest);out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    if not 1<=len(manifest['cases'])<=6:raise ValueError('实验最多6对')
    if not a.execute:print(json.dumps({'cases':len(manifest['cases']),'requests':2*len(manifest['cases']),'action':'add --execute for authorized run'}));return
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(run_job,c,r,out) for c in manifest['cases'] for r in ['multimodal','text_experiment']]
        results=[f.result() for f in futures]
    write_json(out/'结果.json',results)
    print(json.dumps([{k:r[k] for k in ['case','route','status','seconds','usage','anchor_hits']} for r in results],ensure_ascii=False))
if __name__=='__main__':main()
