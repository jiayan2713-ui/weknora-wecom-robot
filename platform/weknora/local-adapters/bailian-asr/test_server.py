import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import wave

import server


def wav(seconds):
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b'\0\0' * int(seconds * 16000))
    return b.getvalue()


def multipart(model=server.MODEL):
    return ('multipart/form-data; boundary=proof', (
        '--proof\r\nContent-Disposition: form-data; name="model"\r\n\r\n'+model+
        '\r\n--proof\r\nContent-Disposition: form-data; name="response_format"\r\n\r\nverbose_json'+
        '\r\n--proof\r\nContent-Disposition: form-data; name="file"; filename="../../bad.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        + wav(0.1) + b'\r\n--proof--\r\n')


class AdapterTests(unittest.TestCase):
    def test_multipart_and_unknown_model(self):
        fields, audio = server.parse_multipart(*multipart())
        self.assertEqual(fields['model'], server.MODEL)
        self.assertTrue(audio.startswith(b'RIFF'))
        with self.assertRaises(server.ASRError): server.parse_multipart(*multipart('whisper-1'))

    def test_native_request_shape(self):
        p = server.request_payload(b'abc')
        self.assertEqual(p['input']['messages'][0]['content'][0]['input_audio']['data'], 'data:audio/wav;base64,YWJj')
        self.assertNotIn('response_format', p)
        self.assertEqual(p['parameters']['format'], 'wav')

    def test_both_official_response_shapes(self):
        self.assertEqual(server.extract_text({'output': {'text': '测试'}}), '测试')
        self.assertEqual(server.extract_text({'output': {'output': {'sentence': {'text': '测试'}}}}), '测试')
        with self.assertRaises(server.ASRError): server.extract_text({'output': {}})
        with self.assertRaises(server.ASRError): server.extract_text({'code': 'InvalidApiKey', 'message': 'secret'})

    def test_endpoint_blocks_redirect_targets_and_internal_hosts(self):
        server.validate_endpoint(server.UPSTREAM)
        server.validate_endpoint('https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation')
        for value in ['http://localhost/api', 'https://evil.test/api', 'https://dashscope.aliyuncs.com.evil.test/api', server.UPSTREAM+'?key=secret']:
            with self.assertRaises(ValueError): server.validate_endpoint(value)
        self.assertIsNone(server.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://localhost'))

    def test_long_audio_windows_and_offset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'in.wav'; p.write_bytes(wav(241))
            chunks = list(server.windows(p))
        self.assertEqual([(x[0], x[1]) for x in chunks], [(0, 240), (240, 241)])
        for _, _, data in chunks:
            with wave.open(io.BytesIO(data)) as w: self.assertLessEqual(w.getnframes()/w.getframerate(), 240)

    def test_real_decode_and_merge(self):
        calls = []
        def fake(data, token, language, timeout):
            calls.append((token, language)); return '转写测试'
        r = server.transcribe(wav(0.2), 'test-key', call=fake)
        self.assertEqual(r['text'], '转写测试')
        self.assertEqual(r['timestamp_granularity'], 'audio_window')
        self.assertAlmostEqual(r['duration'], 0.2)
        self.assertEqual(calls, [('test-key', '')])

    def test_later_failure_never_returns_partial_success(self):
        with patch.object(server, 'prepare_audio', return_value=('ignored', 241)), patch.object(server, 'windows', return_value=[(0,240,b'a'),(240,241,b'b')]):
            count = 0
            def fail_second(*args):
                nonlocal count
                count += 1
                if count == 2: raise server.ASRError(502, 'failed', 'failed')
                return 'first'
            with self.assertRaises(server.ASRError): server.transcribe(b'x', 'key', call=fail_second)

    def test_upstream_http_errors_do_not_expose_response(self):
        fake = unittest.mock.Mock()
        fake.open.side_effect = urllib.error.HTTPError(server.UPSTREAM, 401, 'secret-key', {}, io.BytesIO(b'secret'))
        with patch('urllib.request.build_opener', return_value=fake):
            with self.assertRaises(server.ASRError) as caught: server.call_bailian(b'abc', 'key', '', 1)
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn('secret', str(caught.exception))

    def test_invalid_audio_does_not_call_upstream(self):
        with self.assertRaises(server.ASRError): server.transcribe(b'not audio', 'key', call=lambda *a: self.fail('upstream called'))


if __name__ == '__main__': unittest.main()
