"""Offline OCR fallback. The caller must configure an installed local environment."""
import os
import subprocess
import sys
from pathlib import Path
from .common import WorkflowError, read_json, write_json


def transcribe_local(python, image, directory, native_text):
    target = Path(directory)/'local-ocr.json'
    if not Path(python).is_file():
        raise WorkflowError('LOCAL_OCR_UNAVAILABLE', '配置的本地OCR环境不存在')
    environment = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ['KEY','TOKEN','SECRET','PASSWORD'])}
    script = Path(__file__).with_name('ocr_worker.py')
    command = ['/usr/bin/sandbox-exec', '-p', '(version 1) (allow default) (deny network*)',
               python, str(script), str(image), str(target)]
    try:
        process = subprocess.run(command, capture_output=True, timeout=180, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowError('LOCAL_OCR_FAILED', '本地OCR无法启动或超时') from error
    if process.returncode:
        raise WorkflowError('LOCAL_OCR_FAILED', '本地OCR失败；未转发资料到其他服务')
    result = read_json(target)
    text = '\n'.join(x['text'] for x in result['lines'])
    return {'body': native_text + '\n\n' + text,
            'notes': '原生文字与本地OCR可能重复；需核对顺序、表格及低置信度文字。',
            'issues': ['本地OCR待人工核对'], 'method': 'local_ocr', 'content_status': 'needs_manual_confirmation'}
