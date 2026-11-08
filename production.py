"""Production configuration and preflight. No secret values are returned or logged."""
import argparse
import ipaddress
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


def enabled():
    return os.environ.get('FANNTV_ENV', 'development').strip().lower() == 'production'


def public_origin():
    value = os.environ.get('PUBLIC_URL', '').rstrip('/')
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or '\\' in value):
            return None
        _ = parts.port
    except ValueError:
        return None
    return parts.scheme + '://' + parts.netloc.lower()


def configuration_errors():
    if os.environ.get('FANNTV_ENV', 'development').strip().lower() not in ('development', 'production'):
        return ['FANNTV_ENV must be development or production.']
    if not enabled():
        return []
    errors = []
    if not public_origin():
        errors.append('PUBLIC_URL must be an HTTPS origin without credentials, path or query.')
    database = os.environ.get('FANNTV_DB', '')
    if not database or not Path(database).is_absolute():
        errors.append('FANNTV_DB must be an absolute path on persistent storage.')
    try:
        from account_security import configuration_errors as mail_errors
        errors.extend(mail_errors())
    except ImportError:
        errors.append('Account security module is unavailable.')
    webhook = os.environ.get('FANNTV_PAYMENT_WEBHOOK_SECRET', '')
    if webhook and len(webhook) < 32:
        errors.append('FANNTV_PAYMENT_WEBHOOK_SECRET must contain at least 32 characters.')
    try:
        for value in os.environ.get('FANNTV_TRUSTED_PROXIES', '127.0.0.1,::1').split(','):
            ipaddress.ip_address(value.strip())
    except ValueError:
        errors.append('FANNTV_TRUSTED_PROXIES must contain exact proxy IP addresses.')
    return errors


def validate_startup():
    errors = configuration_errors()
    if errors:
        raise RuntimeError('Production configuration invalid: ' + ' '.join(errors))


def client_address(environ):
    peer = environ.get('REMOTE_ADDR', '')
    trusted = {v.strip() for v in os.environ.get('FANNTV_TRUSTED_PROXIES', '127.0.0.1,::1').split(',')}
    forwarded = environ.get('HTTP_X_FORWARDED_FOR', '')
    if peer in trusted and forwarded:
        try:
            # Caddy overwrites this with one verified remote IP. No arbitrary chain trust.
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return peer
    return peer


def preflight():
    import server
    checks = [dict(name='production_configuration', ok=enabled() and not configuration_errors(),
                   details=configuration_errors() or ([] if enabled() else ['Set FANNTV_ENV=production for deployment.']))]
    try:
        with sqlite3.connect('file:' + str(Path(server.DB).resolve()) + '?mode=ro', uri=True) as db:
            database_ok = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            sources = db.execute("SELECT COUNT(*) FROM channels WHERE enabled=1 AND stream LIKE 'https://%'").fetchone()[0]
        checks.extend([dict(name='database_integrity', ok=database_ok), dict(name='platform_admin', ok=admins == 1),
                       dict(name='licensed_stream_sources_configured', ok=sources > 0)])
    except sqlite3.Error:
        checks.append(dict(name='database_integrity', ok=False, details=['Initialize and migrate the persistent database before checking.']))
    checks.extend([dict(name='payment_provider_configuration', ok=server.billing_ready()),
                   dict(name='protected_playback_adapter', ok=bool(os.environ.get('FANNTV_PLAYBACK_ADAPTER_URL','').startswith('https://') and os.environ.get('FANNTV_PLAYBACK_ADAPTER_KEY')))])
    return dict(checks=checks, passed=all(c['ok'] for c in checks),
                note='Local checks cannot verify provider settlement, content rights, email delivery, off-site backups, or store approval.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check local production prerequisites without changing data.')
    parser.parse_args()
    result = preflight()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
