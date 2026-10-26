"""Explicit checkpoint recovery; preserve good units and re-read only blocked images."""
import base64
import copy
import json
from pathlib import Path

from markdown_assets import has_image_reference
from docflow.common import digest
from docflow.parser import prompt_blocks, split_notes
from response_adapter import response_parts
from docflow.runtime import recovery_action
from docflow.pipeline import ordered_bounded_map

NO_TEXT = '模型报告无可辨认文字，待人工确认'
MISSING = '模型未返回正文，待人工确认'


def recover_units(units, cache, work, config, client, prompt, *, resolver=None, strict=True):
    def recover(original):
        unit = copy.deepcopy(original)
        if unit.get('method') in ('native_text','native_cells'):return unit
        body = unit.get('body', '')
        issues = unit.get('issues', [])
        if not has_image_reference(body) and NO_TEXT not in issues and MISSING not in issues:
            return unit
        if not unit.get('image'):
            native = unit.get('text')
            if MISSING in issues and isinstance(native, str) and native.strip() and not has_image_reference(native):
                unit.update(body=native, method='native_text', content_state='text_present', issues=[i for i in issues if i != MISSING] +
                            ['原生文本单元直接保留原文；未人工审核'])
                return unit
            raise ValueError('source_recovery_requires_image')
        image = resolver(unit) if resolver else (cache.parent / 'prepared' / unit['image']).resolve()
        if not image.is_file() or (resolver is None and not image.is_relative_to((cache.parent / 'prepared').resolve())):
            raise ValueError('source_recovery_image_invalid')
        # Reuse the durable successful body-format response where one exists.
        prior = work / 'body-fixes' / unit['id'] / 'body-format-recovery-v1'
        if MISSING in issues and (prior / 'call.json').is_file():
            call = json.loads((prior / 'call.json').read_text())
            response = prior / 'response.json'
            if response.is_file() and digest(response) == call.get('response_sha256'):
                recovered, notes = split_notes(client._extract(response.read_bytes())[0])
                recovered = recovered.removeprefix('# 正文').strip()
                if recovered and recovered != '[[DOCFLOW_NO_TEXT]]' and not has_image_reference(recovered):
                    unit.update(body=recovered, notes=notes,
                                issues=[i for i in issues if i != MISSING] + ['复用指纹核验的正文补救响应；未人工审核'])
                    return unit
        from docflow.runtime import current_session
        context=current_session()
        if MISSING in issues and context is not None:
            saved=context.saved_body_response(unit,image,config)
            if saved:
                recovered,notes=response_parts(client._extract(saved.read_bytes())[0])
                if recovered and recovered!='[[DOCFLOW_NO_TEXT]]' and not has_image_reference(recovered):
                    unit.update(body=recovered,notes=notes,content_state='text_present',
                        reused_response_ref={'path':str(saved),'sha256':digest(saved)},
                        issues=[i for i in issues if i!=MISSING]+['按原始文字与图像输入复用已核验正文响应'])
                    return unit
        parent = unit.get('recovery_parent', unit['id'])
        recovery_action(parent, 'read-source-v1')
        core, extra, _ = prompt_blocks(prompt, Path(image).suffix)
        instruction = ('先输出# 正文，只转写这张原始页面或分区中可见的文字和表格。'
                       '不要输出Markdown图片标记，不生成图片地址或占位符；图中可读文字必须转写。'
                       '不要描述无文字照片中的人物、物体或推测业务事实。'
                       '如果整个区域确实没有可辨认文字，正文仅输出[[DOCFLOW_NO_TEXT]]。'
                       '遮挡或裁切处不要补写，疑点写在# 解析记录（非原文）中。')
        messages = [
            {'role': 'system', 'content': core + '\n' + extra + '\n' + instruction},
            {'role': 'user', 'content': [{'type': 'text', 'text': unit.get('label', '') + '\n' + unit.get('text', '')},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()}}]}]
        stage = 'source-text-recovery-v1'
        response = client.call(work / 'source-recovery' / unit['id'], stage, messages)
        recovered, notes = response_parts(response)
        if has_image_reference(recovered):
            # One separately persisted correction, never an unbounded model loop.
            recovery_action(parent, 'correct-format-v1')
            stage = 'source-text-without-image-markup-v1'
            messages[0]['content'] += ('\n必须纠正图片占位格式：只输出原图可见文字。'
                '图标内可见字母只逐字转写，不可读时不要输出Logo或推测文字。'
                '禁止使用感叹号加方括号的图片语法，禁止为图片创造资源路径、地址或占位符。')
            response = client.call(work / 'source-recovery' / unit['id'], stage, messages)
            recovered, notes = response_parts(response)
        if not recovered or has_image_reference(recovered):
            if strict:raise ValueError('source_recovery_body_invalid')
            unit['issues'] = list(unit.get('issues',[]))+[MISSING]
            unit['content_state']='missing_body'
            return unit
        unit['issues'] = [i for i in issues if i not in (NO_TEXT, MISSING)]
        unit['issues'].append('已对原始页面定向复核；未人工审核')
        unit.update(body='' if recovered == '[[DOCFLOW_NO_TEXT]]' else recovered, notes=notes)
        if recovered == '[[DOCFLOW_NO_TEXT]]':
            first = None
            candidates = [cache.parent/unit['id']/'draft',
                work/'body-fixes'/unit['id']/'body-format-recovery-v1',
                work/'xlsx-region-recovery'/unit['id']/'draft',
                work/'derived-transcriptions'/unit['id']/'draft']
            for folder in candidates:
                receipt, saved = folder/'call.json', folder/'response.json'
                if not receipt.is_file() or not saved.is_file():continue
                value = json.loads(receipt.read_text())
                if digest(saved) != value.get('response_sha256'):continue
                original_body, _ = response_parts(client._extract(saved.read_bytes())[0])
                if original_body == '[[DOCFLOW_NO_TEXT]]':
                    first = {'path': str(saved), 'sha256': digest(saved)}
                    break
            if unit.get('native_text', '').strip():
                unit['content_state'] = 'native_visual_conflict'
                unit['issues'].append('原生文字与无文字判断冲突，待核对')
                unit['issues'].append(MISSING)
            elif first is not None:
                response_file = work/'source-recovery'/unit['id']/stage/'response.json'
                unit['no_text_verification'] = {'method': 'second_source_image_read',
                    'image_sha256': digest(image), 'human_review': False,
                    'response_path': str(response_file), 'response_sha256': digest(response_file),
                    'first_response': first, 'verified': True}
                unit['content_state'] = 'no_text_machine_confirmed'
            else:
                unit['issues'].append(NO_TEXT)
                unit['content_state'] = 'no_text_claimed'

        return unit
    return list(ordered_bounded_map(recover, units, config.get('parser_workers', 4)))
