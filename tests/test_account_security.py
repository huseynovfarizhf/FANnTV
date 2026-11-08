import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import account_security
import server
from tests_helper import AccountHarness


class AccountSecurityTests(AccountHarness):
    def setUp(self):
        super().setUp()
        self.sent=[]
        self.mail_event=threading.Event()
        def mail(recipient,purpose,token):
            self.sent.append((recipient,purpose,token))
            self.mail_event.set()
        self.mock_mail=patch('account_security.send_account_email',side_effect=mail)
        self.mock_mail.start()

    def tearDown(self):
        self.mock_mail.stop()
        super().tearDown()

    def configure_mail(self):
        os.environ.update(PUBLIC_URL='https://fanntv.example',FANNTV_SMTP_HOST='smtp.example',FANNTV_SMTP_FROM='accounts@fanntv.example')

    def test_email_unavailable_is_clear_and_does_not_enumerate(self):
        self.register()
        for email in ['alex@example.com','unknown@example.com']:
            status,result=self.request('account/forgot',{'email':email})
            self.assertEqual(status,503)
            self.assertEqual(result['error'],'Email delivery is not configured.')
        self.assertEqual(self.request('account/resend-verification',{})[0],503)
        self.assertEqual(self.sent,[])
        self.assertFalse(self.request('me')[1]['email_delivery'])

    def test_verification_single_use_hashed_and_production_owner_gate(self):
        self.configure_mail()
        user=self.register()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0][:2],('alex@example.com','verify'))
        token=self.sent[0][2]
        with server.connect() as db:
            stored=db.execute('SELECT * FROM account_tokens').fetchone()
            self.assertNotEqual(stored['token_hash'],token)
            self.assertEqual(stored['token_hash'],account_security.digest(token))
        os.environ['FANNTV_ENV']='production'
        me=self.request('me')[1]
        self.assertTrue(me['email_verification_required'])
        self.assertFalse(me['user']['email_verified'])
        self.assertFalse(me['user']['access'])
        self.assertEqual(self.request('play/1')[0],403)
        self.assertEqual(self.request('checkout',{'plan':'monthly'})[0],403)
        self.assertEqual(self.request('orders',{'plan':'monthly'})[0],403)
        self.assertEqual(self.request('account/verify',{'token':token})[0],200)
        self.assertTrue(self.request('me')[1]['user']['email_verified'])
        self.assertTrue(self.request('me')[1]['user']['access'])
        self.assertEqual(self.request('account/verify',{'token':token})[0],400)

    def test_expired_or_cross_purpose_tokens_cannot_be_used(self):
        self.configure_mail()
        user=self.register()
        token=self.sent[0][2]
        self.assertEqual(self.request('account/reset',{'token':token,'password':'new-password-123'})[0],400)
        with server.connect() as db:
            db.execute('UPDATE account_tokens SET expires=?',(time.time()-1,))
        self.assertEqual(self.request('account/verify',{'token':token})[0],400)
        self.assertFalse(self.request('me')[1]['user']['email_verified'])

    def test_forgot_response_generic_and_reset_revokes_household(self):
        self.configure_mail()
        self.register()
        self.member()
        child=self.new_client()
        self.request('login',{'email':'child@example.com','password':'test-password-123'},client=child)
        self.sent.clear()
        self.mail_event.clear()
        status,known=self.request('account/forgot',{'email':'alex@example.com'})
        self.assertEqual(status,200)
        self.assertTrue(self.mail_event.wait(2))
        # Wait for the deferred mock delivery, never a real SMTP operation.
        for _ in range(100):
            if self.sent:
                break
            threading.Event().wait(.01)
        status,unknown=self.request('account/forgot',{'email':'missing@example.com'})
        self.assertEqual(status,200)
        self.assertEqual(known,unknown)
        reset=next(item[2] for item in self.sent if item[1]=='reset')
        self.assertEqual(self.request('account/reset',{'token':reset,'password':'changed-password-123'})[0],200)
        self.assertIsNone(self.request('me')[1]['user'])
        self.assertIsNone(self.request('me',client=child)[1]['user'])
        self.assertEqual(self.request('account/reset',{'token':reset,'password':'another-password-123'})[0],400)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'test-password-123'})[0],401)
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'changed-password-123'})[0],200)

    def test_change_password_requires_current_and_revokes_every_session(self):
        self.register()
        other=self.new_client()
        self.request('login',{'email':'alex@example.com','password':'test-password-123'},client=other)
        self.assertEqual(self.request('account/password',{'current_password':'wrong','password':'changed-password-123'})[0],400)
        self.assertEqual(self.request('account/password',{'current_password':'test-password-123','password':'changed-password-123'})[0],200)
        self.assertIsNone(self.request('me')[1]['user'])
        self.assertIsNone(self.request('me',client=other)[1]['user'])
        self.assertEqual(self.request('login',{'email':'alex@example.com','password':'changed-password-123'})[0],200)

    def test_resend_invalidates_old_link_and_limits_are_durable(self):
        self.configure_mail()
        self.register()
        old=self.sent[0][2]
        self.assertEqual(self.request('account/resend-verification',{})[0],200)
        self.assertEqual(self.request('account/verify',{'token':old})[0],400)
        for _ in range(2):
            self.assertEqual(self.request('account/resend-verification',{})[0],200)
        self.assertEqual(self.request('account/resend-verification',{})[0],429)
        with server.connect() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM account_limits').fetchone()[0],0)
            self.assertFalse(any('alex@example.com' in row[0] for row in db.execute('SELECT key FROM account_limits')))

    def test_production_child_inherits_owner_verification_admin_exempt(self):
        owner=self.register()
        self.member()
        child=self.new_client()
        self.request('login',{'email':'child@example.com','password':'test-password-123'},client=child)
        os.environ['FANNTV_ENV']='production'
        self.assertFalse(self.request('me',client=child)[1]['user']['access'])
        with server.connect() as db:
            db.execute('UPDATE users SET email_verified=1 WHERE id=?',(owner['id'],))
        self.assertTrue(self.request('me',client=child)[1]['user']['access'])
        with server.connect() as db:
            db.execute("UPDATE users SET role='admin',email_verified=0 WHERE id=?",(owner['id'],))
        self.assertTrue(self.request('me')[1]['user']['access'])

    def test_failed_delivery_does_not_leak_token_or_auto_verify(self):
        self.configure_mail()
        with patch('account_security.send_account_email',side_effect=RuntimeError('SMTP denied')):
            user=self.register()
        self.assertFalse(user['email_verified'])
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM account_tokens').fetchone()[0],0)

    def test_smtp_uses_tls_and_fragment_link_without_logging(self):
        self.configure_mail()
        os.environ.update(FANNTV_SMTP_USERNAME='mail-user',FANNTV_SMTP_PASSWORD='test-mail-password')
        self.mock_mail.stop()
        try:
            with patch('account_security.smtplib.SMTP') as smtp:
                account_security.send_account_email('alex@example.com','reset','test-token')
                instance=smtp.return_value
                instance.starttls.assert_called_once()
                instance.login.assert_called_once_with('mail-user','test-mail-password')
                message=instance.send_message.call_args.args[0]
                self.assertIn('https://fanntv.example/#reset=test-token',message.get_content())
                self.assertNotIn('?reset=',message.get_content())
        finally:
            self.mock_mail.start()

    def test_provider_io_does_not_hold_writer_lock_and_rechecks_revocation(self):
        user=self.register()
        def provider(channel,account,session_id):
            with server.connect() as db:
                db.execute('PRAGMA busy_timeout=100')
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE users SET suspended=1 WHERE id=?',(user['id'],))
            return {'url':'/media/demo.mp4','expires_at':None}
        with patch('server.playback.resolve_playback_details',side_effect=provider):
            status,result=self.request('play/1')
        self.assertEqual(status,403,result)
        self.assertNotIn('url',result)

    def test_connections_close_and_checkout_json_is_bounded(self):
        with server.connect() as db:
            db.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        import io
        with self.assertRaises(ValueError):
            server.provider_json(io.BytesIO(b' '*32769))
        self.assertFalse(server.secure_provider_url('https://trusted.example@attacker.example/'))
        self.assertFalse(server.secure_provider_url('http://trusted.example/'))

    def test_provider_redirect_handler_rejects_authenticated_redirects(self):
        request=urllib.request.Request('https://bank-adapter.example/start',headers={'Authorization':'Bearer test'})
        handler=server.NoProviderRedirect()
        for target in ['http://bank-adapter.example/plain','https://other.example/steal']:
            self.assertIsNone(handler.redirect_request(request,None,302,'Found',{},target))
        with self.assertRaises(ValueError):
            server.provider_urlopen(urllib.request.Request('http://bank-adapter.example/plain'))


if __name__=='__main__':
    unittest.main()
