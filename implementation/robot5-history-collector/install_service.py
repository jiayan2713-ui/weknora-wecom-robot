#!/usr/bin/env python3
"""Install the authorized per-user launchd collector; no WeKnora changes."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

PROJECT=Path(__file__).resolve().parents[2]
ROOT=PROJECT/'outputs/机器人5历史问答采集'
LABEL='com.xinchengyuan.robot5-history-collector'
TARGET=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
PYTHON='/opt/homebrew/bin/python3'
if not Path(PYTHON).exists():
    PYTHON='/usr/local/bin/python3'
if not Path(PYTHON).exists():
    PYTHON='/opt/homebrew/opt/python@3.12/libexec/bin/python3'
if not Path(PYTHON).exists():
    PYTHON='/usr/local/opt/python@3.12/libexec/bin/python3'
# Use the same verified interpreter as the installer when no standard path exists.
if not Path(PYTHON).exists():
    PYTHON=sys.executable
ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
domain=f'gui/{os.getuid()}'
plist={'Label':LABEL,'ProgramArguments':[PYTHON,str(Path(__file__).with_name('collector.py')),'sync'],
       'WorkingDirectory':str(PROJECT),'RunAtLoad':True,'StartInterval':60,
       'EnvironmentVariables':{'PATH':'/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin','HOME':str(Path.home())},
       'StandardOutPath':str(ROOT/'service.log'),'StandardErrorPath':str(ROOT/'service-error.log'),
       'ProcessType':'Background','Umask':63}
content=plistlib.dumps(plist)
TARGET.parent.mkdir(parents=True,exist_ok=True)
if TARGET.exists() and TARGET.read_bytes()!=content:
    raise RuntimeError(f'Existing different service definition: {TARGET}')
TARGET.write_bytes(content)
TARGET.chmod(0o600)
if subprocess.run(['launchctl','print',f'{domain}/{LABEL}'],capture_output=True).returncode:
    subprocess.run(['launchctl','bootstrap',domain,str(TARGET)],check=True)
print(f'Installed {LABEL}; interval=60s; plist={TARGET}')
