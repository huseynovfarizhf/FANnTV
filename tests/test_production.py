import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import playback
import production
import server


class ProductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.original_db = server.DB
        server.DB = str(Path(cls.tmp.name) / 'test.db')
        with patch.dict(os.environ, {'FANNTV_ENV': 'development'}):
            cls.wsgi = importlib.import_module('wsgi')
            cls.app = staticmethod(cls.wsgi.create_application())

    @classmethod
    def tearDownClass(cls):
        server.DB = cls.original_db
        cls.tmp.cleanup()

    def call(self, path='/', method='GET', body=b'', extra=None):
        env = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': '',
               'CONTENT_LENGTH': str(len(body)), 'CONTENT_TYPE': 'application/json',
               'HTTP_HOST': 'app.example', 'REMOTE_ADDR': '127.0.0.1',
               'wsgi.url_scheme': 'https', 'wsgi.input': io.BytesIO(body)}
        env.update(extra or {})
        captured = []
        result = self.app(env, lambda status, headers: captured.append((status, dict(headers))))
        data = b''.join(result)
        if hasattr(result, 'close'):
            result.close()
        return captured[0][0], captured[0][1], data

    def test_production_configuration_fails_closed(self):
        with patch.dict(os.environ, {'FANNTV_ENV': 'production', 'PUBLIC_URL': 'http://unsafe.example', 'FANNTV_DB': 'relative.db'}, clear=True):
            errors = production.configuration_errors()
            self.assertTrue(any('PUBLIC_URL' in e for e in errors))
            self.assertTrue(any('FANNTV_DB' in e for e in errors))
            self.assertTrue(any('SMTP' in e for e in errors))
            with self.assertRaises(RuntimeError):
                production.validate_startup()
        for value in ['https://user:password@app.example', 'https://app.example/path', 'https://app.example/?token=secret']:
            with patch.dict(os.environ, {'PUBLIC_URL': value}):
                self.assertIsNone(production.public_origin())

    def test_mode_normalization_does_not_disable_protection(self):
        import account_security
        for mode in ['production', 'PRODUCTION', ' Production ']:
            with patch.dict(os.environ, {'FANNTV_ENV': mode}, clear=True):
                self.assertTrue(production.enabled())
                self.assertTrue(account_security.production_mode())
                with self.assertRaises(playback.PlaybackError):
                    playback.resolve_playback({'id': 1, 'stream': 'https://provider.example/raw'}, {'id': 1}, 'device')
        with patch.dict(os.environ, {'FANNTV_ENV': 'mistyped-mode'}, clear=True):
            with self.assertRaises(RuntimeError):
                production.validate_startup()

    def test_security_headers_host_origin_and_json(self):
        with patch.dict(os.environ, {'FANNTV_ENV': 'production', 'PUBLIC_URL': 'https://app.example'}):
            status, headers, _ = self.call('/api/me')
            self.assertEqual(status, '200 OK')
            self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
            self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(self.call('/api/me', extra={'HTTP_HOST': 'attacker.example'})[0], '400 Bad Request')
            self.assertEqual(self.call('/api/login', 'POST', b'{}', {'HTTP_ORIGIN': 'https://attacker.example'})[0], '403 Forbidden')
            self.assertEqual(self.call('/api/login', 'POST', b'{}', {'CONTENT_TYPE': 'text/plain'})[0], '415 Unsupported Media Type')
            self.assertEqual(self.call('/api/me', extra={'wsgi.url_scheme': 'http'})[0], '400 Bad Request')

    def test_readiness_head_ranges_and_body_limits(self):
        self.assertEqual(self.call('/healthz')[0], '200 OK')
        self.assertEqual(self.call('/readyz')[0], '200 OK')
        status, headers, body = self.call('/', 'HEAD')
        self.assertEqual(status, '200 OK')
        self.assertTrue(int(headers['Content-Length']) > 0)
        self.assertEqual(body, b'')
        self.assertEqual(self.call('/api/login', 'POST', b'', {'CONTENT_LENGTH': '32769'})[0], '413 Request Entity Too Large')
        self.assertEqual(self.call('/api/login', 'POST', b'', {'CONTENT_LENGTH': '5'})[0], '400 Bad Request')
        self.assertEqual(self.call('/api/me', 'DELETE')[0], '405 Method Not Allowed')
        status, headers, body = self.call('/alpine.jpg', extra={'HTTP_RANGE': 'bytes=0-99'})
        self.assertEqual(status, '206 Partial Content')
        self.assertEqual(len(body), 100)

    def test_trusted_proxy_only_accepts_one_valid_ip(self):
        with patch.dict(os.environ, {'FANNTV_TRUSTED_PROXIES': '127.0.0.1'}):
            self.assertEqual(production.client_address({'REMOTE_ADDR': '127.0.0.1', 'HTTP_X_FORWARDED_FOR': '203.0.113.7'}), '203.0.113.7')
            self.assertEqual(production.client_address({'REMOTE_ADDR': '203.0.113.8', 'HTTP_X_FORWARDED_FOR': '203.0.113.7'}), '203.0.113.8')
            self.assertEqual(production.client_address({'REMOTE_ADDR': '127.0.0.1', 'HTTP_X_FORWARDED_FOR': '203.0.113.7, 127.0.0.1'}), '127.0.0.1')

    def test_commit_failure_cannot_acknowledge_success(self):
        class CommitFailure:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                raise RuntimeError('sensitive-provider-token-do-not-log')
        def handler(request):
            with CommitFailure():
                request.send_json({'ok': True})
        with patch.object(server.Handler, 'do_POST', handler), self.assertLogs('fanntv', 'ERROR') as logs:
            status, headers, body = self.call('/api/example', 'POST', b'{}')
        self.assertEqual(status, '500 Internal Server Error')
        self.assertNotIn(b'"ok": true', body)
        self.assertNotIn('sensitive-provider-token', ' '.join(logs.output))
        self.assertIn('request_id', json.loads(body))

    def test_production_playback_requires_protected_provider(self):
        channel = {'id': 1, 'stream': 'https://provider.example/permanent.m3u8'}
        with patch.dict(os.environ, {'FANNTV_ENV': 'production'}, clear=True):
            with self.assertRaises(playback.PlaybackError):
                playback.resolve_playback(channel, {'id': 1}, 'opaque-session')
        with patch.dict(os.environ, {'FANNTV_ENV': 'development'}, clear=True):
            self.assertEqual(playback.resolve_playback(channel, {'id': 1}, 'opaque-session'), channel['stream'])

    def test_signed_playback_expiry_and_url_validation(self):
        channel = {'id': 2, 'stream': 'https://provider.example/source'}
        config = {'FANNTV_ENV': 'production', 'FANNTV_PLAYBACK_ADAPTER_URL': 'https://adapter.example/play', 'FANNTV_PLAYBACK_ADAPTER_KEY': 'never-return-this-key'}
        for response, valid in [({'url': 'https://cdn.example/token', 'expires_at': 1090}, True),
                                ({'url': 'http://cdn.example/token', 'expires_at': 1090}, False),
                                ({'url': 'https://cdn.example/token', 'expires_at': 1500}, False),
                                ({'url': 'https://cdn.example/token', 'expires_at': 999}, False),
                                ({'url': 'https://cdn.example/token', 'expires_at': 1020}, False),
                                ({'url': 'https://user:password@cdn.example/token', 'expires_at': 1090}, False)]:
            with patch.dict(os.environ, config, clear=True), patch('playback.time.time', return_value=1000), patch('playback.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
                if valid:
                    result = playback.resolve_playback_details(channel, {'id': 4}, 'opaque-device')
                    self.assertEqual(result, response)
                else:
                    with self.assertRaises(playback.PlaybackError):
                        playback.resolve_playback(channel, {'id': 4}, 'opaque-device')
        self.assertIsNone(playback.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://other.example'))


if __name__ == '__main__':
    unittest.main()
