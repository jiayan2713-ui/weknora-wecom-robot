"""Translate WeKnora's multipart ASR requests to Bailian native ASR.

No credentials or audio are persisted. Timestamps describe audio windows,
not word/sentence alignment. Only the explicitly supported model is accepted.
"""
import base64
import email.policy
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = 'qwen-audio-3.0-asr-flash'
MAX_UPLOAD = 50 * 1024 * 1024
MAX_DURATION = 20 * 60
WINDOW_SECONDS = 240
REQUEST_SECONDS = 250
MAX_RESPONSE = 4 * 1024 * 1024
UPSTREAM = os.environ.get('BAILIAN_ASR_ENDPOINT', 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation')
SLOTS = threading.BoundedSemaphore(2)


class ASRError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_endpoint(url):
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ''
    if (p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
            or p.query or p.fragment or not (host in {'dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com'}
                or re.fullmatch(r'[a-zA-Z0-9-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com', host))
            or p.path != '/api/v1/services/aigc/multimodal-generation/generation'):
        raise ValueError('BAILIAN_ASR_ENDPOINT must be an official Bailian native ASR HTTPS endpoint')


def parse_multipart(content_type, body):
    if not content_type.lower().startswith('multipart/form-data;'):
        raise ASRError(415, 'invalid_content_type', 'Expected multipart/form-data')
    msg = BytesParser(policy=email.policy.default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
    if not msg.is_multipart():
        raise ASRError(400, 'invalid_multipart', 'Missing multipart boundary')
    fields, audio = {}, None
    for part in msg.iter_parts():
        name = part.get_param('name', header='content-disposition')
        data = part.get_payload(decode=True) or b''
        if name == 'file':
            if audio is not None:
                raise ASRError(400, 'duplicate_file', 'Exactly one audio file is required')
            audio = data
        elif name in {'model', 'language', 'response_format'}:
            if len(data) > 256:
                raise ASRError(400, 'invalid_field', 'Form field is too long')
            fields[name] = data.decode('utf-8', errors='strict')
    if not audio:
        raise ASRError(400, 'empty_audio', 'Audio file is empty')
    if fields.get('model') != MODEL:
        raise ASRError(400, 'unsupported_model', 'This adapter supports ' + MODEL + ' only')
    if fields.get('response_format', 'json') not in {'json', 'verbose_json'}:
        raise ASRError(400, 'unsupported_format', 'Use json or verbose_json')
    return fields, audio


def run_media(args, timeout):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        raise ASRError(504, 'decode_timeout', 'Audio decoding timed out') from None
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise ASRError(400, 'invalid_audio', 'Cannot decode audio; check the file format and audio track') from None


def prepare_audio(audio, directory):
    source, dest = Path(directory) / 'input.bin', Path(directory) / 'audio.wav'
    source.write_bytes(audio)
    info = run_media(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=duration', '-of', 'json', str(source)], 20)
    try:
        duration = float(json.loads(info)['format']['duration'])
    except (KeyError, ValueError, TypeError):
        raise ASRError(400, 'unknown_duration', 'Cannot determine audio duration') from None
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION:
        raise ASRError(400, 'duration_limit', 'Use an audio file no longer than 20 minutes')
    run_media(['ffmpeg', '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
        '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', '-t', str(MAX_DURATION + 1), str(dest)], 45)
    with wave.open(str(dest), 'rb') as w:
        decoded_duration = w.getnframes() / w.getframerate()
    if decoded_duration > MAX_DURATION:
        raise ASRError(400, 'duration_limit', 'Decoded audio exceeds 20 minutes')
    return dest, decoded_duration


def windows(path):
    with wave.open(str(path), 'rb') as reader:
        rate = reader.getframerate()
        offset = 0
        while True:
            pcm = reader.readframes(WINDOW_SECONDS * rate)
            if not pcm:
                break
            frames = len(pcm) // 2
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(rate)
                writer.writeframes(pcm)
            yield offset / rate, (offset + frames) / rate, buf.getvalue()
            offset += frames


def request_payload(data, language=''):
    params = {'format': 'wav', 'sample_rate': '16000'}
    if language:
        params['language_hints'] = [language]
    return {'model': MODEL, 'input': {'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': 'data:audio/wav;base64,' + base64.b64encode(data).decode()}}
    ]}]}, 'parameters': params}


