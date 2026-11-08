"""Trusted playback-provider adapter. Raw source URLs never leave production API."""
import json
import os
import time
import urllib.request
from urllib.parse import urlsplit


class PlaybackError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secure_url(value):
    try:
        parts = urlsplit(value)
        return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password and '\\' not in value
    except (TypeError, ValueError):
        return False


def resolve_playback_details(channel, user, session_id):
    source = channel['stream']
    if source.startswith('/media/') or os.environ.get('FANNTV_ENV', '').strip().lower() != 'production':
        return {'url': source, 'expires_at': None}
    endpoint = os.environ.get('FANNTV_PLAYBACK_ADAPTER_URL', '')
    key = os.environ.get('FANNTV_PLAYBACK_ADAPTER_KEY', '')
    if not secure_url(endpoint) or not key:
        raise PlaybackError('Protected playback is not configured. Contact the administrator.')
    body = {'channel_id': channel['id'], 'user_id': user['id'], 'session_id': session_id,
            'source': source, 'min_ttl_seconds': 45, 'max_ttl_seconds': 120}
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            data = json.loads(response.read(32769))
        expires = data['expires_at']
        if (not secure_url(data['url']) or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not time.time() + 45 <= expires <= time.time() + 120):
            raise ValueError()
        return {'url': data['url'], 'expires_at': expires}
    except Exception as error:
        raise PlaybackError('The protected stream is unavailable. Please try again later.') from None


def resolve_playback(channel, user, session_id):
    return resolve_playback_details(channel, user, session_id)['url']
