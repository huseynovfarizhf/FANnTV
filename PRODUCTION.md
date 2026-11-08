# Production review and deployment guide

The repository now includes a production transport and deployment templates. **It is not yet a publicly launched paid IPTV service.** Use a private staging installation first. This review covers the current web application; native/store releases remain separate.

## Findings and changes

| Area | Implemented in this change | Remaining launch requirement |
| --- | --- | --- |
| Account recovery | TLS SMTP, email verification, one-use hashed recovery/verification tokens, expiry, abuse limits, password change, household session revocation | Configure sender domain/SMTP, verify actual inbox delivery and recovery in staging; mail text currently English |
| Production HTTP | Gunicorn WSGI workers, bounded bodies, canonical host/origin checks, trusted proxy identity, CSP/security headers, secure cookies, production-mode guard | Install on chosen server, configure DNS/HTTPS/firewall and validate Caddy/systemd there |
| Payments | No authenticated redirects, bounded responses, durable acknowledgement after commit, retained AZN quote/replay protection | Actual AZN bank adapter, merchant account, 3-D Secure, real charge/settlement and refund/dispute workflows |
| Playback | Production rejects raw external source URLs; trusted short-lived URL adapter; authorization recheck after provider I/O; client renews expiring URLs | Licensed provider/CDN must actually enforce short-lived tokens, stream renewals and device/session revocation; test on real devices |
| Storage/operations | Persistent SQLite/WAL, explicit connection cleanup, limited last-seen writes, safe request IDs, liveness/readiness, live-consistent verified backups | Encrypted off-site backup, restore drill, monitoring/alerts, capacity test and operating owner |
| Regression checks | CI for APIs, backups, production transport and browsers | GitHub Actions run and target-host acceptance after deployment |
| Staff access | Single admin, password limits, hashed passwords and session revocation | Add administrator MFA or enforce a trusted VPN/access gateway before publicly exposing staff controls |

Security defects corrected during review include cross-host payment redirects forwarding credentials, API success emitted before database commit, request-wide SQLite locks around remote playback calls, untrusted proxy/rate-limit identities, and permanent raw source URLs leaving production.

The application still lacks customer invoice downloads, automated refund/chargeback reconciliation, formal privacy/account-deletion/export workflows, and support tooling. Publish actual terms, privacy and refund information, support contact, and content/licensing details before charging the public. Do not collect card numbers, expiry dates or CVV in FANnTV; use the acquiring provider's HTTPS checkout.

## Suggested first hosting setup

A Linux VPS with 2 vCPUs and 4 GB RAM is a reasonable **small-pilot starting point**, not a measured capacity guarantee. Use Ubuntu 24.04 LTS or another maintained Linux distribution with Python 3.12+. Add a domain, persistent disk, a firewall, backups and a monitoring service. Keep the database and private configuration on persistent storage. This is a single-host SQLite deployment; do not place the database on a network filesystem or start multiple independent app replicas sharing it. Reassess PostgreSQL/queues when load or high availability requires them.

Licensed video should flow through the provider/CDN to viewers. The VPS hosts account management and requests signed playback URLs; this setup is not a video distribution/transcoding server. Stream bandwidth and concurrent viewer capacity must be agreed with the video provider.

## Install on the chosen Linux VPS

These are operator instructions, not actions already performed in this cloud workspace. Install Git, Python 3.12+ with venv, and Caddy through your distribution/official installation instructions. Point the domain's DNS at the server. Restrict SSH to administrators and expose only 80/443 publicly; **port 3000 must stay on loopback**. Use SSH keys and OS security updates.

Prepare an unprivileged service user and code directory:

```sh
sudo useradd --system --home /var/lib/fanntv --shell /usr/sbin/nologin fanntv
sudo git clone https://github.com/huseynovfarizhf/FANnTV.git /opt/fanntv
sudo python3 -m venv /opt/fanntv/.venv
sudo /opt/fanntv/.venv/bin/python -m pip install -r /opt/fanntv/requirements.txt
sudo install -d -m 0700 -o fanntv -g fanntv /var/lib/fanntv
sudo install -d -m 0700 /etc/fanntv
sudo install -m 0600 /opt/fanntv/deploy/production.env.example /etc/fanntv/fanntv.env
```

Edit `/etc/fanntv/fanntv.env` securely. Set the real HTTPS origin and SMTP settings. Use TLS or STARTTLS with certificate verification. Configure bank and playback adapter URLs/credentials only when those integrations exist. The HMAC webhook secret must be a strong random value of at least 32 characters, available to the server as an actual runtime secret (not a network-proxy placeholder). Do not put secrets in Git or chat.

Create the single platform administrator with a hidden password prompt; preserve an existing database/admin instead of recreating it:

```sh
cd /opt/fanntv
sudo -u fanntv env FANNTV_DB=/var/lib/fanntv/fanntv.db /opt/fanntv/.venv/bin/python server.py --create-admin your-email@example.com
```

Install the app and backup units:

```sh
sudo install -m 0644 deploy/fanntv.service deploy/fanntv-backup.service deploy/fanntv-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/fanntv.service /etc/systemd/system/fanntv-backup.service /etc/systemd/system/fanntv-backup.timer
sudo systemctl enable --now fanntv
```

