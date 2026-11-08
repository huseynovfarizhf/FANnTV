"""Single-host production workers behind the local Caddy HTTPS proxy."""
bind = '127.0.0.1:3000'
workers = 2
threads = 4
worker_class = 'gthread'
preload_app = True
worker_tmp_dir = '/run/fanntv'
timeout = 30
graceful_timeout = 30
keepalive = 5
max_requests = 2000
max_requests_jitter = 200
limit_request_line = 4094
limit_request_fields = 64
limit_request_field_size = 8190
forwarded_allow_ips = '127.0.0.1'
secure_scheme_headers = {'X-FORWARDED-PROTO': 'https'}
accesslog = '-'
# No request URLs, queries, IPs, cookies, or authentication headers in logs.
access_log_format = '%(m)s %(s)s %(L)s %({x-request-id}o)s'
errorlog = '-'
loglevel = 'info'
capture_output = True
