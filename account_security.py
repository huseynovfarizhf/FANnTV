"""Account mail, one-use token storage, and durable abuse limits.

No reset/verification token or email link is logged. SMTP always uses TLS.
"""
import hashlib
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import urlsplit

VERIFY_TTL = 24 * 3600
RESET_TTL = 30 * 60
GENERIC_RESET_MESSAGE = 'If the account exists, a password reset email will be sent.'


class LimitExceeded(Exception):
    pass


def production_mode():
    return os.environ.get('FANNTV_ENV', '').strip().lower() == 'production'


def valid_email(value):
    if not isinstance(value, str) or len(value) > 254 or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    name, address = parseaddr(value)
    return not name and address == value and address.count('@') == 1 and all(address.split('@'))


def configuration_errors():
    errors = []
    host = os.environ.get('FANNTV_SMTP_HOST', '')
    sender = os.environ.get('FANNTV_SMTP_FROM', '')
    if not host or any(c.isspace() for c in host):
        errors.append('FANNTV_SMTP_HOST must name the SMTP server.')
    if not valid_email(sender):
        errors.append('FANNTV_SMTP_FROM must be a valid sender email address.')
    mode = os.environ.get('FANNTV_SMTP_MODE', 'starttls').lower()
    if mode not in ('starttls', 'tls'):
        errors.append('FANNTV_SMTP_MODE must be starttls or tls.')
    try:
        port = int(os.environ.get('FANNTV_SMTP_PORT', '465' if mode == 'tls' else '587'))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        errors.append('FANNTV_SMTP_PORT must be a valid TCP port.')
    if bool(os.environ.get('FANNTV_SMTP_USERNAME')) != bool(os.environ.get('FANNTV_SMTP_PASSWORD')):
        errors.append('Set both FANNTV_SMTP_USERNAME and FANNTV_SMTP_PASSWORD, or neither for a trusted relay.')
    try:
        origin = urlsplit(os.environ.get('PUBLIC_URL', ''))
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/'):
            raise ValueError()
    except ValueError:
        errors.append('PUBLIC_URL must be the public HTTPS origin for account links.')
    return errors


def email_delivery_ready():
    return not configuration_errors()


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS account_tokens(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, purpose TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS account_tokens_user ON account_tokens(user_id,purpose);
    CREATE TABLE IF NOT EXISTS account_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, reset REAL NOT NULL);
    ''')


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def take_limit(db, scope, value, limit, window):
    """Caller serializes with BEGIN IMMEDIATE and commits even on denied attempts."""
    key = scope + ':' + digest(value)
    now = time.time()
    row = db.execute('SELECT count,reset FROM account_limits WHERE key=?', (key,)).fetchone()
    if row and row['reset'] > now and row['count'] >= limit:
        raise LimitExceeded()
    db.execute('''INSERT INTO account_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN reset<=? THEN 1 ELSE count+1 END,reset=CASE WHEN reset<=? THEN excluded.reset ELSE reset END''', (key, now + window, now, now))


def issue_token(db, uid, purpose):
    if purpose not in ('verify', 'reset'):
        raise ValueError('Invalid account token purpose.')
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute('DELETE FROM account_tokens WHERE user_id=? AND purpose=?', (uid, purpose))
    db.execute('DELETE FROM account_tokens WHERE expires<=?', (now,))
    db.execute('INSERT INTO account_tokens VALUES(?,?,?,?,?)', (digest(token), uid, purpose, now + (VERIFY_TTL if purpose == 'verify' else RESET_TTL), now))
    return token


def consume_token(db, token, purpose):
    if not isinstance(token, str) or not 32 <= len(token) <= 256:
        return None
    row = db.execute('SELECT * FROM account_tokens WHERE token_hash=? AND purpose=? AND expires>?', (digest(token), purpose, time.time())).fetchone()
    if not row:
        return None
    db.execute('DELETE FROM account_tokens WHERE token_hash=?', (digest(token),))
    return row['user_id']


def send_account_email(recipient, purpose, token):
    """Use a configured SMTP relay only; exceptions never expose links or secrets."""
    if not email_delivery_ready():
        raise RuntimeError('Email delivery is not configured.')
    if not valid_email(recipient) or purpose not in ('verify', 'reset'):
        raise ValueError('Invalid account email.')
    url = os.environ['PUBLIC_URL'].rstrip('/') + '/#' + purpose + '=' + token
    message = EmailMessage()
    message['From'] = os.environ['FANNTV_SMTP_FROM']
    message['To'] = recipient
    if purpose == 'verify':
        message['Subject'] = 'FANnTV: verify your email address'
        text = 'Confirm your email address to finish setting up your FANnTV account. This link expires in 24 hours.'
    else:
        message['Subject'] = 'FANnTV: reset your password'
        text = 'Use this link to reset your FANnTV password. It expires in 30 minutes. If you did not request this, ignore this message.'
    message.set_content(text + '\n\n' + url + '\n\nNever share this link with anyone.')
    mode = os.environ.get('FANNTV_SMTP_MODE', 'starttls').lower()
    port = int(os.environ.get('FANNTV_SMTP_PORT', '465' if mode == 'tls' else '587'))
    context = ssl.create_default_context()
    if mode == 'tls':
        smtp = smtplib.SMTP_SSL(os.environ['FANNTV_SMTP_HOST'], port, timeout=10, context=context)
    else:
        smtp = smtplib.SMTP(os.environ['FANNTV_SMTP_HOST'], port, timeout=10)
    with smtp:
        if mode == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if os.environ.get('FANNTV_SMTP_USERNAME'):
            smtp.login(os.environ['FANNTV_SMTP_USERNAME'], os.environ['FANNTV_SMTP_PASSWORD'])
        smtp.send_message(message)
