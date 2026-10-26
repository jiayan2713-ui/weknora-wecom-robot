import importlib.util,tempfile,unittest,zipfile
from pathlib import Path
from lxml import etree as E
from docflow import office_static as m
REL='http://schemas.openxmlformats.org/package/2006/relationships'
class RepairTests(unittest.TestCase):
 def package(self,path,external_only=False):
  with zipfile.ZipFile(path,'w') as z:
   z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
   z.writestr('ppt/slides/slide1.xml',f'<p:sld xmlns:p="{m.P}" xmlns:a="{m.A}" xmlns:r="{m.R}"><p:cSld><p:spTree><a:blip '+('' if external_only else 'r:embed="r1" ')+'r:link="r2"/></p:spTree></p:cSld></p:sld>')
   z.writestr('ppt/slides/_rels/slide1.xml.rels',f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{m.R}/image" Target="../media/image.png"/><Relationship Id="r2" Type="{m.R}/image" Target="file:///not-allowed.png" TargetMode="External"/></Relationships>')
   z.writestr('ppt/media/image.png',b'original-image-bytes')
 def test_embedded_image_preserved_external_link_removed(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'src.pptx';self.package(src);before=m.sha(src);out,log,_=m.sanitize_office(src,root/'out')
   self.assertEqual(m.sha(src),before)
   with zipfile.ZipFile(out) as z:
    self.assertEqual(z.read('ppt/media/image.png'),b'original-image-bytes');tree=E.fromstring(z.read('ppt/slides/slide1.xml'));blip=tree.find('.//{*}blip');self.assertEqual(blip.get('{'+m.R+'}embed'),'r1');self.assertIsNone(blip.get('{'+m.R+'}link'))
 def test_unavailable_external_image_is_not_silently_dropped(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'src.pptx';self.package(src,True)
   with self.assertRaises(m.Hold):m.sanitize_office(src,root/'out')
 def test_duplicate_embedded_payloads_preserved_without_overwrite(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'src.pptx';self.package(src)
   with zipfile.ZipFile(src,'a') as z:
    z.writestr('ppt/embeddings/a.bin',b'opaque');z.writestr('ppt/embeddings/b.bin',b'opaque')
   output,_,assets=m.sanitize_office(src,root/'out');self.assertEqual(len(assets),2);self.assertEqual(assets[0]['path'],assets[1]['path']);self.assertEqual(Path(assets[0]['path']).read_bytes(),b'opaque')
   m.sanitize_office(src,root/'out')

class ActiveObjectTests(unittest.TestCase):
 def test_ole_preview_keeps_object_geometry(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'ole.pptx';RepairTests().package(src)
   with zipfile.ZipFile(src) as z:files={n:z.read(n) for n in z.namelist()}
   files['ppt/slides/slide1.xml']=f'<p:sld xmlns:p="{m.P}" xmlns:a="{m.A}" xmlns:r="{m.R}"><p:cSld><p:spTree><p:graphicFrame><p:xfrm><a:off x="10" y="20"/><a:ext cx="30" cy="40"/></p:xfrm><a:graphic><a:graphicData><p:oleObj><p:pic><p:blipFill><a:blip r:embed="r1"/></p:blipFill><p:spPr/></p:pic></p:oleObj></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>'.encode()
   with zipfile.ZipFile(src,'w') as z:
    for n,b in files.items():z.writestr(n,b)
   target,_,_=m.sanitize_office(src,root/'out')
   with zipfile.ZipFile(target) as z:
    tree=E.fromstring(z.read('ppt/slides/slide1.xml'));self.assertFalse(tree.findall('.//{*}oleObj'));xf=tree.find('.//{'+m.P+'}pic/{'+m.P+'}spPr/{'+m.A+'}xfrm');self.assertEqual(xf[0].get('x'),'10');self.assertEqual(xf[1].get('cy'),'40')
 def test_chart_cache_preserved_and_missing_data_rejected(self):
  for points in [True,False]:
   with tempfile.TemporaryDirectory() as t:
    root=Path(t);src=root/'chart.pptx';RepairTests().package(src)
    cache='<c:pt idx="0"><c:v>123.45</c:v></c:pt>' if points else ''
    xml=f'<c:chartSpace xmlns:c="{m.C}" xmlns:r="{m.R}"><c:chart><c:ser><c:numCache>{cache}</c:numCache></c:ser></c:chart><c:externalData r:id="r5"/></c:chartSpace>'
    with zipfile.ZipFile(src,'a') as z:
     z.writestr('ppt/charts/chart1.xml',xml);z.writestr('ppt/charts/_rels/chart1.xml.rels',f'<Relationships xmlns="{REL}"><Relationship Id="r5" Type="{m.R}/oleObject" Target="file:///missing.xlsx" TargetMode="External"/></Relationships>')
    if not points:
     with self.assertRaises(m.Hold):m.sanitize_office(src,root/'out')
    else:
     target,_,_=m.sanitize_office(src,root/'out')
     with zipfile.ZipFile(target) as z:
      tree=E.fromstring(z.read('ppt/charts/chart1.xml'));self.assertEqual(tree.find('.//{*}v').text,'123.45');self.assertIsNone(tree.find('.//{*}externalData'))

if __name__=='__main__':unittest.main()
