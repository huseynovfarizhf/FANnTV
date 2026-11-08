import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
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
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from zoneinfo import ZoneInfo

import server


class AccountHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        server.DB=cls.tmp.name+'/test.db'
        server.initialize()
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.client=self.new_client()
        self.env=patch.dict(os.environ,{},clear=False)
        self.env.start()
        for key in ['STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','PUBLIC_URL','FANNTV_BANK_ADAPTER_URL','FANNTV_BANK_ADAPTER_KEY','FANNTV_PAYMENT_WEBHOOK_SECRET','FANNTV_ENV','FANNTV_SMTP_HOST','FANNTV_SMTP_FROM','FANNTV_SMTP_USERNAME','FANNTV_SMTP_PASSWORD','FANNTV_SMTP_MODE','FANNTV_SMTP_PORT']:
            os.environ.pop(key,None)
        with server.connect() as db:
            for table in ['users','sessions','favorites','logs','attempts','payments','orders','receipts','price_overrides','plans','account_tokens','account_limits']:
                db.execute('DELETE FROM '+table)
            db.executemany('INSERT INTO plans VALUES(?,?,?,?)',server.PLAN_SPECS)

    def tearDown(self):
        self.env.stop()

    def new_client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))

    def request(self,path,body=None,headers=None,client=None):
        req=urllib.request.Request(self.base+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        try:
            response=(client or self.client).open(req)
        except urllib.error.HTTPError as error:
            response=error
        with response:
            result=(response.status,json.load(response))
        # These integration fixtures use loopback HTTP even when exercising HTTPS deployment configuration.
        for handler in (client or self.client).handlers:
            if hasattr(handler,'cookiejar'):
                for cookie in handler.cookiejar:
                    cookie.secure=False
        return result

    def register(self,email='alex@example.com',client=None):
        status,data=self.request('register',{'name':'Alex','email':email,'password':'test-password-123'},client=client)
        self.assertEqual(status,200,data)
        return data['user']

    def admin(self):
        user=self.register('admin@example.com')
        with server.connect() as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
        return user

    def owner_row(self,uid):
        with server.connect() as db:
            return db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()

    def order(self,uid,plan='monthly'):
        with server.connect() as db:
            return dict(server.create_order(db,uid,plan))

    def settle(self,order,tx='tx1'):
        with server.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return server.settle_order(db,order['id'],tx,'evt:'+tx,'test',order['amount_minor'],'AZN')

    def member(self,email='child@example.com',**kwargs):
        return self.request('family/member',dict(name='Child',email=email,password='test-password-123',**kwargs))

    def signed(self,event,path='payments/webhook',**kwargs):
        raw=json.dumps(event).encode()
        timestamp=str(int(time.time()))
        signature=hmac.new(b'test-webhook-secret',timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        os.environ['FANNTV_PAYMENT_WEBHOOK_SECRET']='test-webhook-secret'
        return self.request(path,event,{'X-FANNTV-Timestamp':timestamp,'X-FANNTV-Signature':signature},**kwargs)
