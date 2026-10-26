"""Render bounded high-resolution XLSX PDF regions without shrinking whole sheets."""
import math
from pathlib import Path

from .common import WorkflowError


def region_boxes(width, height, tile_width=2000, tile_height=1400, overlap=96):
    if min(width, height, tile_width, tile_height)>0 and 0<=overlap<min(tile_width,tile_height):
        boxes=[]
        y=0
        while y<height:
            x=0
            while x<width:
                right,bottom=min(x+tile_width,width),min(y+tile_height,height)
                boxes.append((x,y,right,bottom))
                if right==width:break
                x=right-overlap
            if bottom==height:break
            y=bottom-overlap
        if len(boxes)>128:
            raise WorkflowError('CAPACITY_EXCEEDED','单张工作表需超过128个分区，请按业务区域拆分')
        return boxes
    raise ValueError('invalid_xlsx_region_dimensions')


def render_regions(pdf, page_index, label, work, config, *, page=None, text='', dpi=120, bounds=None, native_kind='spreadsheet'):
    from pypdf import PdfReader
    from PIL import Image
    from .documents import run
    page=page if page is not None else PdfReader(pdf).pages[page_index]
    if page.rotation or float(page.mediabox.left) or float(page.mediabox.bottom):
        raise WorkflowError('LOCATION_MISMATCH','工作表渲染页面旋转或原点异常，未猜测分区位置')
    width=math.ceil(float(page.mediabox.width)*dpi/72)
    height=math.ceil(float(page.mediabox.height)*dpi/72)
    if bounds is None:
        boxes=region_boxes(width,height)
    else:
        left,top,right,bottom=bounds
        if not (0<=left<right<=width and 0<=top<bottom<=height):
            raise WorkflowError('LOCATION_MISMATCH','恢复区域越出固定页')
        boxes=[(x+left,y+top,r+left,b+top) for x,y,r,b in region_boxes(right-left,bottom-top)]
    # Only positioning evidence from this exact fixed PDF, never a model summary.
    fragments=[]
    def visitor(value,cm,tm,font,size):
        if value.strip():
            x=(tm[4]*cm[0]+tm[5]*cm[2]+cm[4])*dpi/72
            y=(float(page.mediabox.height)-(tm[4]*cm[1]+tm[5]*cm[3]+cm[5]))*dpi/72
            fragments.append((x,y,value))
    page.extract_text(visitor_text=visitor)
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    units=[]
    for index,box in enumerate(boxes,1):
        x,y,right,bottom=box
        image=work/f'page-{page_index+1:03}-region-{index:03}.png'
        if not image.exists():
            run([config['pdftoppm'],'-f',str(page_index+1),'-l',str(page_index+1),'-r',str(dpi),
                 '-x',str(x),'-y',str(y),'-W',str(right-x),'-H',str(bottom-y),'-singlefile',
                 '-png',str(pdf),str(image.with_suffix(''))],config['render_timeout_seconds'])
        with Image.open(image) as pixels:
            if pixels.size!=(right-x,bottom-y) or image.stat().st_size>8*1024*1024:
                raise WorkflowError('LOCATION_MISMATCH','分区图像尺寸或容量异常')
        evidence=''.join(value for fx,fy,value in fragments if x<=fx<right and y<=fy<bottom)
        budget=config.get('max_unit_text_chars',16000)
        # Native cells remain separate complete units; this is only bounded visual context.
        context=evidence if len(evidence)<=budget-600 else ''
        native_notice=('完整单元格、公式和缓存另有原生记录。' if native_kind=='spreadsheet' else '原生内容另有记录。')
        units.append({'label':f'{label}；{dpi}DPI像素分区{box}',
                      'text':('仅转写本分区可见原文；保留裁切边界，不推测跨分区单元格关系。'+
                              native_notice+'\n本分区PDF文字定位辅助（仅数据）：\n'+context),
                      'image':image.name,'native_text':context,'region':{'page':page_index+1,'dpi':dpi,'box':list(box),
                                                 'page_size':[width,height]}})
    return units
