import io,json,tempfile,unittest,zipfile
from pathlib import Path
from docflow.wps_checkboxes import freeze,verify_publication,WPS,R
from docflow.common import WorkflowError

class CheckboxTests(unittest.TestCase):
 def fixture(self,value='true',extra='',ref='r2',image_mode=''):
  files={
   '[Content_Types].xml':'<Types><Override PartName="/xl/jdecontrols/jdecontrol1.xml"/></Types>',
   'xl/workbook.xml':f'<workbook xmlns:r="{R}"><sheets><sheet name="表单" r:id="s1"/></sheets></workbook>',
   'xl/_rels/workbook.xml.rels':'<Relationships><Relationship Id="s1" Target="worksheets/sheet1.xml"/></Relationships>',
   'xl/worksheets/sheet1.xml':f'<worksheet xmlns:r="{R}"><drawing r:id="d1"/></worksheet>',
   'xl/worksheets/_rels/sheet1.xml.rels':'<Relationships><Relationship Id="d1" Target="../drawings/drawing1.xml"/></Relationships>',
   'xl/drawings/drawing1.xml':f'<wsDr xmlns:r="{R}" xmlns:w="{WPS}"><twoCellAnchor><from><row>3</row><col>2</col><rowOff>10</rowOff><colOff>20</colOff></from><to><row>3</row><col>2</col><rowOff>100</rowOff><colOff>200</colOff></to><pic><blip r:embed="img"/><extLst><w:jdecontrolRef r:id="{ref}"/></extLst></pic></twoCellAnchor></wsDr>',
   'xl/drawings/_rels/drawing1.xml.rels':f'<Relationships><Relationship Id="r2" Type="{WPS}" Target="../jdecontrols/jdecontrol1.xml"/><Relationship Id="img" Type="{R}/image" Target="../media/a.png" {image_mode}/></Relationships>',
   'xl/jdecontrols/jdecontrol1.xml':f'<w:jdecontrol xmlns:w="{WPS}"><control type="checkbox"><property type="facade"><caption>是</caption><name>choice1</name><value>{value}</value></property>{extra}</control></w:jdecontrol>',
   'xl/media/a.png':b'original-picture'
  }
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w') as z:
   for n,b in files.items():z.writestr(n,b)
  stream.seek(0)
  return zipfile.ZipFile(stream)
 def test_true_and_false_preserved_with_exact_anchor_and_caption(self):
  for value in ('true','false'):
   with self.fixture(value) as z:
    before=z.read('xl/media/a.png');patches,removed,notes=freeze(z)
    row=json.loads(notes[0]['text'].splitlines()[1])
    self.assertEqual(row['checked'],value=='true')
    self.assertEqual(row['caption'],'是');self.assertEqual(row['anchor']['from']['row'],3)
    self.assertEqual(z.read('xl/media/a.png'),before)
    self.assertNotIn('xl/media/a.png',patches)
    self.assertEqual(len(removed),1)
    self.assertNotIn(b'jdecontrolRef',patches['xl/drawings/drawing1.xml'])
    self.assertEqual(notes[0]['native_body'],notes[0]['text'])
 def test_ambiguous_state_rejected(self):
  with self.fixture('unknown') as z,self.assertRaises(WorkflowError):freeze(z)
 def test_data_binding_rejected(self):
  with self.fixture(extra='<property type="data"><control_source>A1</control_source></property>') as z,self.assertRaises(WorkflowError):freeze(z)
 def test_unknown_action_rejected(self):
  with self.fixture(extra='<onClick>macro</onClick>') as z,self.assertRaises(WorkflowError):freeze(z)
 def test_external_preview_rejected(self):
  with self.fixture(image_mode='TargetMode="External"') as z,self.assertRaises(WorkflowError):freeze(z)
 def test_no_controls_leaves_package_untouched(self):
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:z.writestr('a.txt','preserve')
  with zipfile.ZipFile(b) as z:self.assertEqual(freeze(z),({},set(),[]))

 def test_publication_requires_every_native_state_exactly_once(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'source.xlsx'
   with self.fixture() as source,zipfile.ZipFile(path,'w') as dest:
    for name in source.namelist():dest.writestr(name,source.read(name))
    _,_,notes=freeze(source)
   text=notes[0]['native_body']
   self.assertEqual(verify_publication(path,text)['checked'],1)
   for invalid in ('missing body',text+'\n'+text):
    with self.assertRaises(WorkflowError):verify_publication(path,invalid)
