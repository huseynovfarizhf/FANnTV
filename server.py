import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DB = os.environ.get('FANNTV_DB', str(ROOT / 'instance' / 'fanntv.db'))
TRIAL = 6 * 86400


def connect():
    db = sqlite3.connect(DB)
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
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, password TEXT, role TEXT DEFAULT 'user', created REAL, paid_until REAL DEFAULT 0, suspended INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT, category TEXT, title TEXT, description TEXT, art TEXT, stream TEXT, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, channel_id INTEGER, PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, user_id INTEGER, event TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY, count INTEGER, reset REAL);
        ''')
        if not db.execute('SELECT 1 FROM channels LIMIT 1').fetchone():
            rows = [
                ('NATURE+', 'Nature', 'The wild is calling', 'Find a little wonder. Explore extraordinary places, remarkable wildlife, and the world beyond your window.', 'nature', '/media/demo.mp4'),
                ('WORLD NEWS', 'News', 'A wider perspective', 'Your place for a world of stories.', 'news', ''),
                ('ARENA', 'Sports', 'Every moment matters', 'Get closer to the action.', 'sports', ''),
                ('CINEMA', 'Movies', 'Stay for the story', 'Discover your next favorite film.', 'cinema', ''),
                ('LITTLE STARS', 'Kids', 'Big little adventures', 'A world of imagination.', 'kids', ''),
                ('HORIZON', 'Travel', 'Somewhere new', 'Take the scenic route.', 'travel', ''),
            ]
            db.executemany('INSERT INTO channels(name,category,title,description,art,stream) VALUES(?,?,?,?,?,?)', rows)


def audit(db, uid, event):
    db.execute('INSERT INTO logs(user_id,event,created) VALUES(?,?,?)', (uid, event, time.time()))


def public_user(u):
    now = time.time()
    trial_end = u['created'] + TRIAL
    return dict(id=u['id'], email=u['email'], name=u['name'], role=u['role'], trial_end=trial_end, paid_until=u['paid_until'], suspended=bool(u['suspended']), access=not u['suspended'] and (u['role'] == 'admin' or max(trial_end, u['paid_until']) > now))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, value, status=200, cookie=None):
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

    def user(self, db):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['session'].value if 'session' in cookie else ''
        except Exception:
            token = ''
        return db.execute('SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/api/'):
            with connect() as db:
                u = self.user(db)
                if path == '/api/me':
                    return self.send_json({'user': public_user(u) if u else None, 'billing': bool(os.environ.get('STRIPE_SECRET_KEY') and os.environ.get('STRIPE_WEBHOOK_SECRET') and os.environ.get('STRIPE_MONTHLY_PRICE') and os.environ.get('STRIPE_YEARLY_PRICE') and os.environ.get('PUBLIC_URL'))})
                if not u:
                    return self.send_json({'error': 'Please sign in.'}, 401)
                if path == '/api/channels':
                    channels = [dict(r) for r in db.execute('SELECT id,name,category,title,description,art,enabled FROM channels WHERE enabled=1')]
                    favorites = [r[0] for r in db.execute('SELECT channel_id FROM favorites WHERE user_id=?', (u['id'],))]
                    return self.send_json({'channels': channels, 'favorites': favorites})
                if path.startswith('/api/play/'):
                    if not public_user(u)['access']:
                        return self.send_json({'error': 'Your access has ended. Choose a subscription to keep watching.'}, 403)
                    channel = db.execute('SELECT * FROM channels WHERE id=? AND enabled=1', (path.rsplit('/', 1)[-1],)).fetchone()
                    if not channel or not channel['stream']:
                        return self.send_json({'error': 'This channel is not connected yet. An administrator can add a stream URL.'}, 404)
                    audit(db, u['id'], 'Playback: ' + channel['name'])
                    return self.send_json({'url': channel['stream'], 'name': channel['name']})
                if path == '/api/admin' and u['role'] == 'admin' and not u['suspended']:
                    users = [public_user(r) for r in db.execute('SELECT * FROM users ORDER BY created DESC')]
                    logs = [dict(r) for r in db.execute('SELECT l.event,l.created,u.email FROM logs l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 100')]
                    return self.send_json({'users': users, 'logs': logs, 'channels': [dict(r) for r in db.execute('SELECT * FROM channels')]})
                return self.send_json({'error': 'Not found.'}, 404)
        if path == '/media/demo.mp4':
            target = ROOT / 'static' / 'demo.mp4'
        else:
            target = ROOT / 'static' / (path.lstrip('/') or 'index.html')
            if not target.resolve().is_relative_to((ROOT / 'static').resolve()):
                return self.send_error(404)
        if not target.is_file():
            return self.send_error(404)
        types = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json', '.mp4': 'video/mp4'}
        size = target.stat().st_size
        start, end = 0, size - 1
        partial = self.headers.get('Range', '')
        if partial:
            try:
                left, right = partial.removeprefix('bytes=').split('-')
                start = int(left or 0)
                end = min(int(right) if right else size - 1, size - 1)
                if start < 0 or start > end:
                    raise ValueError()
            except ValueError:
                return self.send_error(416)
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', types.get(target.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Accept-Ranges', 'bytes')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with target.open('rb') as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 32768:
                return self.send_json({'error': 'Request too large.'}, 413)
            raw = self.rfile.read(length)
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/stripe/webhook':
                return self.webhook(raw)
            origin = self.headers.get('Origin')
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'):
                return self.send_json({'error': 'Origin not allowed.'}, 403)
            body = json.loads(raw or '{}')
            if not isinstance(body, dict):
                raise ValueError()
            with connect() as db:
                u = self.user(db)
                if path in ('/api/register', '/api/login'):
                    email = str(body.get('email', '')).strip().lower()
                    password = str(body.get('password', ''))
                    key = self.client_address[0]
                    attempt = db.execute('SELECT * FROM attempts WHERE key=?', (key,)).fetchone()
                    if attempt and attempt['reset'] > time.time() and attempt['count'] >= 20:
                        return self.send_json({'error': 'Too many attempts. Try again in 15 minutes.'}, 429)
                    db.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN reset<? THEN 1 ELSE count+1 END, reset=CASE WHEN reset<? THEN excluded.reset ELSE reset END', (key, time.time()+900, time.time(), time.time()))
                    if path == '/api/register':
                        name = str(body.get('name', '')).strip()[:80]
                        if not name or '@' not in email or len(email) > 254 or len(password) < 10 or len(password) > 256:
                            return self.send_json({'error': 'Enter your name, a valid email, and a password of 10–256 characters.'}, 400)
                        try:
                            db.execute('INSERT INTO users(email,name,password,created) VALUES(?,?,?,?)', (email, name, password_hash(password), time.time()))
                        except sqlite3.IntegrityError:
                            return self.send_json({'error': 'This email already has an account. Please sign in.'}, 409)
                        u = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
                        audit(db, u['id'], 'Six-day trial started')
                    else:
                        u = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
                        if not u or len(password) > 256 or not verify(password, u['password']):
                            return self.send_json({'error': 'Email or password is incorrect.'}, 401)
                        audit(db, u['id'], 'Signed in')
                    token = secrets.token_urlsafe(32)
                    db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), u['id'], time.time()+30*86400))
                    secure = '; Secure' if os.environ.get('PUBLIC_URL', '').startswith('https://') else ''
                    return self.send_json({'user': public_user(u)}, cookie=f'session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}')
                if not u:
                    return self.send_json({'error': 'Please sign in.'}, 401)
                if path == '/api/logout':
                    cookie = SimpleCookie(self.headers.get('Cookie', ''))
                    if 'session' in cookie:
                        db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(cookie['session'].value.encode()).hexdigest(),))
                    return self.send_json({'ok': True}, cookie='session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0')
                if path == '/api/favorite':
                    cid = int(body['id'])
                    if not db.execute('SELECT 1 FROM channels WHERE id=?', (cid,)).fetchone():
                        return self.send_json({'error': 'Channel not found.'}, 404)
                    if body.get('active'):
                        db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?)', (u['id'], cid))
                    else:
                        db.execute('DELETE FROM favorites WHERE user_id=? AND channel_id=?', (u['id'], cid))
                    return self.send_json({'ok': True})
                if path == '/api/checkout':
                    if u['suspended']:
                        return self.send_json({'error': 'Account suspended. Contact the administrator.'}, 403)
                    price = os.environ.get('STRIPE_YEARLY_PRICE' if body.get('plan') == 'yearly' else 'STRIPE_MONTHLY_PRICE')
                    base = os.environ.get('PUBLIC_URL', '').rstrip('/')
                    key = os.environ.get('STRIPE_SECRET_KEY')
                    if not (price and base and key and os.environ.get('STRIPE_WEBHOOK_SECRET')):
                        return self.send_json({'error': 'Payments are not connected yet. Ask the administrator to configure billing.'}, 503)
                    fields = {'mode': 'subscription', 'line_items[0][price]': price, 'line_items[0][quantity]': 1, 'client_reference_id': u['id'], 'customer_email': u['email'], 'success_url': base+'/?payment=success', 'cancel_url': base+'/?payment=cancelled', 'subscription_data[metadata][user_id]': u['id']}
                    req = urllib.request.Request('https://api.stripe.com/v1/checkout/sessions', data=urllib.parse.urlencode(fields).encode(), headers={'Authorization': 'Bearer '+key})
                    try:
                        with urllib.request.urlopen(req, timeout=20) as response:
                            return self.send_json({'url': json.load(response)['url']})
                    except Exception:
                        return self.send_json({'error': 'Payment provider unavailable. Please try again later.'}, 502)
                if path.startswith('/api/admin/'):
                    if u['role'] != 'admin' or u['suspended']:
                        return self.send_json({'error': 'Administrator access required.'}, 403)
                    if path == '/api/admin/user':
                        uid = int(body['id'])
                        if uid == u['id']:
                            return self.send_json({'error': 'You cannot suspend your own account.'}, 400)
                        db.execute('UPDATE users SET suspended=? WHERE id=?', (bool(body.get('suspended')), uid))
                        audit(db, u['id'], f"User {uid}: {'suspended' if body.get('suspended') else 'restored'}")
                        return self.send_json({'ok': True})
                    if path == '/api/admin/channel':
                        stream = str(body.get('stream', '')).strip()
                        if stream and not stream.startswith(('https://', '/media/')):
                            return self.send_json({'error': 'Use an HTTPS stream URL.'}, 400)
                        db.execute('UPDATE channels SET stream=?,enabled=? WHERE id=?', (stream, bool(body.get('enabled')), int(body['id'])))
                        audit(db, u['id'], 'Updated channel '+str(body['id']))
                        return self.send_json({'ok': True})
                return self.send_json({'error': 'Not found.'}, 404)
        except (ValueError, KeyError, TypeError):
            return self.send_json({'error': 'Invalid request.'}, 400)

    def webhook(self, raw):
        secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
        if not secret:
            return self.send_json({'error': 'Billing not configured.'}, 503)
        try:
            parts = urllib.parse.parse_qs(self.headers.get('Stripe-Signature', '').replace(',', '&'))
            timestamp = parts['t'][0]
            signature = hmac.new(secret.encode(), timestamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
            if abs(time.time()-int(timestamp)) > 300 or not any(hmac.compare_digest(signature, v) for v in parts.get('v1', [])):
                raise ValueError()
            event = json.loads(raw)
            with connect() as db:
                if db.execute('SELECT 1 FROM payments WHERE id=?', (event['id'],)).fetchone():
                    return self.send_json({'ok': True})
                obj = event['data']['object']
                if event['type'] == 'invoice.paid':
                    # Supports Stripe invoice schemas before and after 2025-03-31.
                    details = obj.get('subscription_details') or obj.get('parent', {}).get('subscription_details', {})
                    uid = int(details.get('metadata', {})['user_id'])
                    until = max(line['period']['end'] for line in obj['lines']['data'])
                    db.execute('UPDATE users SET paid_until=MAX(paid_until,?) WHERE id=?', (until, uid))
                    audit(db, uid, 'Subscription payment confirmed')
                db.execute('INSERT INTO payments VALUES(?)', (event['id'],))
            return self.send_json({'ok': True})
        except (ValueError, KeyError, TypeError):
            return self.send_json({'error': 'Invalid webhook.'}, 400)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3000)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--create-admin', metavar='EMAIL')
    args = parser.parse_args()
    initialize()
    if args.create_admin:
        password = getpass.getpass('Admin password (minimum 12 characters): ')
        if len(password) < 12:
            raise SystemExit('Password must have at least 12 characters.')
        with connect() as db:
            db.execute("INSERT INTO users(email,name,password,role,created) VALUES(?,?,?,'admin',?)", (args.create_admin.lower(), 'Administrator', password_hash(password), time.time()))
        print('Administrator created. Sign in through the app.')
    else:
        print(f'FANnTV listening on port {args.port}', flush=True)
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
