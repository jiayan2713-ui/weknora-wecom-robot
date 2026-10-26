"""Prepare reversible, provenance-linked static views without fetching external content."""
from pathlib import Path,PurePosixPath
import copy,hashlib,io,json,posixpath,re,shutil,zipfile
from lxml import etree as E
from PIL import Image,ImageOps
from pypdf import PdfReader,PdfWriter
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/presentationml/2006/main'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
C='http://schemas.openxmlformats.org/drawingml/2006/chart'
S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
MEDIA={'.mp4','.mov','.avi','.wmv','.m4v','.mp3','.wav','.m4a','.wma','.flv','.webm'}
ACTIVE=re.compile(r'(?i)\b(IMAGE|WEBSERVICE|DDE|DDEAUTO|RTD|INCLUDEPICTURE|INCLUDETEXT|LINK)\b')
class Hold(Exception):pass

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
 return h.hexdigest()

def tree(raw):
 if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise Hold('XML包含自定义实体，保留原件待处理')
 return E.fromstring(raw,E.XMLParser(resolve_entities=False,no_network=True))
def local(n):return n.tag.rsplit('}',1)[-1] if isinstance(n.tag,str) else ''
def owner(rel):
 p=PurePosixPath(rel);return str(p.parent.parent/p.name[:-5])
def target(part,link):return posixpath.normpath(posixpath.join(posixpath.dirname(part),link)).lstrip('/')
def emit_text_parts(lines,folder,prefix):
 folder.mkdir(parents=True,exist_ok=True);result=[];chunk=[];length=0
 for line in lines:
  # Long cell values are split only for transport; sequence labels preserve reconstruction.
  pieces=[line[i:i+7000] for i in range(0,len(line),7000)] or ['']
  for part in pieces:
   if chunk and length+len(part)>10000:
    p=folder/f'{prefix}-{len(result)+1:03d}.txt';p.write_text('\n'.join(chunk)+'\n');result.append(p);chunk=[];length=0
   chunk.append(part);length+=len(part)+1
 if chunk:
  p=folder/f'{prefix}-{len(result)+1:03d}.txt';p.write_text('\n'.join(chunk)+'\n');result.append(p)
 return result

def xlsx_records(source):
 """Read sparse XML cells directly, retaining coordinates, formulas and cached values."""
 with zipfile.ZipFile(source) as z:
  names=set(z.namelist());book=tree(z.read('xl/workbook.xml'));rels=tree(z.read('xl/_rels/workbook.xml.rels'));mapping={n.get('Id'):n.get('Target') for n in rels};strings=[]
  if 'xl/sharedStrings.xml' in names:
   strings=[''.join(n.itertext()) for n in tree(z.read('xl/sharedStrings.xml'))]
  style_formats=[];formats={}
  if 'xl/styles.xml' in names:
   styles=tree(z.read('xl/styles.xml'));fm=styles.find('{*}numFmts')
   if fm is not None:formats={n.get('numFmtId'):n.get('formatCode') for n in fm}
   xfs=styles.find('{*}cellXfs')
   if xfs is not None:style_formats=[formats.get(n.get('numFmtId'), 'builtin:'+str(n.get('numFmtId'))) for n in xfs]
  result=[]
  for sheet in book.find('{*}sheets'):
   ref=mapping[sheet.get('{'+R+'}id')];part=target('xl/workbook.xml',ref);t=tree(z.read(part));merges=[n.get('ref') for n in t.findall('.//{*}mergeCell')];cells=[]
   for c in t.findall('.//{*}sheetData/{*}row/{*}c'):
    f=c.find('{*}f');v=c.find('{*}v');inline=c.find('{*}is');value=v.text if v is not None else None
    if c.get('t')=='s' and value is not None:value=strings[int(value)]
    if inline is not None:value=''.join(inline.itertext())
    if f is None and (value is None or value==''):continue
    cells.append({'cell':c.get('r'),'type':c.get('t'),'value_or_cached':value,'formula':f.text if f is not None else None,'formula_attributes':dict(f.attrib) if f is not None else None,'number_format':style_formats[int(c.get('s',0))] if style_formats else None})
   result.append({'sheet':sheet.get('name'),'state':sheet.get('state','visible'),'dimension':t.find('{*}dimension').get('ref') if t.find('{*}dimension') is not None else None,'merges':merges,'cells':cells})
  return result

