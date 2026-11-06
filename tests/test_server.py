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


class ApplicationTests(unittest.TestCase):
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
        for key in ['STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','PUBLIC_URL','FANNTV_BANK_ADAPTER_URL','FANNTV_BANK_ADAPTER_KEY','FANNTV_PAYMENT_WEBHOOK_SECRET']:
            os.environ.pop(key,None)
        with server.connect() as db:
            for table in ['users','sessions','favorites','logs','attempts','payments','orders','receipts','price_overrides','plans']:
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
            return response.status,json.load(response)

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

    def test_trial_playback_expiry_and_protected_demo(self):
        user=self.register()
        self.assertTrue(user['access'])
        self.assertAlmostEqual(user['trial_end']-time.time(),6*86400,delta=3)
        self.assertEqual(self.request('play/1')[1]['url'],'/media/demo.mp4')
        req=urllib.request.Request(self.base+'/media/demo.mp4',headers={'Range':'bytes=0-99'})
        with self.client.open(req) as response:
            self.assertEqual(response.status,206)
            self.assertEqual(len(response.read()),100)
        for path in ['/demo.mp4','/x/../demo.mp4']:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.open(self.base+path)
            self.assertEqual(error.exception.code,404)
            error.exception.close()
        with server.connect() as db:
            db.execute('UPDATE users SET created=?',(time.time()-7*86400,))
        self.assertEqual(self.request('play/1')[0],403)
        self.assertEqual(self.request('play/heartbeat',{'channel_id':1})[0],403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.client.open(req)
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_registration_login_logout(self):
        self.register()
        self.assertEqual(self.request('register',{'name':'A','email':'alex@example.com','password':'test-password-123'})[0],409)
        self.request('logout',{})
        self.assertEqual(self.request('channels')[0],401)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'wrong'})[0],401)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'})[0],200)
        with server.connect() as db:
            self.assertNotIn('test-password-123',db.execute('SELECT password FROM users').fetchone()[0])

    def test_favorites_and_azn_catalog(self):
        self.register()
        self.assertEqual(self.request('favorite',{'id':1,'active':True})[0],200)
        self.assertEqual(self.request('channels')[1]['favorites'],[1])
        self.request('favorite',{'id':1,'active':False})
        self.assertEqual(self.request('channels')[1]['favorites'],[])
        plans=self.request('me')[1]['plans']
        self.assertEqual([p['months'] for p in plans],[1,6,12])
        self.assertTrue(all(p['currency']=='AZN' and type(p['amount_minor']) is int for p in plans))

    def test_admin_and_manual_payment_idempotency(self):
        admin=self.admin()
        viewer=self.new_client()
        user=self.register('viewer@example.com',viewer)
        self.assertEqual(self.request('admin',client=viewer)[0],404)
        receipt={'user_id':user['id'],'plan':'six_month','reference':'bank-verified-1','amount_minor':2499}
        self.assertEqual(self.request('admin/payment',receipt,client=viewer)[0],403)
        status,result=self.request('admin/payment',receipt)
        self.assertEqual(status,200,result)
        self.assertTrue(result['user']['bonus_until']>time.time())
        end=result['user']['paid_until']
        self.assertEqual(self.request('admin/payment',receipt)[1]['user']['paid_until'],end)
        self.assertEqual(self.request('admin/payment',{**receipt,'user_id':admin['id']})[0],409)
        self.assertEqual(self.request('admin/payment',{**receipt,'reference':'bank-verified-2','amount_minor':1})[0],409)
        self.assertEqual(self.request('admin/user',{'id':admin['id'],'suspended':True})[0],400)
        self.assertEqual(self.request('admin/user',{'id':user['id'],'suspended':True})[0],200)
        self.assertEqual(self.request('play/1',client=viewer)[0],403)

    def test_global_and_customer_prices_freeze_orders(self):
        self.admin()
        client=self.new_client()
        user=self.register('viewer@example.com',client)
        self.assertEqual(self.request('admin/pricing',{'plan':'monthly','user_id':None,'amount_minor':900})[0],200)
        self.assertEqual(self.request('me',client=client)[1]['plans'][0]['amount_minor'],900)
        self.request('admin/pricing',{'plan':'monthly','user_id':user['id'],'amount_minor':750})
        self.assertEqual(self.request('me',client=client)[1]['plans'][0]['amount_minor'],750)
        order=self.order(user['id'])
        self.request('admin/pricing',{'plan':'monthly','user_id':user['id'],'amount_minor':1100})
        self.settle(order)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT amount_minor FROM orders WHERE id=?',(order['id'],)).fetchone()[0],750)
        self.request('admin/pricing',{'plan':'monthly','user_id':user['id'],'amount_minor':None})
        self.assertEqual(self.request('me',client=client)[1]['plans'][0]['amount_minor'],900)
        for invalid in [True,1.5,0,-1,10000001]:
            self.assertEqual(self.request('admin/pricing',{'plan':'monthly','amount_minor':invalid})[0],400)

    def test_signed_payment_auto_activation_and_replays(self):
        user=self.register()
        with server.connect() as db:
            db.execute('UPDATE users SET created=?',(time.time()-7*86400,))
        order=self.order(user['id'],'six_month')
        event={'id':'evt_1','order_id':order['id'],'transaction_id':'tx_1','status':'paid','amount_minor':2499,'currency':'AZN'}
        for change in [{'amount_minor':1},{'currency':'USD'},{'amount_minor':True}]:
            self.assertEqual(self.signed({**event,**change})[0],400)
        self.assertFalse(self.request('me')[1]['user']['access'])
        self.assertEqual(self.signed(event)[0],200)
        after=self.request('me')[1]['user']
        self.assertTrue(after['access'])
        self.assertTrue(after['parental_until']>time.time())
        self.assertEqual(self.signed(event)[0],200)
        self.assertEqual(self.request('me')[1]['user']['paid_until'],after['paid_until'])
        other=self.order(user['id'],'six_month')
        self.assertEqual(self.signed({**event,'id':'evt_2','order_id':other['id']})[0],409)
        self.assertEqual(self.signed({**event,'id':'evt_3','transaction_id':'other_tx'})[0],409)
        self.assertEqual(self.signed({**event,'transaction_id':'different_tx'})[0],409)

    def test_invalid_webhook_and_no_success_url_bypass(self):
        self.register()
        self.assertEqual(self.request('checkout',{'plan':'monthly'})[0],503)
        self.assertEqual(self.request('payments/webhook',{})[0],503)
        os.environ['FANNTV_PAYMENT_WEBHOOK_SECRET']='test-webhook-secret'
        self.assertEqual(self.request('payments/webhook',{}, {'X-FANNTV-Timestamp':str(int(time.time())),'X-FANNTV-Signature':'bad'})[0],400)
        self.assertEqual(self.request('favorite',{'id':1,'active':True},{'Origin':'https://attacker.example'})[0],403)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT paid_until FROM users').fetchone()[0],0)

    def test_four_household_sessions_reuse_and_revoke(self):
        self.register()
        clients=[self.new_client() for _ in range(4)]
        for client in clients[:3]:
            self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'},client=client)[0],200)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'},client=clients[3])[0],409)
        # Reauthentication in an existing browser must not consume a fifth slot.
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'})[0],200)
        family=self.request('family')[1]
        self.assertEqual(len(family['devices']),4)
        self.assertTrue(all('token' not in device and len(device['id'])<40 for device in family['devices']))
        target=next(device for device in family['devices'] if not device['current'])
        self.assertEqual(self.request('family/device',{'id':target['id']})[0],200)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'},client=clients[3])[0],200)

    def test_family_roles_parental_enforcement_and_expiry(self):
        user=self.register()
        status,result=self.member()
        self.assertEqual(status,200,result)
        self.assertEqual(self.member('extra@example.com')[0],403)
        child=self.new_client()
        self.assertEqual(self.request('login',{'email':'child@example.com','password':'test-password-123'},client=child)[0],200)
        self.assertEqual(self.request('family',client=child)[0],403)
        self.assertEqual(self.request('checkout',{'plan':'monthly'},client=child)[0],403)
        self.assertEqual(self.request('admin/payment',{},client=child)[0],403)
        self.settle(self.order(user['id'],'six_month'))
        member_id=self.request('family')[1]['members'][0]['id']
        status,result=self.request('family/member',{'id':member_id,'allowed_categories':['Kids']})
        self.assertEqual(status,200,result)
        self.assertEqual([c['category'] for c in self.request('channels',client=child)[1]['channels']],['Kids'])
        self.assertEqual(self.request('play/1',client=child)[0],404)
        self.assertEqual(self.request('play/heartbeat',{'channel_id':1},client=child)[0],404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            child.open(self.base+'/media/demo.mp4')
        self.assertEqual(error.exception.code,403)
        error.exception.close()
        self.assertEqual(self.member('extra@example.com')[0],200)
        bonus=self.new_client()
        self.assertEqual(self.request('login',{'email':'extra@example.com','password':'test-password-123'},client=bonus)[0],200)
        with server.connect() as db:
            db.execute('UPDATE users SET bonus_until=?,parental_until=? WHERE id=?',(time.time()-1,time.time()-1,user['id']))
        self.assertFalse(self.request('me',client=bonus)[1]['user']['access'])
        self.assertEqual(self.request('play/1',client=bonus)[0],403)
        self.assertEqual(self.request('family/member',{'id':member_id,'allowed_categories':None})[0],403)
        # Non-parental edits after expiry preserve restrictions when the field is omitted.
        self.assertEqual(self.request('family/member',{'id':member_id,'name':'New name'})[0],200)
        self.assertEqual(self.request('channels',client=child)[1]['channels'][0]['category'],'Kids')

    def test_owner_suspension_propagates_and_cross_family_access_denied(self):
        owner=self.register()
        self.member()
        child=self.new_client()
        self.request('login',{'email':'child@example.com','password':'test-password-123'},client=child)
        other=self.new_client()
        self.register('other@example.com',other)
        family=self.request('family')[1]
        self.assertEqual(self.request('family/device',{'id':family['current_session_id']},client=other)[0],404)
        self.assertEqual(self.request('family/member',{'id':family['members'][0]['id'],'name':'hack'},client=other)[0],404)
        with server.connect() as db:
            db.execute('UPDATE users SET suspended=1 WHERE id=?',(owner['id'],))
        self.assertEqual(self.request('play/1',client=child)[0],403)
        self.assertEqual(self.request('play/heartbeat',{'channel_id':1},client=child)[0],403)

    def test_six_calendar_months_and_independent_bonus(self):
        user=self.register()
        now=time.time()
        annual=self.order(user['id'],'yearly')
        self.settle(annual)
        self.assertEqual(self.owner_row(user['id'])['bonus_until'],0)
        self.settle(self.order(user['id'],'six_month'),'tx2')
        row=self.owner_row(user['id'])
        self.assertAlmostEqual(row['bonus_until'],server.add_months(now,6),delta=3)
        self.assertTrue(row['paid_until']>row['bonus_until']+300*86400)
        jan31=datetime(2027,1,31,15,tzinfo=ZoneInfo('Asia/Baku')).timestamp()
        feb=datetime.fromtimestamp(server.add_months(jan31,1),ZoneInfo('Asia/Baku'))
        self.assertEqual((feb.year,feb.month,feb.day,feb.hour),(2027,2,28,15))

    def test_single_platform_admin_and_additive_migration(self):
        self.admin()
        with server.connect() as db:
            with self.assertRaises(__import__('sqlite3').IntegrityError):
                db.execute("INSERT INTO users(email,name,password,role,created) VALUES('second@example.com','Second','hash','admin',?)",(time.time(),))
        # Reinitialization preserves accounts/prices and does not reset durable state.
        with server.connect() as db:
            db.execute("UPDATE plans SET amount_minor=777 WHERE id='monthly'")
        server.initialize()
        self.assertEqual(self.request('me')[1]['plans'][0]['amount_minor'],777)

    def test_editing_member_duplicate_email_is_safe(self):
        self.register()
        self.member()
        member=self.request('family')[1]['members'][0]
        self.assertEqual(self.request('family/member',{'id':member['id'],'email':'alex@example.com'})[0],409)
        self.assertEqual(self.request('family')[1]['members'][0]['email'],'child@example.com')

    def test_reauthentication_rotates_cookie_but_preserves_device(self):
        self.register()
        before=self.request('family')[1]['current_session_id']
        with server.connect() as db:
            old=db.execute('SELECT token FROM sessions').fetchone()[0]
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'})[0],200)
        self.assertEqual(self.request('family')[1]['current_session_id'],before)
        with server.connect() as db:
            self.assertNotEqual(db.execute('SELECT token FROM sessions').fetchone()[0],old)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)
        self.assertEqual(server.device_label('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1'),'iPhone · Safari')
        self.assertEqual(server.device_label('Mozilla/5.0 (Linux; Android 12; TV) AppleWebKit/537.36 Chrome/120.0 Safari/537.36'),'Android TV · Chrome')

    def test_concurrent_logins_cannot_exceed_four_devices(self):
        self.register()
        clients=[self.new_client() for _ in range(5)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda c:self.request('login',{'email':'alex@example.com','password':'test-password-123'},client=c)[0],clients))
        self.assertEqual(results.count(200),3)
        self.assertEqual(results.count(409),2)
        self.assertEqual(len(self.request('family')[1]['devices']),4)

    def test_trusted_bank_adapter_and_payment_confirmation(self):
        user=self.register()
        os.environ.update(PUBLIC_URL='https://app.example',FANNTV_BANK_ADAPTER_URL='https://trusted-adapter.example/checkout',FANNTV_BANK_ADAPTER_KEY='test-adapter-key',FANNTV_PAYMENT_WEBHOOK_SECRET='test-webhook-secret')
        captured=[]
        def bank(req,timeout):
            captured.append(json.loads(req.data))
            self.assertEqual(req.headers['Authorization'],'Bearer test-adapter-key')
            return io.BytesIO(json.dumps({'checkout_url':'https://bank.example/hosted/payment','session_id':'bank-session-1'}).encode())
        with patch('server.urllib.request.urlopen',side_effect=bank):
            status,data=self.request('checkout',{'plan':'six_month','amount_minor':1,'currency':'USD'})
        self.assertEqual(status,200,data)
        self.assertEqual(data['url'],'https://bank.example/hosted/payment')
        self.assertEqual(captured[0]['amount_minor'],2499)
        self.assertEqual(captured[0]['currency'],'AZN')
        self.assertNotIn('card_number',captured[0])
        self.assertNotIn('cvv',captured[0])
        self.assertEqual(self.owner_row(user['id'])['paid_until'],0)
        event={'id':'bank_evt_1','order_id':data['order_id'],'transaction_id':'bank_tx_1','status':'paid','amount_minor':2499,'currency':'AZN'}
        self.assertEqual(self.signed(event)[0],200)
        self.assertTrue(self.owner_row(user['id'])['paid_until']>time.time())
        self.assertEqual(self.request('orders')[1]['orders'][0]['status'],'paid')
        with patch('server.urllib.request.urlopen',return_value=io.BytesIO(b'{"checkout_url":"javascript:alert(1)"}')):
            self.assertEqual(self.request('checkout',{'plan':'monthly'})[0],502)

    def test_stripe_paid_checkout_requires_verified_session_and_quote(self):
        user=self.register()
        os.environ.update(PUBLIC_URL='https://app.example',STRIPE_SECRET_KEY='test-stripe-key',STRIPE_WEBHOOK_SECRET='test-webhook-secret')
        captured=[]
        def provider(req,timeout):
            captured.append(urllib.parse.parse_qs(req.data.decode()))
            return io.BytesIO(b'{"id":"cs_verified","url":"https://checkout.stripe.com/test"}')
        with patch('server.urllib.request.urlopen',side_effect=provider):
            status,data=self.request('checkout',{'plan':'six_month','amount_minor':1})
        self.assertEqual(status,200,data)
        self.assertEqual(captured[0]['line_items[0][price_data][unit_amount]'],['2499'])
        self.assertEqual(captured[0]['line_items[0][price_data][currency]'],['azn'])
        event={'id':'evt_1','type':'checkout.session.completed','data':{'object':{'id':'cs_verified','payment_intent':'pi_1','payment_status':'paid','mode':'payment','metadata':{'order_id':data['order_id']},'amount_total':2499,'currency':'azn'}}}
        def stripe(e):
            raw=json.dumps(e).encode()
            timestamp=str(int(time.time()))
            signature=hmac.new(b'test-webhook-secret',timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
            return self.request('stripe/webhook',e,{'Stripe-Signature':f't={timestamp},v1={signature}'})
        bad=json.loads(json.dumps(event))
        bad['data']['object']['id']='cs_other'
        self.assertEqual(stripe(bad)[0],400)
        self.assertEqual(stripe(event)[0],200)
        until=self.owner_row(user['id'])['paid_until']
        self.assertEqual(stripe(event)[0],200)
        self.assertEqual(self.owner_row(user['id'])['paid_until'],until)

    def test_login_rate_limit(self):
        for _ in range(20):
            self.assertEqual(self.request('login',{'email':'missing@example.com','password':'wrong'})[0],401)
        self.assertEqual(self.request('login',{'email':'missing@example.com','password':'wrong'})[0],429)


if __name__=='__main__':
    unittest.main()
