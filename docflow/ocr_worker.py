from pathlib import Path
import json,sys
from rapidocr import RapidOCR
engine=RapidOCR()
r=engine(sys.argv[1]);rows=[]
if r.txts is not None:
 for t,s,b in zip(r.txts,r.scores,r.boxes):rows.append({'text':t,'confidence':float(s),'box':b.tolist()})
Path(sys.argv[2]).write_text(json.dumps({'engine':'RapidOCR ONNX CPU','lines':rows},ensure_ascii=False,indent=2))
