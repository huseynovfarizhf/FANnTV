# FANnTV

IPTV web application for Azerbaijan, with mobile layouts, remote-friendly TV mode, a platform administrator dashboard, and family accounts. Python 3.12+, SQLite, and browser JavaScript. Fonts, HLS playback, artwork, and a generated demo video are included locally.

## Run

```sh
cd /workspace/FANnTV
python3 server.py --port 3000
```

Open port 3000 through your development environment's viewer. Create an account to start a six-day free trial. NATURE+ plays a generated ten-second sample; other channels need your licensed sources.

Create the single platform administrator:

```sh
python3 server.py --create-admin your-email@example.com
```

Enter a unique password at the hidden prompt. There are no default credentials. Sign in normally to access platform management. Only one platform administrator is permitted; migration stops for operator review if an older database contains multiple administrators. Existing checkouts and databases are retained. Data lives in ignored `instance/fanntv.db`, or the location selected by `FANNTV_DB`. Back up the database before deployment.

## Account roles and devices

- **Platform administrator:** manages users, channels, global/customer prices, confirmed payment receipts, and activity logs.
- **Family owner:** pays for the household, adds sub-users, manages devices, and configures eligible parental controls. Each registered subscriber is the sole owner of their own household.
- **Sub-users:** have separate email/password logins and favorites. They inherit their owner's service period and suspension, and cannot pay or manage the household.

Each household includes one regular sub-user. A six-month package adds one extra sub-user place for six calendar months. That extra login stops working when its benefit expires. The regular member remains attached to the household, subject to its service expiry.

A household can have up to **four signed-in device sessions across the owner and all sub-users combined**. This conservatively enforces four-device access; an idle signed-in device occupies a place until sign-out, revocation, or session expiry (30 days). Tabs sharing one browser session use one place. A fifth sign-in is rejected. The owner can revoke another device from Family & devices. Device admission and payment settlement use serialized database transactions to prevent concurrent requests exceeding limits.

## Packages and parental controls

Monthly, six-month, and yearly packages are prepaid purchases. A verified payment activates service or extends its existing paid expiry. Terms use calendar months in **Asia/Baku**, clamped to a valid day in the destination month. Trials remain six elapsed 24-hour periods.

Each six-month purchase also grants six calendar months of **parental controls and one additional sub-user**. Their expiry is tracked separately from service expiry. Renewing that package extends the benefit period from its current end; unrelated prepaid service does not multiply the bonus duration. The yearly package does not implicitly grant six-month family bonuses.

While parental controls are active, the owner can choose allowed channel categories per sub-user. No selected categories blocks all channels; all categories permits unrestricted browsing. The server checks restrictions for channel lists, playback URLs, demo media, and player heartbeats. Existing restrictions remain enforced when the editing entitlement expires. Sub-users cannot change them.

The app periodically refreshes entitlements; playback rechecks occur every 15 seconds. The protected local demo checks access on every request. External stream URLs still require provider-side signed URLs, short-lived playback tokens or DRM for protection outside this application. URL admission and client heartbeats cannot revoke an external URL already copied to another player.

## AZN pricing and payments

All package amounts are **Azerbaijan manat (AZN)**. They are stored as integer **qəpik**, where 100 qəpik = 1 AZN. Initial example prices are 4.99 AZN monthly, 24.99 AZN for six months, and 39.99 AZN yearly. Set your actual prices in the admin dashboard before accepting payments. No exchange-rate conversion is performed.

The platform admin can increase/decrease each global package price or give a specific household its own price. A customer override takes precedence; removing it restores the global price. Changes apply to new purchases. Checkouts retain their frozen quoted amount and duration.

Service activates **only after a verified paid notification whose currency and amount match the stored order**, or after the platform admin records a payment they have independently verified. Unpaid notifications, mismatched amounts/currencies, return URLs, and duplicate receipt retries cannot extend service. A receipt is globally unique. The admin receipt flow shows the current price and requires confirmation; it does not initiate a bank transfer or charge a card. A stale confirmed price is rejected.

### Bank acquiring

The requested bank/acquiring provider has not yet been named. A bank-adapter integration contract is provided so a bank-specific connector can be added without moving price or entitlement decisions into the browser. This is **not a completed direct integration with an unnamed bank**. Live charges are unavailable until the chosen provider's API, merchant credentials, HTTPS callback URL, and adapter are configured. Never enter credentials in source control or chat.

See [PAYMENTS.md](PAYMENTS.md) for the adapter callback contract and optional Stripe configuration. Payment-return screens poll the stored order status and never activate access themselves. Bank payment acceptance, settlement, refunds, disputes, and real Azerbaijani-network access have not been validated. Refunds/disputes currently require manual suspension.

## Languages

**Azərbaycanca, English, Русский, 简体中文 (Simplified Chinese)** cover welcome, user/TV controls, subscriptions, family management, admin, errors, and activity labels. Choose a language before sign-in or in the header/settings. The device remembers the choice. A supported browser language is used initially; other languages fall back to Azerbaijani. User names, channel brands, emails, and stream URLs are preserved.

Search understands translated categories. Dates and times use Asia/Baku with locale formatting. Built-in browser validation and native video controls follow the browser/device language. Message catalogs are in `static/i18n.js`. AZN is explicitly shown alongside localized numbers.

## Development and validation

Python runtime uses the standard library. Normal startup needs no npm install because browser assets are vendored. To refresh assets:

```sh
npm ci --ignore-scripts
npm run assets
```

API integration tests:

```sh
npm test
```

Browser workflows with Chromium installed:

```sh
npm run test:browser
npm run test:family
```

Set `CHROMIUM_PATH` if needed. Tests use isolated temporary databases; browser screenshots go to ignored `test-results/`. They exercise trial expiry, inherited family access, devices, parental restrictions, prices, verified payment activation and replay handling, calendar terms, localization, video playback, and administrator workflows. Real bank charges are not exercised.

## Production deployment

See [PRODUCTION.md](PRODUCTION.md) for the production review, Linux VPS installation, HTTPS, SMTP account verification/recovery, protected playback, backups, monitoring, and remaining launch requirements. Production uses the pinned dependencies in `requirements.txt` and `wsgi:application` under Gunicorn; the development server refuses production mode. SQLite migrations preserve existing accounts, which must verify their email before production playback/purchases (platform admin exempt).

Additional checks: `npm run test:auth` uses a mail fixture; `npm run test:production` exercises the actual Gunicorn workers with local proxy headers (install production dependencies in `.venv` first). No real emails, bank charges, or provider streams are used.

## Deployment and store releases

This is a web application, not published native/store packages. Desktop Chromium and mobile viewport tests do not certify physical TV/mobile hardware. Before public launch, configure and validate the included HTTPS hosting, backups, monitoring and email verification/recovery; complete billing self-service, staff access protection and real provider-side playback integration. The Python standard-library development server is not a public production edge.

Android/iOS store billing and packages, Samsung Tizen, LG webOS, and Android/Google TV packaging for Sony remain separate release work. Program guides, M3U imports, catch-up TV, and full video-on-demand catalogs are not included. Connect only content you are authorized to distribute.

Alpine artwork and the demo clip are generated assets. Library/font license texts are retained under `static/vendor/`.
