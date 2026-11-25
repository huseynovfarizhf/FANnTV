"""Short-lived signaling for two authenticated browser sessions."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time

INVITE_LIFETIME = 600
PRESENCE_LIFETIME = 60
CALL_LIFETIME = 4 * 3600


class CallError(Exception):
    def __init__(self, message, status):
        self.message, self.status = message, status


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS video_calls(
            id TEXT PRIMARY KEY, host TEXT NOT NULL, guest TEXT,
            created REAL NOT NULL, host_seen REAL NOT NULL, guest_seen REAL,
            offer TEXT, answer TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS video_call_host ON video_calls(host);
        CREATE UNIQUE INDEX IF NOT EXISTS video_call_guest ON video_calls(guest);
    """)


def ice_servers(now):
    servers = []
    stun = os.environ.get('FANNTV_STUN_URL', 'stun:stun.l.google.com:19302')
    if stun:
        servers.append({'urls': stun})
    urls = os.environ.get('FANNTV_TURN_URLS', '').split()
    secret = os.environ.get('FANNTV_TURN_SECRET')
    if urls and secret:
        username = f'{int(now + CALL_LIFETIME + INVITE_LIFETIME)}:{secrets.token_hex(8)}'
        digest = hmac.digest(secret.encode(), username.encode(), 'sha1')
        servers.append({'urls': urls, 'username': username,
                        'credential': base64.b64encode(digest).decode()})
    return servers


def call_id(invite):
    return hashlib.sha256(invite.encode()).hexdigest()


def manage(db, action, body, session):
    now = time.time()
    db.execute('''DELETE FROM video_calls WHERE
        host_seen <= ? OR host NOT IN (SELECT token FROM sessions WHERE expires > ?)
        OR (guest IS NULL AND created <= ?)
        OR (guest IS NOT NULL AND (guest_seen <= ? OR created <= ?
            OR guest NOT IN (SELECT token FROM sessions WHERE expires > ?)))''',
        (now - PRESENCE_LIFETIME, now, now - INVITE_LIFETIME,
         now - PRESENCE_LIFETIME, now - CALL_LIFETIME, now))

    if action == 'create':
        require_available(db, session)
        invite = secrets.token_urlsafe(24)
        db.execute('INSERT INTO video_calls(id,host,created,host_seen) VALUES(?,?,?,?)',
                   (call_id(invite), session, now, now))
        return {'invite': invite, 'ice_servers': ice_servers(now)}

    invite = body.get('invite')
    if not isinstance(invite, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', invite):
        raise CallError('Invalid video call invitation.', 400)
    room_id = call_id(invite)
    room = db.execute('SELECT * FROM video_calls WHERE id=?', (room_id,)).fetchone()
    if not room:
        raise CallError('This video call has ended or the invitation expired.', 404)

    if action == 'join':
        if room['guest'] or room['host'] == session:
            raise CallError('This video call invitation is unavailable.', 409)
        require_available(db, session)
        db.execute('UPDATE video_calls SET guest=?,guest_seen=? WHERE id=?', (session, now, room_id))
        return {'ice_servers': ice_servers(now)}

    host = room['host'] == session
    if not host and room['guest'] != session:
        raise CallError('This video call is unavailable.', 404)
    if action == 'end':
        db.execute('DELETE FROM video_calls WHERE id=?', (room_id,))
        return {'ok': True}
    if action != 'signal':
        raise CallError('Not found.', 404)

    description = body.get('sdp')
    own, peer = ('offer', 'answer') if host else ('answer', 'offer')
    if description is not None:
        if (not isinstance(description, str) or not description.startswith('v=0')
                or len(description) > 24000 or not room['guest']
                or (not host and not room['offer'])):
            raise CallError('Invalid video call signal.', 400)
        if room[own] is not None and room[own] != description:
            raise CallError('Video call signal already set.', 409)
        db.execute(f'UPDATE video_calls SET {own}=? WHERE id=?', (description, room_id))
    seen = 'host_seen' if host else 'guest_seen'
    db.execute(f'UPDATE video_calls SET {seen}=? WHERE id=?', (now, room_id))
    return {'joined': room['guest'] is not None, 'sdp': room[peer]}


def require_available(db, session):
    if db.execute('SELECT 1 FROM video_calls WHERE host=? OR guest=?', (session, session)).fetchone():
        raise CallError('End your current video call before starting another.', 409)
