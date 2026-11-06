import hashlib
import hmac
import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB = cls.tmp.name + '/test.db'
        server.initialize()
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), urllib.request.ProxyHandler({}))
        with server.connect() as db:
            for table in ['users', 'sessions', 'favorites', 'logs', 'attempts', 'payments']:
                db.execute('DELETE FROM ' + table)

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + '/api/' + path, data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            res = self.client.open(req)
        except urllib.error.HTTPError as error:
            res = error
        return res.status, json.load(res)

    def register(self):
        status, data = self.request('register', {'name': 'Alex', 'email': 'alex@example.com', 'password': 'test-password-123'})
        self.assertEqual(status, 200)
        return data['user']

    def test_trial_and_playback(self):
        user = self.register()
        self.assertTrue(user['access'])
        self.assertAlmostEqual(user['trial_end'] - time.time(), 6 * 86400, delta=3)
        status, data = self.request('play/1')
        self.assertEqual(status, 200)
        self.assertEqual(data['url'], '/media/demo.mp4')
        with server.connect() as db:
            db.execute('UPDATE users SET created=?', (time.time()-7*86400,))
        self.assertEqual(self.request('play/1')[0], 403)
        self.assertFalse(self.request('me')[1]['user']['access'])

    def test_registration_login_logout(self):
        self.register()
        self.assertEqual(self.request('register', {'name': 'A', 'email': 'alex@example.com', 'password': 'test-password-123'})[0], 409)
        self.assertEqual(self.request('logout', {})[0], 200)
        self.assertEqual(self.request('channels')[0], 401)
        self.assertEqual(self.request('login', {'email': 'alex@example.com', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('login', {'email': 'alex@example.com', 'password': 'test-password-123'})[0], 200)
        with server.connect() as db:
            self.assertNotIn('test-password-123', db.execute('SELECT password FROM users').fetchone()[0])

    def test_favorites_persist(self):
        self.register()
        self.assertEqual(self.request('favorite', {'id': 1, 'active': True})[0], 200)
        self.assertEqual(self.request('channels')[1]['favorites'], [1])
        self.request('favorite', {'id': 1, 'active': False})
        self.assertEqual(self.request('channels')[1]['favorites'], [])

    def test_admin_authorization_and_suspension(self):
        user = self.register()
        self.assertEqual(self.request('admin')[0], 404)
        self.assertEqual(self.request('admin/user', {'id': 99, 'suspended': True})[0], 403)
        with server.connect() as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (user['id'],))
            db.execute('INSERT INTO users(email,name,password,created) VALUES(?,?,?,?)', ('viewer@example.com', 'Viewer', server.password_hash('test-password-123'), time.time()))
            viewer_id = db.execute("SELECT id FROM users WHERE email='viewer@example.com'").fetchone()[0]
        self.assertEqual(self.request('admin')[0], 200)
        self.assertEqual(self.request('admin/user', {'id': user['id'], 'suspended': True})[0], 400)
        self.assertEqual(self.request('admin/user', {'id': viewer_id, 'suspended': True})[0], 200)
        self.request('logout', {})
        self.request('login', {'email': 'viewer@example.com', 'password': 'test-password-123'})
        self.assertEqual(self.request('play/1')[0], 403)

    def test_origin_protection_and_no_payment_bypass(self):
        self.register()
        self.assertEqual(self.request('favorite', {'id': 1, 'active': True}, {'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('checkout', {'plan': 'monthly'})[0], 503)
        self.assertEqual(self.request('stripe/webhook', {})[0], 503)

    def test_verified_invoice_grants_access_once(self):
        user = self.register()
        with server.connect() as db:
            db.execute('UPDATE users SET created=?', (time.time()-7*86400,))
        os.environ['STRIPE_WEBHOOK_SECRET'] = 'test-webhook-secret'
        try:
            event = {'id': 'evt_test', 'type': 'invoice.paid', 'data': {'object': {'parent': {'subscription_details': {'metadata': {'user_id': str(user['id'])}}}, 'lines': {'data': [{'period': {'end': int(time.time()+30*86400)}}]}}}}
            raw = json.dumps(event).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b'test-webhook-secret', timestamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
            self.assertEqual(self.request('stripe/webhook', event, {'Stripe-Signature': f't={timestamp},v1=bad'})[0], 400)
            for _ in range(2):
                self.assertEqual(self.request('stripe/webhook', event, {'Stripe-Signature': f't={timestamp},v1={signature}'})[0], 200)
            self.assertTrue(self.request('me')[1]['user']['access'])
            with server.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM payments').fetchone()[0], 1)
        finally:
            del os.environ['STRIPE_WEBHOOK_SECRET']

    def test_video_range_request(self):
        req = urllib.request.Request(self.base+'/media/demo.mp4', headers={'Range': 'bytes=0-99'})
        with self.client.open(req) as res:
            self.assertEqual(res.status, 206)
            self.assertEqual(len(res.read()), 100)

    def test_login_rate_limit(self):
        for _ in range(20):
            self.assertEqual(self.request('login', {'email': 'missing@example.com', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('login', {'email': 'missing@example.com', 'password': 'wrong'})[0], 429)


if __name__ == '__main__':
    unittest.main()
