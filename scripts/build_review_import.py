"""Build a hash-bound manifest from a selected unified reading set. No approval."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docflow.common import digest,write_json,WorkflowError

def main():
 p=argparse.ArgumentParser();p.add_argument('--review-dir',required=True);p.add_argument('--output',required=True);p.add_argument('--ids',nargs='*');a=p.parse_args()
 root=Path(a.review_dir).resolve();rows=json.loads((root/'核对记录/文件映射.json').read_text());selected=set(a.ids or []);docs=[]
 for r in rows:
  if selected and r['id'] not in selected and r['sha256'] not in selected:continue
  paths={'source':Path(r['snapshot']),'reading':Path(r['review']),'provenance':root/'来源与解析记录'/(r['id']+'.json')}
  docs.append({'name':r['name'],'source_locations':r['origins'],**{k:{'path':str(v.resolve()),'sha256':digest(v)} for k,v in paths.items()}})
 if not docs:raise WorkflowError('REVIEW_SCOPE_INVALID','未匹配任何资料')
 if selected:
  known={x for r in rows for x in [r['id'],r['sha256']]}
  if selected-known:raise WorkflowError('REVIEW_SCOPE_INVALID','存在未知编号')
 write_json(Path(a.output).resolve(),{'schema_version':1,'documents':docs,'human_review_approved':False})
 print(json.dumps({'documents':len(docs),'status':'manifest_only_not_approved'},ensure_ascii=False))
if __name__=='__main__':main()
