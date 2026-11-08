"""Bounded WSGI transport for the existing application logic.

The handler writes to a private spool. Its DB context exits (commits/rolls back)
before any headers/body are released to the network. Gunicorn owns HTTP parsing,
worker lifecycle, connection timeouts, and concurrent request limits.
"""
import email.message
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
from http import HTTPStatus
from urllib.parse import urlsplit

import production
import server

LOGGER = logging.getLogger('fanntv')
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
MAX_BODY = 32768
SECURITY_HEADERS = [
    ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'), ('Permissions-Policy', 'camera=(), microphone=(), geolocation=()'),
    ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; media-src 'self' https: blob:; connect-src 'self' https:; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
]


class RequestHandler(server.Handler):
    def __init__(self, environ, body, output):
        self.command = environ['REQUEST_METHOD']
        self.path = environ.get('PATH_INFO', '/')
        # Application routes ignore query strings, but payment return is handled by the browser.
        if environ.get('QUERY_STRING'):
            self.path += '?' + environ['QUERY_STRING']
        self.request_version = 'HTTP/1.1'
        self.requestline = self.command  # Never retain token-bearing URLs in access logs.
        self.client_address = (production.client_address(environ), 0)
        self.headers = email.message.Message()
        for key, value in environ.items():
            if key.startswith('HTTP_'):
                self.headers[key[5:].replace('_', '-')] = str(value)
        self.headers['Content-Length'] = str(len(body))
        if environ.get('CONTENT_TYPE'):
            self.headers['Content-Type'] = environ['CONTENT_TYPE']
        self.rfile = io.BytesIO(body)
        self.wfile = output
        self.status = 200
        self.response_headers = []
        self.close_connection = True

    def send_response(self, code, message=None):
        self.status = code
        self.response_headers = []

    def send_header(self, keyword, value):
        self.response_headers.append((keyword, str(value)))

    def end_headers(self):
        pass


def reject(start_response, code, message, request_id):
    data = json.dumps({'error': message, 'request_id': request_id}).encode()
    headers = SECURITY_HEADERS + [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'),
                                  ('Content-Length', str(len(data))), ('X-Request-ID', request_id)]
    if production.enabled():
        headers.append(('Strict-Transport-Security', 'max-age=31536000'))
    start_response(f'{code} {HTTPStatus(code).phrase}', headers)
    return [data]


def create_application():
    production.validate_startup()
    server.initialize()

    def application(environ, start_response):
        request_id = secrets.token_hex(12)
        started = time.monotonic()
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')
        if path in ('/healthz', '/readyz') and method in ('GET', 'HEAD'):
            try:
                if path == '/readyz':
                    with server.connect() as db:
                        db.execute('SELECT 1 FROM users LIMIT 1').fetchone()
                payload = b'{"status":"ok"}'
                start_response('200 OK', [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'), ('Content-Length', str(len(payload))), ('X-Request-ID', request_id)])
                return [] if method == 'HEAD' else [payload]
            except sqlite3.Error:
                return reject(start_response, 503, 'Service is not ready.', request_id)
        if method not in ('GET', 'POST', 'HEAD'):
            return reject(start_response, 405, 'Method not allowed.', request_id)
        if production.enabled():
            origin = production.public_origin()
            host = environ.get('HTTP_HOST', '').lower()
            if host != urlsplit(origin).netloc.lower():
                return reject(start_response, 400, 'Invalid request host.', request_id)
            if environ.get('wsgi.url_scheme') != 'https':
                return reject(start_response, 400, 'HTTPS is required.', request_id)
            supplied_origin = environ.get('HTTP_ORIGIN')
            if supplied_origin and supplied_origin.rstrip('/').lower() != origin:
                return reject(start_response, 403, 'Origin not allowed.', request_id)
        try:
            length = int(environ.get('CONTENT_LENGTH') or 0)
            if length < 0 or length > MAX_BODY or environ.get('HTTP_TRANSFER_ENCODING'):
                return reject(start_response, 413, 'Request too large or invalid framing.', request_id)
            if method == 'POST' and environ.get('CONTENT_TYPE', '').split(';')[0].strip().lower() != 'application/json':
                return reject(start_response, 415, 'Use application/json.', request_id)
            body = environ['wsgi.input'].read(length) if method == 'POST' else b''
            if len(body) != length and method == 'POST':
                return reject(start_response, 400, 'Incomplete request body.', request_id)
        except (ValueError, TypeError):
            return reject(start_response, 400, 'Invalid request framing.', request_id)
        output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        try:
            request = RequestHandler({**environ, 'REQUEST_METHOD': 'GET' if method == 'HEAD' else method}, body, output)
            if method in ('GET', 'HEAD'):
                request.do_GET()
            else:
                request.do_POST()
            # The handler and all transaction context managers have returned here.
            headers = [(key, value) for key, value in request.response_headers
                       if key.lower() not in {k.lower() for k, _ in SECURITY_HEADERS}]
            headers.extend(SECURITY_HEADERS)
            headers.append(('X-Request-ID', request_id))
            if production.enabled():
                headers.append(('Strict-Transport-Security', 'max-age=31536000'))
            output.seek(0)
            start_response(f'{request.status} {HTTPStatus(request.status).phrase}', headers)
            LOGGER.info(json.dumps({'event': 'http_request', 'request_id': request_id, 'method': method,
                                    'status': request.status, 'duration_ms': round((time.monotonic()-started)*1000)}))
            if method == 'HEAD':
                output.close()
                return []
            def chunks():
                try:
                    while chunk := output.read(65536):
                        yield chunk
                finally:
                    output.close()
            return chunks()
        except Exception as error:
            output.close()
            # Error class only; provider exceptions may embed tokens, credentials or customer data.
            LOGGER.error(json.dumps({'event': 'request_failed', 'request_id': request_id, 'error_type': type(error).__name__}))
            return reject(start_response, 500, 'Unexpected server error.', request_id)
    return application


application = create_application()
