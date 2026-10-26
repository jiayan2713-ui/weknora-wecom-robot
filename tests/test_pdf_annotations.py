import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter,PdfReader
from pypdf.generic import DictionaryObject,ArrayObject,NameObject,TextStringObject,IndirectObject,NumberObject,NullObject
from PIL import Image
from docflow.documents import pdf_annotation_links,prepare
from docflow.config import configuration

class PdfAnnotationTests(unittest.TestCase):
 def test_real_indirect_action_uri_and_annotation_array(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'indirect.pdf';w=PdfWriter();page=w.add_blank_page(width=100,height=100)
   uri=w._add_object(TextStringObject('https://example.invalid/source'))
   action=w._add_object(DictionaryObject({NameObject('/S'):NameObject('/URI'),NameObject('/URI'):uri}))
   annotation=w._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Link'),NameObject('/A'):action}))
   page[NameObject('/Annots')]=w._add_object(ArrayObject([annotation]));w.write(p)
   self.assertEqual(pdf_annotation_links(PdfReader(p).pages[0]),(['https://example.invalid/source'],[]))
 def test_missing_action_reference_reports_issue_and_keeps_page(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();p=root/'missing.pdf';w=PdfWriter();page=w.add_blank_page(width=100,height=100)
   annotation=DictionaryObject({NameObject('/A'):IndirectObject(9999,0,w)})
   page[NameObject('/Annots')]=ArrayObject([annotation]);w.write(p)
   def render(command,timeout,**kwargs):Image.new('RGB',(20,20),'white').save(command[-1]+'.png')
   with patch('docflow.documents.run',side_effect=render):units=prepare(p,root/'prepared',configuration())
   self.assertEqual(units[0]['label'],'PDF第1页');self.assertTrue(units[0]['image'])
   self.assertEqual(units[1]['label'],'转换处理记录（非原文）');self.assertIn('引用失效',units[1]['text'])
 def test_absent_null_and_direct_annotations(self):
  self.assertEqual(pdf_annotation_links({}),([],[]))
  self.assertEqual(pdf_annotation_links({'/Annots':NullObject()}),([],[]))
  self.assertEqual(pdf_annotation_links({'/Annots':[{'/A':{'/URI':'https://example.invalid'}}]}),(['https://example.invalid'],[]))
 def test_bad_objects_do_not_crash(self):
  links,issues=pdf_annotation_links({'/Annots':[NumberObject(4),{'/A':NumberObject(2)},{'/A':{'/URI':None}}]})
  self.assertEqual(links,[]);self.assertEqual(len(issues),3)
 def test_cyclic_reference_is_bounded(self):
  class Resolver:
   def get_object(self,ref):return ref
  links,issues=pdf_annotation_links({'/Annots':IndirectObject(1,0,Resolver())})
  self.assertFalse(links);self.assertTrue(issues)

if __name__=='__main__':unittest.main()