def extract_text(data):
    if not isinstance(data, dict):
        raise ASRError(502, 'invalid_upstream_response', 'Bailian returned an invalid response')
    if data.get('code'):
        # Never echo upstream messages: they may include request data or credentials.
        raise ASRError(502, 'bailian_error', 'Bailian returned an API error; check model access and region')
    output = data.get('output', {})
    text = output.get('text') if isinstance(output, dict) else None
    if text is None and isinstance(output, dict):
        nested = output.get('output', {})
        sentence = nested.get('sentence', {}) if isinstance(nested, dict) else {}
        text = sentence.get('text') if isinstance(sentence, dict) else None
    if not isinstance(text, str):
        raise ASRError(502, 'invalid_upstream_response', 'Bailian response has no transcription text field')
    return text.strip()


def call_bailian(wav_data, token, language, timeout):
    req = urllib.request.Request(UPSTREAM, data=json.dumps(request_payload(wav_data, language)).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'X-DashScope-SSE': 'disable'})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ASRError(502, 'response_limit', 'Bailian response is too large')
        return extract_text(json.loads(raw))
    except urllib.error.HTTPError as exc:
        status = exc.code if exc.code in {401, 403, 429} else 502
        raise ASRError(status, 'bailian_http_error', f'Bailian HTTP {exc.code}; check API key, model access, region or quota') from None
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise ASRError(504, 'bailian_unreachable', 'Bailian connection failed or timed out') from None
    except (ValueError, UnicodeError):
        raise ASRError(502, 'invalid_upstream_json', 'Bailian returned invalid JSON') from None


def transcribe(audio, token, language='', call=call_bailian):
    deadline = time.monotonic() + REQUEST_SECONDS
    with tempfile.TemporaryDirectory(prefix='bailian-asr-') as tmp:
        path, duration = prepare_audio(audio, tmp)
        segments = []
        for start, end, part in windows(path):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise ASRError(504, 'request_timeout', 'Transcription exceeded time budget; retry with a shorter file')
            text = call(part, token, language, min(90, remaining))
            segments.append({'id': len(segments), 'start': start, 'end': end, 'text': text})
    return {'task': 'transcribe', 'text': '\n'.join(s['text'] for s in segments if s['text']),
            'duration': duration, 'segments': segments, 'timestamp_granularity': 'audio_window'}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # Do not log tokens, request payloads or transcripts.

    def send_json(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        if self.path == '/health':
            self.send_json(200, {'status': 'ok', 'model': MODEL})
        else:
            self.send_json(404, {'error': {'message': 'Not found', 'type': 'invalid_request_error'}})

    def do_POST(self):
        if self.path != '/v1/audio/transcriptions':
            self.send_json(404, {'error': {'message': 'Not found', 'type': 'invalid_request_error'}})
            return
        if not SLOTS.acquire(blocking=False):
            self.send_json(429, {'error': {'message': 'Adapter busy; retry later', 'type': 'rate_limit_error'}})
            return
        started = time.monotonic()
        try:
            token = self.headers.get('Authorization', '')
            if not token.startswith('Bearer ') or not token[7:].strip():
                raise ASRError(401, 'missing_api_key', 'Configure the Bailian API key in WeKnora')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ASRError(400, 'invalid_length', 'Invalid Content-Length') from None
            if not 0 < size <= MAX_UPLOAD:
                raise ASRError(413, 'upload_limit', 'Upload must be between 1 byte and 50 MiB')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ASRError(400, 'incomplete_upload', 'Incomplete audio upload')
            fields, audio = parse_multipart(self.headers.get('Content-Type', ''), raw)
            result = transcribe(audio, token[7:].strip(), fields.get('language', ''))
            self.send_json(200, result)
            print(json.dumps({'event': 'transcribed', 'duration': result['duration'], 'windows': len(result['segments']),
                              'text_chars': len(result['text']), 'elapsed': round(time.monotonic()-started, 2)}), flush=True)
        except ASRError as exc:
            self.send_json(exc.status, {'error': {'message': str(exc), 'type': 'asr_error', 'code': exc.code}})
            print(json.dumps({'event': 'failed', 'code': exc.code, 'status': exc.status}), flush=True)
        except Exception:
            self.send_json(500, {'error': {'message': 'Adapter processing failed', 'type': 'asr_error'}})
        finally:
            SLOTS.release()


if __name__ == '__main__':
    validate_endpoint(UPSTREAM)
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