def sanitize_office(source,folder):
 source=Path(source);folder.mkdir(parents=True,exist_ok=True);dest=folder/('safe'+source.suffix);log=[];attachments=[]
 with zipfile.ZipFile(source) as z:
  names=set(z.namelist());trees={}
  for i in z.infolist():
   if i.filename.endswith(('.xml','.rels')):
    if i.file_size>64*1024*1024:raise Hold('结构XML过大，需要专门拆分')
    trees[i.filename]=tree(z.read(i.filename))
  removed={n for n in names if '/embeddings/' in n or '/externalLinks/' in n or any(x in n.lower() for x in ['vbaproject','activex','connections.xml']) or Path(n).suffix.lower() in MEDIA}
  # Keep embedded payloads as quarantined originals, never execute them.
  for name in sorted(removed):
   if '/embeddings/' in name and not name.endswith('/'):
    payload=z.read(name);h=hashlib.sha256(payload).hexdigest();adir=folder/'保留嵌入原件';adir.mkdir(exist_ok=True);p=adir/(h+Path(name).suffix);
    if p.exists():
     if sha(p)!=h:raise Hold('嵌入原件哈希冲突')
    else:p.write_bytes(payload);p.chmod(0o444)
    attachments.append({'member':name,'path':str(p),'sha256':h})
  if attachments:log.append({'action':'archive_embedded_payloads','count':len(attachments),'note':'不执行嵌入对象；静态图片/图表缓存保留；附件另存供核对'})
  # Prefer the declared fallback for active OLE AlternateContent.
  for part,t in list(trees.items()):
   if part in removed or part.endswith('.rels'):continue
   for n in list(t.iter()):
    if local(n)=='AlternateContent' and any(local(c)=='oleObj' for c in n.iter()):
     fallback=next((c for c in n if local(c)=='Fallback'),None)
     if fallback is None:raise Hold('嵌入对象没有静态回退内容')
     parent=n.getparent();pos=parent.index(n)
     for child in list(fallback):parent.insert(pos,copy.deepcopy(child));pos+=1
     parent.remove(n)
   for n in list(t.iter()):
    if local(n)=='oleObj':
     pics=n.findall('.//{'+P+'}pic')
     if not pics:raise Hold('嵌入对象缺少可保留的静态预览')
     frame=n
     while frame is not None and local(frame)!='graphicFrame':frame=frame.getparent()
     if frame is None:raise Hold('嵌入对象布局无法安全转换')
     pic=copy.deepcopy(pics[0]);sp=pic.find('{'+P+'}spPr');xf=frame.find('{'+P+'}xfrm')
     if xf is not None and sp is not None:
      for old in list(sp):
       if local(old)=='xfrm':sp.remove(old)
      xf=copy.deepcopy(xf);xf.tag='{'+A+'}xfrm';sp.insert(0,xf)
     frame.getparent().replace(frame,pic);log.append({'action':'ole_to_cached_picture','part':part})
   for n in list(t.iter()):
    if local(n) in {'videoFile','audioFile','media','audioCd','webVideoPr'} and n.getparent() is not None:
     n.getparent().remove(n);log.append({'action':'skip_media_per_user','part':part})
   for n in list(t.iter()):
    if local(n)=='externalData':
     pts=t.findall('.//{'+C+'}pt')
     if not pts and (t.findall('.//{'+C+'}ser') or t.findall('.//{'+C+'}f')):raise Hold('图表依赖外部数据但无缓存点')
     n.getparent().remove(n);log.append({'action':'retain_chart_cache_no_refresh','part':part,'cached_points':len(pts)})
    elif local(n) in {'externalReferences','attachedTemplate'}:
     n.getparent().remove(n);log.append({'action':'detach_external_binding','part':part,'tag':local(n)})
   if part.startswith('word/'):
    for paragraph in t.findall('.//{'+W+'}p'):
     if any(ACTIVE.search(n.text or '') for n in paragraph.findall('.//{'+W+'}instrText')):
      for n in list(paragraph.iter()):
       if local(n) in {'instrText','fldChar'} and n.getparent() is not None:n.getparent().remove(n)
      log.append({'action':'retain_cached_field_result','part':part})
   if part.startswith('xl/worksheets/'):
    # Freeze displayed cache in this static derivative; original formulas are exported separately.
    for cell in t.findall('.//{*}sheetData/{*}row/{*}c'):
     f=cell.find('{*}f')
     if f is not None:
      cell.remove(f);v=cell.find('{*}v')
      if v is None or v.text is None:
       if v is not None:cell.remove(v)
       cell.set('t','inlineStr');inline=E.SubElement(cell,'{'+S+'}is');E.SubElement(inline,'{'+S+'}t').text='[原公式结果未缓存，待核对]'
   # Preserve text/cached drawings, not instructions that could fetch external content.
  for relpart,rels in list(trees.items()):
   if not relpart.endswith('.rels') or relpart in removed:continue
   part=owner(relpart);t=trees.get(part);byid={n.get('Id'):n for n in rels}
   for rel in list(rels):
    typ=rel.get('Type','').rsplit('/',1)[-1];rid=rel.get('Id');external=rel.get('TargetMode')=='External';destpart=target(part,rel.get('Target',''))
    if external and typ=='hyperlink':continue
    if external and typ=='image':
     uses=[n for n in t.iter() if rid in n.attrib.values()] if t is not None else []
     for n in uses:
      embed=n.get('{'+R+'}embed');er=byid.get(embed)
      if er is None or er.get('TargetMode')=='External' or target(part,er.get('Target','')) not in names:raise Hold('外部图片无内嵌副本，不能假定已恢复')
      for attr in list(n.attrib):
       if n.attrib[attr]==rid:del n.attrib[attr]
     rels.remove(rel);log.append({'action':'use_embedded_image','part':part,'uses':len(uses)})
    elif external or destpart in removed:
     uses=[n for n in t.iter() if rid in n.attrib.values()] if t is not None else []
     if uses:raise Hold('尚有未处理的外部/主动关系引用：'+typ+' '+part)
     rels.remove(rel)
  ct=trees['[Content_Types].xml']
  for n in list(ct):
   if n.get('PartName','').lstrip('/') in removed:ct.remove(n)
  with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as out:
   for info in z.infolist():
    if info.filename in removed or info.is_dir():continue
    if info.filename in trees:out.writestr(info.filename,E.tostring(trees[info.filename],encoding='utf-8',xml_declaration=True))
    else:
     with z.open(info) as src,out.open(info.filename,'w') as dst:shutil.copyfileobj(src,dst,1024*1024)
  log.append({'action':'removed_payload_bytes','bytes':sum(z.getinfo(n).file_size for n in removed),'names':sorted(removed)})
 with zipfile.ZipFile(dest) as z:assert z.testzip() is None
 return dest,log,attachments


def prepare_static(source, work):
    """Validated static fallback; never execute or silently discard attachment evidence."""
    from .common import WorkflowError, write_json
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > 20000 or sum(x.file_size for x in members) > 512 * 1024 * 1024:
            raise WorkflowError('CAPACITY_EXCEEDED', 'Office解压内容超出静态处理预算')
        if len({x.filename for x in members}) != len(members):
            raise WorkflowError('UNSAFE_OFFICE', 'Office存在重复成员')
    before = sha(source)
    try:
        result, actions, attachments = sanitize_office(source, Path(work))
    except (Hold, KeyError, ValueError, E.XMLSyntaxError) as exc:
        raise WorkflowError('UNSAFE_OFFICE', '无法可靠静态化，保留原件待处理', scope=str(exc)) from exc
    if sha(source) != before:
        raise WorkflowError('CONTENT_CHANGED', '原件在准备期间改变')
    write_json(Path(work)/'static-provenance.json', {'source_sha256': before, 'actions': actions, 'attachments': attachments})
    return result, [{'label': '转换处理记录（非原文）', 'text':
        f'使用安全静态副本；保留隔离二进制附件{len(attachments)}份，其内部未独立转写。图表/图片回退及附件需对照原件审核。'}]
