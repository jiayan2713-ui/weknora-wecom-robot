"""Authenticated encrypted deployment payloads; unlock keys never enter the bundle."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def openssl():
    candidates = ['/opt/homebrew/opt/openssl@3/bin/openssl',
                  '/usr/local/opt/openssl@3/bin/openssl', shutil.which('openssl')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            result = subprocess.run([candidate, 'enc', '-help'], capture_output=True, text=True)
            if '-saltlen' in result.stdout + result.stderr:
                return candidate
    raise ValueError('需要OpenSSL 3.2以上版本；请安装Homebrew openssl@3')


def authentication(cipher, key_file, metadata):
    key = Path(key_file).read_bytes().strip()
    if len(key) < 64:
        raise ValueError('解锁文件无效')
    derived = hashlib.sha256(b'robot5-mac-bundle-auth-v1\0' + key).digest()
    mac = hmac.new(derived, json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256)
    with Path(cipher).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            mac.update(block)
    return mac.hexdigest()


def encrypt(directory, cipher, key_file, metadata):
    command = [openssl(), 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
               '-md', 'sha256', '-saltlen', '16', '-pass', 'file:' + str(key_file)]
    with Path(cipher).open('wb') as output:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|gz') as archive:
                for path in sorted(Path(directory).rglob('*')):
                    if path.is_symlink():
                        raise ValueError('迁移资料含符号链接，暂停打包')
                    archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
        finally:
            process.stdin.close()
            process.stderr.read()
            process.stderr.close()
            returncode=process.wait()
        if returncode:
            raise ValueError('加密进程失败')
    result = {**metadata, 'format': 'robot5-mac-encrypted-v1'}
    result['authentication'] = authentication(cipher, key_file, result)
    return result


def unpack(cipher, key_file, metadata, destination):
    metadata = dict(metadata)
    expected = metadata.pop('authentication')
    if metadata.get('format') != 'robot5-mac-encrypted-v1' or not hmac.compare_digest(
            authentication(cipher, key_file, metadata), expected):
        raise ValueError('解锁文件不匹配或迁移包已损坏；尚未解密')
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('解密目标目录必须为空，避免覆盖已有资料')
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    plain = destination / '.payload.tar.gz'
    command = [openssl(), 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
               '-md', 'sha256', '-saltlen', '16', '-pass', 'file:' + str(key_file),
               '-in', str(cipher), '-out', str(plain)]
    if subprocess.run(command, capture_output=True).returncode:
        raise ValueError('解密失败，未恢复任何服务')
    with tarfile.open(plain, 'r:gz') as archive:
        for item in archive.getmembers():
            path = destination / item.name
            if not path.resolve().is_relative_to(destination) or not (item.isfile() or item.isdir()):
                raise ValueError('迁移包包含不安全路径或链接')
        archive.extractall(destination, filter='data')
    plain.unlink()
    for path in destination.rglob('*'):
        if path.is_file():
            path.chmod(0o600)

