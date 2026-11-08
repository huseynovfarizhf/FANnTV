import argparse
import calendar
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import account_security
import playback

ROOT = Path(__file__).parent
DB = os.environ.get('FANNTV_DB', str(ROOT / 'instance' / 'fanntv.db'))
TRIAL = 6 * 86400
SESSION_LIFETIME = 30 * 86400
DEVICE_LIMIT = 4
CATEGORIES = ['News', 'Sports', 'Movies', 'Nature', 'Kids', 'Travel']
PLAN_SPECS = [('monthly', 'Monthly', 1, 499), ('six_month', 'Six months', 6, 2499), ('yearly', 'Yearly', 12, 3999)]


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type,exc_value,traceback)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DB, timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()


def verify(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


def initialize():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, password TEXT, role TEXT DEFAULT 'user', created REAL, paid_until REAL DEFAULT 0, suspended INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT, category TEXT, title TEXT, description TEXT, art TEXT, stream TEXT, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, channel_id INTEGER, PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, user_id INTEGER, event TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY, count INTEGER, reset REAL);
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, name TEXT, months INTEGER, amount_minor INTEGER);
        CREATE TABLE IF NOT EXISTS price_overrides(user_id INTEGER, plan TEXT, amount_minor INTEGER, PRIMARY KEY(user_id,plan));
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, months INTEGER, amount_minor INTEGER, currency TEXT DEFAULT 'AZN', status TEXT DEFAULT 'pending', created REAL, paid_at REAL, reference TEXT UNIQUE, provider_session TEXT);
        CREATE TABLE IF NOT EXISTS receipts(transaction_id TEXT PRIMARY KEY, order_id TEXT UNIQUE, event_id TEXT UNIQUE, provider TEXT, created REAL);
        ''')
        additions = {
            'users': [('owner_id', 'INTEGER'), ('parental_until', 'REAL DEFAULT 0'), ('bonus_until', 'REAL DEFAULT 0'), ('bonus', 'INTEGER DEFAULT 0'), ('allowed_categories', 'TEXT'), ('email_verified', 'INTEGER NOT NULL DEFAULT 0')],
            'sessions': [('id', 'TEXT'), ('label', "TEXT DEFAULT 'Browser'"), ('last_seen', 'REAL DEFAULT 0')],
        }
        for table, columns in additions.items():
            present = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            for column, declaration in columns:
                if column not in present:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
        account_security.initialize(db)
        for row in db.execute('SELECT token FROM sessions WHERE id IS NULL').fetchall():
            db.execute('UPDATE sessions SET id=? WHERE token=?', (secrets.token_urlsafe(12), row['token']))
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_public_id ON sessions(id)')
        db.execute('CREATE INDEX IF NOT EXISTS sessions_user_expiry ON sessions(user_id,expires)')
        # Existing databases with multiple administrators require operator review.
        if db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] > 1:
            raise RuntimeError('Only one platform administrator is supported. Review existing administrator accounts before migration.')
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_platform_admin ON users(role) WHERE role='admin'")
        db.executemany('INSERT OR IGNORE INTO plans VALUES(?,?,?,?)', PLAN_SPECS)
        if not db.execute('SELECT 1 FROM channels LIMIT 1').fetchone():
            db.executemany('INSERT INTO channels(name,category,title,description,art,stream) VALUES(?,?,?,?,?,?)', [
                ('NATURE+', 'Nature', 'The wild is calling', 'Explore extraordinary places.', 'nature', '/media/demo.mp4'),
                ('WORLD NEWS', 'News', 'A wider perspective', 'A world of stories.', 'news', ''),
                ('ARENA', 'Sports', 'Every moment matters', 'Get closer to the action.', 'sports', ''),
                ('CINEMA', 'Movies', 'Stay for the story', 'Discover your next favorite film.', 'cinema', ''),
                ('LITTLE STARS', 'Kids', 'Big little adventures', 'A world of imagination.', 'kids', ''),
                ('HORIZON', 'Travel', 'Somewhere new', 'Take the scenic route.', 'travel', ''),
            ])


def audit(db, uid, event):
    db.execute('INSERT INTO logs(user_id,event,created) VALUES(?,?,?)', (uid, event, time.time()))


def owner_for(db, u):
    if u['owner_id']:
        return db.execute('SELECT * FROM users WHERE id=? AND owner_id IS NULL', (u['owner_id'],)).fetchone()
    return u


def public_user(u, db=None):
    if db is None:
        with connect() as db:
            return public_user(u, db)
    owner = owner_for(db, u)
    now = time.time()
    if owner is None:
        raise RequestError('Account owner not found.', 403)
    trial_end = owner['created'] + TRIAL
    active = not u['bonus'] or owner['bonus_until'] > now
    email_verified = bool(owner['email_verified'])
    requires_verification = account_security.production_mode() and owner['role'] != 'admin' and not email_verified
    access = not requires_verification and active and not u['suspended'] and not owner['suspended'] and (owner['role'] == 'admin' or max(trial_end, owner['paid_until']) > now)
    return dict(id=u['id'], email=u['email'], name=u['name'], role=u['role'], owner_id=owner['id'],
                account_role='platform_admin' if u['role'] == 'admin' else 'subuser' if u['owner_id'] else 'owner',
                trial_end=trial_end, paid_until=owner['paid_until'], suspended=bool(u['suspended'] or owner['suspended']), access=bool(access), active=bool(active),
                parental_until=owner['parental_until'], bonus_until=owner['bonus_until'], subuser_limit=2 if owner['bonus_until'] > now else 1,
                email_verified=email_verified, own_email_verified=bool(u['email_verified']), requires_email_verification=requires_verification,
                can_manage_family=not u['owner_id'] and not u['suspended'])


def ensure_owner(u):
    if u['owner_id'] or u['suspended']:
        raise RequestError('Only the account owner can manage the family or payments.', 403)


def plans_for(db, owner_id=None):
    rows = []
    for r in db.execute('SELECT * FROM plans ORDER BY months'):
        plan = dict(r)
        override = db.execute('SELECT amount_minor FROM price_overrides WHERE user_id=? AND plan=?', (owner_id, r['id'])).fetchone()
        if override:
            plan['amount_minor'] = override[0]
        plan['currency'] = 'AZN'
        rows.append(plan)
    return rows


def add_months(timestamp, months):
    dt = datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Baku'))
    month = dt.month - 1 + months
    year, month = dt.year + month // 12, month % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1])).timestamp()


def create_order(db, uid, plan_id):
    owner = db.execute('SELECT * FROM users WHERE id=? AND owner_id IS NULL', (uid,)).fetchone()
    if not owner or owner['suspended']:
        raise RequestError('Account owner not found or suspended.', 400)
    plan = next((p for p in plans_for(db, uid) if p['id'] == plan_id), None)
    if not plan:
        raise RequestError('Invalid service package.')
    oid = secrets.token_urlsafe(18)
    db.execute('INSERT INTO orders(id,user_id,plan,months,amount_minor,created) VALUES(?,?,?,?,?,?)', (oid, uid, plan_id, plan['months'], plan['amount_minor'], time.time()))
    return db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()


def settle_order(db, oid, transaction_id, event_id, provider, amount_minor, currency):
    order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order:
        raise RequestError('Order not found.', 404)
    if type(amount_minor) is not int or order['amount_minor'] != amount_minor or currency.upper() != 'AZN':
        raise RequestError('Payment amount or currency does not match the order.')
    receipt = db.execute('SELECT * FROM receipts WHERE transaction_id=? OR event_id=?', (transaction_id, event_id)).fetchone()
    if receipt:
        if receipt['order_id'] != oid or receipt['transaction_id'] != transaction_id:
            raise RequestError('Payment reference is already used by another order.', 409)
        return order
    if order['status'] == 'paid':
        raise RequestError('Order has already been paid with another payment reference.',409)
    owner = db.execute('SELECT * FROM users WHERE id=? AND owner_id IS NULL', (order['user_id'],)).fetchone()
    if not owner:
        raise RequestError('Account owner not found.', 404)
    now = time.time()
    paid_until = add_months(max(now, owner['paid_until']), order['months'])
    db.execute('UPDATE users SET paid_until=? WHERE id=?', (paid_until, owner['id']))
    if order['plan'] == 'six_month':
        # Bonuses last six calendar months from payment, even if service is prepaid further ahead.
        benefit_end = add_months(max(now, owner['bonus_until']), 6)
        db.execute('UPDATE users SET parental_until=?,bonus_until=? WHERE id=?', (benefit_end, benefit_end, owner['id']))
    db.execute("UPDATE orders SET status='paid',paid_at=?,reference=? WHERE id=?", (now, transaction_id, oid))
    db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', (transaction_id, oid, event_id, provider, now))
    audit(db, owner['id'], 'Subscription payment confirmed')
    return db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()


def billing_ready():
    bank = os.environ.get('FANNTV_BANK_ADAPTER_URL', '').startswith('https://') and os.environ.get('FANNTV_BANK_ADAPTER_KEY') and os.environ.get('FANNTV_PAYMENT_WEBHOOK_SECRET')
    stripe = os.environ.get('STRIPE_SECRET_KEY') and os.environ.get('STRIPE_WEBHOOK_SECRET')
    selected = bank if os.environ.get('FANNTV_BANK_ADAPTER_URL') else stripe
    return bool(os.environ.get('PUBLIC_URL', '').startswith('https://') and selected)


def secure_provider_url(value):
    if not isinstance(value,str) or any(c.isspace() or ord(c)<32 for c in value):
        return False
    try:
        parsed=urllib.parse.urlsplit(value)
        return parsed.scheme=='https' and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.port!=0
    except ValueError:
        return False


def provider_json(response):
    raw=response.read(32769)
    if len(raw)>32768:
        raise ValueError('Provider response too large.')
    result=json.loads(raw)
    if not isinstance(result,dict):
        raise ValueError('Invalid provider response.')
    return result


class NoProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_urlopen(req, timeout=20):
    """Reject all redirects for authenticated provider requests, preserving proxy/TLS defaults."""
    if not secure_provider_url(req.full_url):
        raise ValueError('An HTTPS provider endpoint is required.')
    opener = urllib.request.build_opener(NoProviderRedirect())
    return opener.open(req, timeout=timeout)


def device_label(user_agent):
    ua=str(user_agent or '').lower()
    if 'iphone' in ua:
        device='iPhone'
    elif 'ipad' in ua:
        device='iPad'
    elif 'tizen' in ua or 'samsungtv' in ua:
        device='Samsung TV'
    elif 'webos' in ua or 'web0s' in ua:
        device='LG TV'
    elif 'android' in ua and ('tv' in ua or 'aft' in ua):
        device='Android TV'
    elif 'android' in ua:
        device='Android'
    elif 'windows' in ua:
        device='Windows'
    elif 'macintosh' in ua or 'mac os' in ua:
        device='macOS'
    elif 'linux' in ua:
        device='Linux'
    else:
        return 'Browser'
    browser='Edge' if 'edg/' in ua else 'Firefox' if 'firefox' in ua or 'fxios' in ua else 'Chrome' if 'chrome' in ua or 'crios' in ua else 'Safari' if 'safari' in ua else ''
    return device + (' · '+browser if browser else '')


def valid_categories(value):
    if value is None:
        return None
    if not isinstance(value, list) or any(c not in CATEGORIES for c in value):
        raise RequestError('Invalid channel categories.')
    return json.dumps(list(dict.fromkeys(value)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, value, status=200, cookie=None):
        if getattr(self,'_defer_json',False):
            self._pending_json=(value,status,cookie)
            return
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def token_hash(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['session'].value if 'session' in cookie else ''
        except Exception:
            token = ''
        return hashlib.sha256(token.encode()).hexdigest()

    def user(self, db):
        row = db.execute('SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (self.token_hash(), time.time())).fetchone()
        if row:
            session=db.execute('SELECT last_seen FROM sessions WHERE token=?',(self.token_hash(),)).fetchone()
            if session and session['last_seen'] <= time.time()-60:
                db.execute('UPDATE sessions SET last_seen=? WHERE token=?', (time.time(), self.token_hash()))
        return row

    def allowed(self, u, channel):
        allowed = json.loads(u['allowed_categories']) if u['allowed_categories'] is not None else None
        return allowed is None or channel['category'] in allowed

    def family(self, db, u):
        ensure_owner(u)
        now = time.time()
        members = []
        for m in db.execute('SELECT * FROM users WHERE owner_id=? ORDER BY bonus,id', (u['id'],)):
            member = public_user(m, db)
            member.update(allowed_categories=json.loads(m['allowed_categories']) if m['allowed_categories'] is not None else None, bonus=bool(m['bonus']), expires_at=u['bonus_until'] if m['bonus'] else None)
            members.append(member)
        devices = [dict(r) for r in db.execute('SELECT s.id,s.user_id,s.label,s.last_seen,s.expires,u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE (u.id=? OR u.owner_id=?) AND s.expires>? ORDER BY s.last_seen DESC', (u['id'],u['id'],now))]
        current = db.execute('SELECT id FROM sessions WHERE token=?', (self.token_hash(),)).fetchone()
        for d in devices:
            if 'Mozilla/' in (d['label'] or '') or len(d['label'] or '')>60:
                d['label']=device_label(d['label'])
            d['current'] = bool(current and d['id'] == current['id'])
        return dict(members=members, devices=devices, current_session_id=current['id'] if current else None, owner_id=u['id'], parental_until=u['parental_until'], bonus_until=u['bonus_until'], subuser_limit=2 if u['bonus_until'] > now else 1, bonus_active=u['bonus_until'] > now, device_limit=DEVICE_LIMIT)

    def do_GET(self):
        self._defer_json=True
        self._pending_json=None
        try:
            self.get_request()
        except RequestError as error:
            self.send_json({'error':error.message},error.status)
        except Exception:
            self._pending_json=None
            raise
        finally:
            self._defer_json=False
            if self._pending_json is not None:
                value,status,cookie=self._pending_json
                self._pending_json=None
                self.send_json(value,status,cookie)

    def get_request(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/api/'):
            with connect() as db:
                u = self.user(db)
                if path == '/api/me':
                    return self.send_json({'user': public_user(u,db) if u else None, 'billing': billing_ready(), 'plans': plans_for(db, owner_for(db,u)['id'] if u else None), 'currency':'AZN','email_delivery':account_security.email_delivery_ready(),'email_verification_required':account_security.production_mode()})
                if not u:
                    raise RequestError('Please sign in.', 401)
                if path == '/api/channels':
                    channels = [dict(r) for r in db.execute('SELECT id,name,category,title,description,art,enabled,(LENGTH(stream)>0) AS connected FROM channels WHERE enabled=1') if self.allowed(u,r)]
                    favorites = [r[0] for r in db.execute('SELECT channel_id FROM favorites WHERE user_id=?', (u['id'],))]
                    return self.send_json({'channels':channels,'favorites':favorites})
                if path.startswith('/api/play/'):
                    if not public_user(u,db)['access']:
                        raise RequestError('Your access has ended. Choose a subscription to keep watching.',403)
                    channel = db.execute('SELECT * FROM channels WHERE id=? AND enabled=1', (path.rsplit('/',1)[-1],)).fetchone()
                    if not channel or not self.allowed(u,channel):
                        raise RequestError('Channel not found.',404)
                    if not channel['stream']:
                        raise RequestError('This channel is not connected yet. An administrator can add a stream URL.',404)
                    session=db.execute('SELECT id FROM sessions WHERE token=?',(self.token_hash(),)).fetchone()
                    # Provider IO must never hold the SQLite writer lock.
                    db.commit()
                    try:
                        details=playback.resolve_playback_details(channel,u,session['id'] if session else '')
                    except playback.PlaybackError as error:
                        raise RequestError(str(error),503)
                    # A device or account may have been revoked while the adapter answered.
                    db.execute('BEGIN IMMEDIATE')
                    current=self.user(db)
                    latest=db.execute('SELECT * FROM channels WHERE id=? AND enabled=1',(channel['id'],)).fetchone()
                    if not current or not public_user(current,db)['access']:
                        raise RequestError('Playback access required.',403)
                    if not latest or not self.allowed(current,latest):
                        raise RequestError('Channel not found.',404)
                    if latest['stream']!=channel['stream']:
                        raise RequestError('The channel source changed. Please try again.',503)
                    audit(db,current['id'],'Playback: '+latest['name'])
                    return self.send_json({**details,'name':latest['name']})
                if path == '/api/family':
                    return self.send_json(self.family(db,u))
                if path == '/api/orders':
                    ensure_owner(u)
                    return self.send_json({'orders':[dict(r) for r in db.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 100',(u['id'],))]})
                if path == '/api/admin' and u['role']=='admin' and not u['suspended']:
                    users=[public_user(r,db) for r in db.execute('SELECT * FROM users ORDER BY created DESC')]
                    logs=[dict(r) for r in db.execute('SELECT l.event,l.created,u.email FROM logs l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 100')]
                    overrides=[dict(r) for r in db.execute('SELECT p.*,u.email FROM price_overrides p JOIN users u ON u.id=p.user_id')]
                    orders=[dict(r) for r in db.execute('SELECT o.*,u.email FROM orders o JOIN users u ON u.id=o.user_id ORDER BY created DESC LIMIT 100')]
                    return self.send_json({'users':users,'logs':logs,'channels':[dict(r) for r in db.execute('SELECT * FROM channels')], 'plans':plans_for(db),'price_overrides':overrides,'orders':orders,'payments':[o for o in orders if o['status']=='paid']})
                raise RequestError('Not found.',404)
        if path == '/media/demo.mp4':
            with connect() as db:
                u=self.user(db)
                channel=db.execute('SELECT * FROM channels WHERE id=1').fetchone()
                if not u or not public_user(u,db)['access'] or not channel or not channel['enabled'] or not self.allowed(u,channel):
                    raise RequestError('Playback access required.',403)
            target=ROOT/'static'/'demo.mp4'
        else:
            target=ROOT/'static'/(path.lstrip('/') or 'index.html')
            if not target.resolve().is_relative_to((ROOT/'static').resolve()) or target.resolve()==(ROOT/'static'/'demo.mp4').resolve():
                return self.send_error(404)
        if not target.is_file():
            return self.send_error(404)
        types={'.html':'text/html','.css':'text/css','.js':'application/javascript','.svg':'image/svg+xml','.webmanifest':'application/manifest+json','.mp4':'video/mp4'}
        size=target.stat().st_size
        start,end=0,size-1
        partial=self.headers.get('Range','')
        if partial:
            try:
                left,right=partial.removeprefix('bytes=').split('-')
                start=int(left or 0)
                end=min(int(right) if right else size-1,size-1)
                if start<0 or start>end:
                    raise ValueError()
            except ValueError:
                return self.send_error(416)
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',types.get(target.suffix,'application/octet-stream'))
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Accept-Ranges','bytes')
        if target.suffix=='.mp4':
            self.send_header('Cache-Control','private, no-store')
        if partial:
            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with target.open('rb') as f:
            f.seek(start)
            remaining=end-start+1
            while remaining:
                chunk=f.read(min(65536,remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining-=len(chunk)

    def do_POST(self):
        self._defer_json=True
        self._pending_json=None
        try:
            self.post_request()
        except Exception:
            self._pending_json=None
            raise
        finally:
            self._defer_json=False
            if self._pending_json is not None:
                value,status,cookie=self._pending_json
                self._pending_json=None
                self.send_json(value,status,cookie)

    def post_request(self):
        try:
            length=int(self.headers.get('Content-Length',0))
            if length<0 or length>32768:
                raise RequestError('Request too large.',413)
            raw=self.rfile.read(length)
            path=urllib.parse.urlsplit(self.path).path
            if path in ('/api/stripe/webhook','/api/payments/webhook'):
                return self.webhook(raw,path)
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'):
                raise RequestError('Origin not allowed.',403)
            body=json.loads(raw or '{}')
            if not isinstance(body,dict):
                raise ValueError()
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                u=self.user(db)
                if path.startswith('/api/account/'):
                    return self.manage_account(db,u,path,body)
                if path in ('/api/register','/api/login'):
                    return self.authenticate(db,path,body,u)
                if not u:
                    raise RequestError('Please sign in.',401)
                if path=='/api/logout':
                    db.execute('DELETE FROM sessions WHERE token=?',(self.token_hash(),))
                    return self.send_json({'ok':True},cookie='session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0')
                if path=='/api/play/heartbeat':
                    if not public_user(u,db)['access']:
                        raise RequestError('Your access has ended. Choose a subscription to keep watching.',403)
                    channel=db.execute('SELECT * FROM channels WHERE id=? AND enabled=1',(int(body['channel_id']),)).fetchone()
                    if not channel or not self.allowed(u,channel):
                        raise RequestError('Channel not found.',404)
                    return self.send_json({'ok':True})
                if path=='/api/favorite':
                    cid=int(body['id'])
                    channel=db.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                    if not channel or not self.allowed(u,channel):
                        raise RequestError('Channel not found.',404)
                    if body.get('active'):
                        db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?)',(u['id'],cid))
                    else:
                        db.execute('DELETE FROM favorites WHERE user_id=? AND channel_id=?',(u['id'],cid))
                    return self.send_json({'ok':True})
                if path.startswith('/api/family/'):
                    return self.manage_family(db,u,path,body)
                if path=='/api/orders':
                    ensure_owner(u)
                    if public_user(u,db)['requires_email_verification']:
                        raise RequestError("Verify the account owner's email address before purchasing a package.",403)
                    return self.send_json({'order':dict(create_order(db,u['id'],body.get('plan')))})
                if path=='/api/checkout':
                    ensure_owner(u)
                    if public_user(u,db)['requires_email_verification']:
                        raise RequestError("Verify the account owner's email address before purchasing a package.",403)
                    if not billing_ready():
                        raise RequestError('Payments are not connected yet. Ask the administrator to configure billing.',503)
                    order=create_order(db,u['id'],body.get('plan'))
                    # Commit frozen quote before provider call; failed network attempts never grant access.
                    db.commit()
                    return self.checkout(db,u,order,body)
                if path.startswith('/api/admin/'):
                    if u['role']!='admin' or u['suspended']:
                        raise RequestError('Administrator access required.',403)
                    return self.manage_admin(db,u,path,body)
                raise RequestError('Not found.',404)
        except RequestError as error:
            self.send_json({'error':error.message},error.status)
        except (ValueError,KeyError,TypeError,OverflowError):
            self.send_json({'error':'Invalid request.'},400)

    def authenticate(self,db,path,body,current):
        email=str(body.get('email','')).strip().lower()
        password=str(body.get('password',''))
        try:
            account_security.take_limit(db,'auth-ip',self.client_address[0],20,900)
            account_security.take_limit(db,'auth-account',email,20,900)
        except account_security.LimitExceeded:
            return self.send_json({'error':'Too many attempts. Try again in 15 minutes.'},429)
        key=self.client_address[0]
        attempt=db.execute('SELECT * FROM attempts WHERE key=?',(key,)).fetchone()
        if attempt and attempt['reset']>time.time() and attempt['count']>=20:
            return self.send_json({'error':'Too many attempts. Try again in 15 minutes.'},429)
        db.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN reset<? THEN 1 ELSE count+1 END,reset=CASE WHEN reset<? THEN excluded.reset ELSE reset END',(key,time.time()+900,time.time(),time.time()))
        if path=='/api/register':
            name=str(body.get('name','')).strip()[:80]
            if not name or not account_security.valid_email(email) or not 10<=len(password)<=256:
                return self.send_json({'error':'Enter your name, a valid email, and a password of 10–256 characters.'},400)
            try:
                db.execute('INSERT INTO users(email,name,password,created) VALUES(?,?,?,?)',(email,name,password_hash(password),time.time()))
            except sqlite3.IntegrityError:
                return self.send_json({'error':'This email already has an account. Please sign in.'},409)
            u=db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            audit(db,u['id'],'Six-day trial started')
        else:
            u=db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if not u or len(password)>256 or not verify(password,u['password']):
                return self.send_json({'error':'Email or password is incorrect.'},401)
            if u['owner_id'] and not public_user(u,db)['active']:
                return self.send_json({'error':'This bonus sub-user has expired. Ask the account owner to renew.'},403)
        if u['suspended'] or owner_for(db,u)['suspended']:
            return self.send_json({'error':'Account suspended. Contact the administrator.'},403)
        # Existing browser sessions are reused, including login to another household member.
        db.execute('DELETE FROM sessions WHERE expires<=? OR user_id IN (SELECT m.id FROM users m JOIN users o ON o.id=m.owner_id WHERE m.bonus=1 AND o.bonus_until<=?)',(time.time(),time.time()))
        old=db.execute('SELECT * FROM sessions WHERE token=?',(self.token_hash(),)).fetchone()
        oid=owner_for(db,u)['id']
        occupied=db.execute('SELECT COUNT(*) FROM sessions s JOIN users m ON m.id=s.user_id WHERE (m.id=? OR m.owner_id=?) AND s.expires>? AND s.token!=?',(oid,oid,time.time(),self.token_hash())).fetchone()[0]
        if occupied>=DEVICE_LIMIT:
            return self.send_json({'error':'Four devices are already signed in. The account owner can remove a device in Family settings.'},409)
        if path=='/api/login':
            audit(db,u['id'],'Signed in')
        token=secrets.token_urlsafe(32)
        secure='; Secure' if os.environ.get('PUBLIC_URL','').startswith('https://') else ''
        cookie=f'session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_LIFETIME}{secure}'
        if old:
            db.execute('UPDATE sessions SET token=?,user_id=?,expires=?,last_seen=?,label=? WHERE id=?',(hashlib.sha256(token.encode()).hexdigest(),u['id'],time.time()+SESSION_LIFETIME,time.time(),device_label(self.headers.get('User-Agent','')),old['id']))
            sent=self.signup_verification(db,u) if path=='/api/register' else None
            return self.send_json({'user':public_user(u,db),'verification_sent':sent},cookie=cookie)
        db.execute('INSERT INTO sessions(token,user_id,expires,id,label,last_seen) VALUES(?,?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],time.time()+SESSION_LIFETIME,secrets.token_urlsafe(12),device_label(self.headers.get('User-Agent','')),time.time()))
        sent=self.signup_verification(db,u) if path=='/api/register' else None
        return self.send_json({'user':public_user(u,db),'verification_sent':sent},cookie=cookie)

    def deliver_account_token(self, db, user, purpose, deferred=False):
        token = account_security.issue_token(db, user['id'], purpose)
        db.commit()
        if deferred:
            # Forgot responses never wait for SMTP, avoiding existence disclosure via network timing.
            def delivery():
                try:
                    account_security.send_account_email(user['email'], purpose, token)
                except Exception:
                    with connect() as cleanup:
                        cleanup.execute('DELETE FROM account_tokens WHERE token_hash=?',(account_security.digest(token),))
            threading.Thread(target=delivery,daemon=True).start()
            return True
        try:
            account_security.send_account_email(user['email'], purpose, token)
            return True
        except Exception:
            # An uncertain/failed delivery must not leave a usable unattended token.
            db.execute('DELETE FROM account_tokens WHERE token_hash=?', (account_security.digest(token),))
            db.commit()
            return False

    def signup_verification(self, db, user):
        if not account_security.email_delivery_ready():
            return False
        return self.deliver_account_token(db, user, 'verify')

    def account_limit(self, db, scope, identity, maximum, window):
        try:
            account_security.take_limit(db, scope, identity, maximum, window)
            return True
        except account_security.LimitExceeded:
            return False

    def revoke_account_sessions(self, db, user):
        if user['owner_id']:
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        else:
            db.execute('DELETE FROM sessions WHERE user_id=? OR user_id IN (SELECT id FROM users WHERE owner_id=?)', (user['id'], user['id']))

    def manage_account(self, db, user, path, body):
        if path == '/api/account/forgot':
            if not account_security.email_delivery_ready():
                return self.send_json({'error':'Email delivery is not configured.'},503)
            email = str(body.get('email','')).strip().lower()
            ip_ok = self.account_limit(db,'forgot-ip',self.client_address[0],10,3600)
            email_ok = self.account_limit(db,'forgot-account',email,3,3600)
            if not ip_ok or not email_ok:
                # Identical response for known and unknown accounts, including limits.
                return self.send_json({'ok':True,'message':account_security.GENERIC_RESET_MESSAGE})
            target = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            if target:
                self.deliver_account_token(db,target,'reset',deferred=True)
            return self.send_json({'ok':True,'message':account_security.GENERIC_RESET_MESSAGE})
        if path in ('/api/account/reset','/api/account/verify'):
            purpose = 'reset' if path.endswith('/reset') else 'verify'
            if not self.account_limit(db,purpose+'-ip',self.client_address[0],20,900):
                return self.send_json({'error':'Too many attempts. Try again in 15 minutes.'},429)
            password = body.get('password','')
            if purpose == 'reset' and (not isinstance(password,str) or not 10<=len(password)<=256):
                return self.send_json({'error':'Use a password of 10–256 characters.'},400)
            uid = account_security.consume_token(db,body.get('token'),purpose)
            if not uid:
                return self.send_json({'error':'This account link is invalid or has expired. Request a new email.'},400)
            target = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not target:
                return self.send_json({'error':'This account link is invalid or has expired. Request a new email.'},400)
            if purpose == 'verify':
                db.execute('UPDATE users SET email_verified=1 WHERE id=?', (uid,))
                db.execute("DELETE FROM account_tokens WHERE user_id=? AND purpose='verify'",(uid,))
                audit(db,uid,'Email address verified')
                return self.send_json({'ok':True})
            db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),uid))
            db.execute('DELETE FROM account_tokens WHERE user_id=?',(uid,))
            self.revoke_account_sessions(db,target)
            audit(db,uid,'Password reset completed')
            return self.send_json({'ok':True},cookie='session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0')
        if not user:
            raise RequestError('Please sign in.',401)
        if path == '/api/account/password':
            ip_ok = self.account_limit(db,'password-ip',self.client_address[0],20,900)
            user_ok = self.account_limit(db,'password-user',user['id'],5,900)
            if not ip_ok or not user_ok:
                return self.send_json({'error':'Too many attempts. Try again in 15 minutes.'},429)
            current = body.get('current_password','')
            password = body.get('password','')
            if not isinstance(current,str) or len(current)>256 or not verify(current,user['password']):
                return self.send_json({'error':'Your current password is incorrect.'},400)
            if not isinstance(password,str) or not 10<=len(password)<=256:
                return self.send_json({'error':'Use a password of 10–256 characters.'},400)
            if verify(password,user['password']):
                return self.send_json({'error':'Choose a different password.'},400)
            db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),user['id']))
            db.execute('DELETE FROM account_tokens WHERE user_id=?',(user['id'],))
            self.revoke_account_sessions(db,user)
            audit(db,user['id'],'Password changed')
            return self.send_json({'ok':True},cookie='session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0')
        if path == '/api/account/resend-verification':
            ensure_owner(user)
            if user['email_verified']:
                return self.send_json({'ok':True})
            if not account_security.email_delivery_ready():
                return self.send_json({'error':'Email delivery is not configured.'},503)
            ip_ok = self.account_limit(db,'verify-mail-ip',self.client_address[0],10,3600)
            user_ok = self.account_limit(db,'verify-mail-user',user['id'],3,3600)
            if not ip_ok or not user_ok:
                return self.send_json({'error':'Too many email requests. Try again in one hour.'},429)
            if not self.deliver_account_token(db,user,'verify'):
                return self.send_json({'error':'Email could not be sent. Please try again later.'},503)
            return self.send_json({'ok':True})
        raise RequestError('Not found.',404)

    def manage_family(self,db,u,path,body):
        ensure_owner(u)
        if path=='/api/family/device':
            target=db.execute('SELECT s.* FROM sessions s JOIN users m ON m.id=s.user_id WHERE s.id=? AND (m.id=? OR m.owner_id=?)',(str(body.get('id','')),u['id'],u['id'])).fetchone()
            if not target:
                raise RequestError('Device not found.',404)
            db.execute('DELETE FROM sessions WHERE id=?',(target['id'],))
            audit(db,u['id'],'Family device revoked')
            return self.send_json({'ok':True})
        target=None
        if body.get('id'):
            target=db.execute('SELECT * FROM users WHERE id=? AND owner_id=?',(int(body['id']),u['id'])).fetchone()
            if not target:
                raise RequestError('Sub-user not found.',404)
        if path=='/api/family/remove':
            if not target:
                raise RequestError('Sub-user not found.',404)
            db.execute('DELETE FROM sessions WHERE user_id=?',(target['id'],))
            db.execute('DELETE FROM favorites WHERE user_id=?',(target['id'],))
            db.execute('DELETE FROM account_tokens WHERE user_id=?',(target['id'],))
            db.execute('DELETE FROM users WHERE id=?',(target['id'],))
            audit(db,u['id'],'Sub-user removed')
            return self.send_json({'ok':True})
        if path!='/api/family/member':
            raise RequestError('Not found.',404)
        parental_active=u['parental_until']>time.time()
        prior=target['allowed_categories'] if target else None
        categories=valid_categories(body['allowed_categories']) if 'allowed_categories' in body else prior
        if categories!=prior and not parental_active:
            raise RequestError('A six-month package is required to change parental controls.',403)
        name=str(body.get('name',target['name'] if target else '')).strip()[:80]
        email=str(body.get('email',target['email'] if target else '')).strip().lower()
        password=str(body.get('password',''))
        if not name or not account_security.valid_email(email) or ((not target or password) and not 10<=len(password)<=256):
            raise RequestError('Enter your name, a valid email, and a password of 10–256 characters.')
        if target:
            if db.execute('SELECT 1 FROM users WHERE email=? AND id!=?',(email,target['id'])).fetchone():
                raise RequestError('This email already has an account. Please sign in.',409)
            if email != target['email']:
                db.execute('UPDATE users SET email_verified=0 WHERE id=?',(target['id'],))
                db.execute('DELETE FROM account_tokens WHERE user_id=?',(target['id'],))
            db.execute('UPDATE users SET name=?,email=?,allowed_categories=? WHERE id=?',(name,email,categories,target['id']))
            if password:
                db.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),target['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?',(target['id'],))
                db.execute('DELETE FROM account_tokens WHERE user_id=?',(target['id'],))
        else:
            base=db.execute('SELECT COUNT(*) FROM users WHERE owner_id=? AND bonus=0',(u['id'],)).fetchone()[0]
            bonus=db.execute('SELECT COUNT(*) FROM users WHERE owner_id=? AND bonus=1',(u['id'],)).fetchone()[0]
            extra=base>=1 or bool(body.get('bonus'))
            if (extra and (u['bonus_until']<=time.time() or bonus>=1)) or (not extra and base>=1):
                raise RequestError('Your sub-user limit has been reached. A six-month package unlocks one additional sub-user.',403)
            try:
                db.execute("INSERT INTO users(email,name,password,role,created,owner_id,bonus,allowed_categories) VALUES(?,?,?,'subuser',?,?,?,?)",(email,name,password_hash(password),time.time(),u['id'],extra,categories))
            except sqlite3.IntegrityError:
                raise RequestError('This email already has an account. Please sign in.',409)
        audit(db,u['id'],'Family member updated')
        return self.send_json({'ok':True,'family':self.family(db,u)})

    def manage_admin(self,db,u,path,body):
        if path=='/api/admin/user':
            uid=int(body['id'])
            if uid==u['id']:
                raise RequestError('You cannot suspend your own account.')
            db.execute('UPDATE users SET suspended=? WHERE id=?',(bool(body.get('suspended')),uid))
            audit(db,u['id'],f"User {uid}: {'suspended' if body.get('suspended') else 'restored'}")
        elif path=='/api/admin/channel':
            stream=str(body.get('stream','')).strip()
            if stream and not stream.startswith(('https://','/media/')):
                raise RequestError('Use an HTTPS stream URL.')
            db.execute('UPDATE channels SET stream=?,enabled=? WHERE id=?',(stream,bool(body.get('enabled')),int(body['id'])))
            audit(db,u['id'],'Updated channel '+str(body['id']))
        elif path=='/api/admin/pricing':
            plan=body.get('plan')
            if not db.execute('SELECT 1 FROM plans WHERE id=?',(plan,)).fetchone():
                raise RequestError('Invalid service package.')
            uid=body.get('user_id')
            amount=body.get('amount_minor')
            if amount is not None and (type(amount) is not int or not 1<=amount<=10000000):
                raise RequestError('Enter a positive AZN amount with at most two decimal places.')
            if uid is None:
                if amount is None:
                    raise RequestError('A global price is required.')
                db.execute('UPDATE plans SET amount_minor=? WHERE id=?',(amount,plan))
            else:
                uid=int(uid)
                if not db.execute('SELECT 1 FROM users WHERE id=? AND owner_id IS NULL',(uid,)).fetchone():
                    raise RequestError('Account owner not found.',404)
                if amount is None:
                    db.execute('DELETE FROM price_overrides WHERE user_id=? AND plan=?',(uid,plan))
                else:
                    db.execute('INSERT INTO price_overrides VALUES(?,?,?) ON CONFLICT(user_id,plan) DO UPDATE SET amount_minor=excluded.amount_minor',(uid,plan,amount))
            audit(db,u['id'],'AZN pricing updated')
        elif path=='/api/admin/payment':
            uid=int(body['user_id'])
            reference=str(body.get('reference','')).strip()
            if not 3<=len(reference)<=160:
                raise RequestError('Enter a unique verified payment reference.')
            reference='manual:'+reference
            prior=db.execute('SELECT * FROM orders WHERE reference=?',(reference,)).fetchone()
            if prior:
                if prior['user_id']!=uid or prior['plan']!=body.get('plan'):
                    raise RequestError('Payment reference is already used by another order.',409)
                return self.send_json({'ok':True,'order':dict(prior),'user':public_user(db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(),db)})
            order=create_order(db,uid,body.get('plan'))
            if type(body.get('amount_minor')) is not int or body['amount_minor']!=order['amount_minor']:
                raise RequestError('Price changed. Refresh the current price and try again.',409)
            settled=settle_order(db,order['id'],reference,reference,'manual',order['amount_minor'],'AZN')
            audit(db,u['id'],'Verified manual payment recorded')
            return self.send_json({'ok':True,'order':dict(settled),'user':public_user(db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(),db)})
        else:
            raise RequestError('Not found.',404)
        return self.send_json({'ok':True})

    def checkout(self,db,u,order,body):
        base=os.environ['PUBLIC_URL'].rstrip('/')
        adapter=os.environ.get('FANNTV_BANK_ADAPTER_URL')
        if adapter:
            # This is our trusted merchant adapter contract, not a claimed bank API.
            payload={'order_id':order['id'],'amount_minor':order['amount_minor'],'currency':'AZN','description':f"FANnTV {order['months']} month package",'language':body.get('language','en'),'return_url':base+'/?payment=success&order='+order['id'],'cancel_url':base+'/?payment=cancelled','webhook_url':base+'/api/payments/webhook'}
            req=urllib.request.Request(adapter,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['FANNTV_BANK_ADAPTER_KEY'],'Idempotency-Key':order['id']})
            try:
                if not adapter.startswith('https://'):
                    raise ValueError()
                with provider_urlopen(req,timeout=20) as response:
                    result=provider_json(response)
                url=result['checkout_url']
                if not secure_provider_url(url):
                    raise ValueError()
                db.execute('UPDATE orders SET provider_session=? WHERE id=?',(str(result.get('session_id',order['id'])),order['id']))
                return self.send_json({'url':url,'order_id':order['id']})
            except Exception:
                return self.send_json({'error':'Payment provider unavailable. Please try again later.'},502)
        fields={'mode':'payment','locale':{'az':'en','en':'en','ru':'ru','zh-CN':'zh'}.get(body.get('language'),'en'),
                'line_items[0][price_data][currency]':'azn','line_items[0][price_data][unit_amount]':order['amount_minor'],
                'line_items[0][price_data][product_data][name]':f"FANnTV {order['months']} month package",'line_items[0][quantity]':1,
                'client_reference_id':order['id'],'metadata[order_id]':order['id'],'customer_email':u['email'],
                'success_url':base+'/?payment=success&order='+order['id'],'cancel_url':base+'/?payment=cancelled'}
        req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=urllib.parse.urlencode(fields).encode(),headers={'Authorization':'Bearer '+os.environ['STRIPE_SECRET_KEY'],'Idempotency-Key':order['id']})
        try:
            with provider_urlopen(req,timeout=20) as response:
                result=provider_json(response)
            if not secure_provider_url(result.get('url')):
                raise ValueError()
            db.execute('UPDATE orders SET provider_session=? WHERE id=?',(result['id'],order['id']))
            return self.send_json({'url':result['url'],'order_id':order['id']})
        except Exception:
            return self.send_json({'error':'Payment provider unavailable. Please try again later.'},502)

    def webhook(self,raw,path):
        stripe=path=='/api/stripe/webhook'
        secret=os.environ.get('STRIPE_WEBHOOK_SECRET' if stripe else 'FANNTV_PAYMENT_WEBHOOK_SECRET')
        if not secret:
            raise RequestError('Billing not configured.',503)
        try:
            if stripe:
                parts=urllib.parse.parse_qs(self.headers.get('Stripe-Signature','').replace(',','&'))
                timestamp=parts['t'][0]
                signatures=parts.get('v1',[])
            else:
                timestamp=self.headers.get('X-FANNTV-Timestamp','')
                signatures=[self.headers.get('X-FANNTV-Signature','')]
            signature=hmac.new(secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
            if abs(time.time()-int(timestamp))>300 or not any(hmac.compare_digest(signature,s) for s in signatures):
                raise ValueError()
            event=json.loads(raw)
            if stripe:
                if event['type'] not in ('checkout.session.completed','checkout.session.async_payment_succeeded'):
                    return self.send_json({'ok':True,'ignored':True})
                obj=event['data']['object']
                if obj.get('payment_status')!='paid' or obj.get('mode')!='payment':
                    return self.send_json({'ok':True,'ignored':True})
                oid=obj['metadata']['order_id']
                tx='stripe:'+str(obj['payment_intent'])
                if not obj.get('payment_intent'):
                    raise ValueError()
                amount,currency=obj['amount_total'],obj['currency']
                event_id='stripe:'+event['id']
            else:
                if event['status']!='paid':
                    return self.send_json({'ok':True,'ignored':True})
                oid=event['order_id']
                transaction=event['transaction_id']
                if not isinstance(transaction,str) or not transaction.strip():
                    raise ValueError()
                tx='gateway:'+transaction
                amount,currency=event['amount_minor'],event['currency']
                event_id='gateway:'+event['id']
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if stripe:
                    stored=db.execute('SELECT provider_session FROM orders WHERE id=?',(oid,)).fetchone()
                    if not stored or stored['provider_session']!=obj['id']:
                        raise RequestError('Payment checkout session does not match the order.')
                settled=settle_order(db,oid,tx,event_id,'stripe' if stripe else 'gateway',amount,currency)
                return self.send_json({'ok':True,'order_id':settled['id']})
        except (ValueError,KeyError,TypeError,AttributeError):
            raise RequestError('Invalid webhook.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=3000)
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--create-admin',metavar='EMAIL')
    args=parser.parse_args()
    initialize()
    if args.create_admin:
        with connect() as db:
            if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                raise SystemExit('A platform administrator already exists. Only one is allowed.')
        password=getpass.getpass('Admin password (minimum 12 characters): ')
        if len(password)<12:
            raise SystemExit('Password must have at least 12 characters.')
        with connect() as db:
            db.execute("INSERT INTO users(email,name,password,role,created) VALUES(?,?,?,'admin',?)",(args.create_admin.lower(),'Administrator',password_hash(password),time.time()))
        print('Administrator created. Sign in through the app.')
    else:
        if account_security.production_mode():
            raise SystemExit('Production must run through the hardened WSGI entry point with Gunicorn, not server.py.')
        print(f'FANnTV listening on port {args.port}',flush=True)
        ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