The app runs from a protected code directory as `fanntv`, with writable state only in its persistent/runtime directories. Gunicorn listens at `127.0.0.1:3000` with two workers/four threads each. Production startup refuses missing HTTPS/SMTP configuration. `python server.py` is development-only and refuses production mode.

## Configure Caddy HTTPS

Copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`. Set `FANNTV_DOMAIN` (for example `tv.example.az`) and `ACME_EMAIL` in a root-owned Caddy environment file, and load it from a systemd override for the existing Caddy service:

```ini
[Service]
EnvironmentFile=/etc/caddy/fanntv.env
```

Validate the Caddyfile with those environment values on the target host before reloading Caddy. Refer to the installed Caddy version's CLI/environment-loading options. Caddy supplies certificates and HTTP-to-HTTPS redirects. The supplied configuration replaces forwarded IP/protocol headers and proxies every path through the application. **Do not add a direct static file server for the demo video.** Keep `FANNTV_TRUSTED_PROXIES` restricted to the actual local proxy.

`/healthz` is liveness and `/readyz` checks the database can be queried. They do not prove SMTP, bank settlement, off-site backups, or stream-provider health. Configure external HTTPS monitoring and alert on repeated 5xx, failed backups, disk pressure and payment callback failures. Logs include generated request IDs and safe outcomes; never enable logs of request bodies, cookies, Authorization, reset URLs or provider responses.

## Signed playback adapter

Set `FANNTV_PLAYBACK_ADAPTER_URL` and `FANNTV_PLAYBACK_ADAPTER_KEY` for your trusted connector. Production rejects external playback if these are missing. The app POSTs JSON over verified HTTPS with Bearer authentication:

```json
{"channel_id":1,"user_id":42,"session_id":"opaque-device-id","source":"https://private-provider-source.example/live.m3u8","min_ttl_seconds":45,"max_ttl_seconds":120}
```

The source is server-held configuration, never returned to the browser in production. The adapter must issue a provider-enforced signed URL and reply:

```json
{"url":"https://cdn.example/short-lived-playback-token","expires_at":1893456090}
```

`expires_at` is Unix seconds and must be 45–120 seconds from the response. Redirects are rejected. FANnTV rechecks session/entitlement/category after the remote request, then returns the signed URL and expiry. The player renews before fewer than 30 seconds remain, preserving VOD position/paused state where supported. The provider must enforce token expiry/session binding for manifests and segments, handle renewal, and stop revoked access. This adapter contract alone cannot guarantee DRM or revoke already buffered content. Validate live HLS, seeking, native Safari playback and TV hardware with the selected provider.

## Backups and restore

Enable the timer only after a database exists:

```sh
sudo systemctl start fanntv-backup.service
sudo systemctl enable --now fanntv-backup.timer
sudo systemctl list-timers fanntv-backup.timer
```

The root-owned job makes a consistent SQLite backup, checks integrity, restores into a temporary database and verifies it, then publishes a private snapshot under `/var/lib/fanntv-backups`. It retains 14 managed verified snapshots; unrelated files are not removed. The timer runs daily at 03:15 UTC (07:15 Azerbaijan time) with up to 15 minutes of jitter. Ensure both DB and backup disk capacity are monitored. Local copies do not protect against server loss: add encrypted off-site storage with separate access credentials and a retention schedule.

Before every release, take a verified backup. For a restore drill on a staging host: stop the app, preserve the current DB and its `-wal`/`-shm` companions, restore a verified snapshot to a fresh path, set ownership/mode 0600 for `fanntv`, run integrity checks, then start the app and test representative accounts/entitlements. Restore should be performed while the app is stopped; never overwrite a live SQLite database. Keep the prior database and matching code release until the drill succeeds.

## Preflight and acceptance

Run `python production.py --check` with the deployment's environment loaded securely by your service/operations tooling. It reads existing data without modifying it and reports local prerequisites without secret values. It intentionally fails while bank/stream integrations or an admin are missing. A pass does not certify external services, rights, throughput or store acceptance.

Before public paid launch, verify on staging:

1. DNS, certificates, firewall, HTTPS redirect, secure sessions, health alerts, log redaction and server reboot recovery.
2. Registration, actual email verification/recovery, expired/reused links, session revocation, staff MFA/access-gateway restrictions and rate limits from separate real client IPs.
3. A real low-value AZN card payment using the chosen provider, 3-D Secure, callback retries, failed/pending payment, exact quote activation, refunds/disputes, and bank settlement reconciliation.
4. Licensed live and VOD playback, token renewal/expiry, parental restrictions, owner suspension, four-device admission/revocation and long viewing sessions on target mobile/TV hardware and Azerbaijani networks.
5. A restore onto a fresh staging machine from the off-site backup, plus a representative load/soak test. The included 24-request smoke is functional evidence only, not a production capacity benchmark.
6. Customer-facing terms/privacy/refund pages, content rights, support and incident ownership. If distributing through stores, complete platform packages, store billing integration and review separately.

CI, local SMTP fixtures, mocked banks and generated video do not substitute for these live acceptance checks. This workspace has not deployed a public server or issued a real TLS certificate; Caddy and systemd units must be validated on the selected host.
