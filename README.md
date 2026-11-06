# FANnTV

A working IPTV web application with responsive mobile layouts, a remote-friendly TV mode, and an administrator dashboard. Built with Python 3.12+, SQLite, and browser JavaScript. The application includes its fonts, HLS player, artwork, and a generated sample video locally; no CDN is required at runtime.

## Run

```sh
cd /workspace/FANnTV
python3 server.py --port 3000
```

Open the application through your environment's port viewer for port 3000, or open `http://localhost:3000` on your own machine. Create an account to start a six-day trial. The NATURE+ channel plays a ten-second generated sample clip. Other channels are placeholders until an administrator connects authorized sources.

Create your administrator account in a separate terminal:

```sh
python3 server.py --create-admin your-email@example.com
```

Enter a unique password at the hidden prompt, then sign in normally. The Admin dashboard appears for administrator accounts. There are no built-in administrator credentials. Use the dashboard to inspect users, suspend/restore access, see activity, and enable or configure streams. SQLite data is retained in the ignored `instance/` directory. Back it up before replacing an environment. Override its location with `FANNTV_DB` if necessary.

## Included behavior

- Welcome, account registration, sign in/out, and persistent HttpOnly cookie sessions.
- Server-controlled six-day trial beginning at registration. An expired or suspended account cannot obtain playback URLs through the API.
- Channel categories, search, favorites saved per account, MP4 playback, native HLS, and HLS.js fallback where MediaSource is supported.
- Subscription selection, Stripe Checkout integration, and signature-verified, replay-safe `invoice.paid` notifications. Merely visiting a success URL does not activate access.
- Administrator-only user management, channel source configuration, and recent activity (latest 100 entries).
- Mobile navigation, install metadata, keyboard accessibility, and TV mode. In TV mode use arrows and Enter; Escape closes dialogs or returns home. Press `/` to search.

## Payments

The displayed $4.99 monthly and $39.99 annual prices are illustrative. Set your actual product prices in Stripe and keep the application display in sync before launch. Checkout always shows Stripe's authoritative price.

Configure these values securely in the runtime environment, never in source control:

| Variable | Purpose |
| --- | --- |
| `PUBLIC_URL` | External HTTPS origin of this deployment, without a trailing slash |
| `STRIPE_SECRET_KEY` | Stripe secret API key |
| `STRIPE_MONTHLY_PRICE` | Recurring monthly Stripe Price ID |
| `STRIPE_YEARLY_PRICE` | Recurring annual Stripe Price ID |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for your webhook endpoint |

Register `PUBLIC_URL/api/stripe/webhook` for `invoice.paid` events. Checkout writes the internal user ID to subscription metadata. The webhook reads invoice subscription metadata and extends access to the paid billing period's end. Cancellation leaves access available until that date. Payment failures do not extend access. Refunds and disputes require manual suspension in this version. Checkout needs outbound access to `api.stripe.com`; channel providers must allow the client to load video, and HLS sources must supply appropriate CORS headers. No live payment or external stream provider has been configured or tested.

## Development and validation

Runtime Python uses only the standard library. Vendored browser assets are included, so starting the server does not need npm. To refresh those assets from the pinned npm lockfile:

```sh
npm ci --ignore-scripts
npm run assets
```

Run API integration tests:

```sh
npm test
```

Run browser workflow checks with Chromium installed:

```sh
npm run test:browser
```

Set `CHROMIUM_PATH` when Chromium is installed elsewhere. Browser tests create an isolated temporary database, create disposable test accounts, and remove them afterwards. Screenshots are written to ignored `test-results/`. API tests cover trial expiry, account sessions, passwords, favorites, authorization, suspension, request origins, payment configuration, signed webhook replay handling, rate limits, and video range requests.

## Deployment and store releases

This is a functional web MVP, not a published native/store application. It has been tested in desktop Chromium and a mobile viewport; no physical TV or mobile hardware has been certified. An HTTPS reverse proxy, production hosting, database backups, monitoring, email verification/recovery, device/session management, billing self-service, and production security review remain before a public launch. The standard-library development server is not intended to be the public production edge.

Android/iOS wrappers and their store billing integrations, Samsung Tizen packaging, LG webOS packaging, and Android/Google TV packaging for supported Sony devices are separate release work. Store-specific subscriptions and review requirements must be implemented for each distribution target. A web manifest alone does not supply those packages. Account entitlement logic should remain server-side across all clients. DRM, signed provider playback URLs, program guides, M3U imports, catch-up TV, real video-on-demand catalogs, and concurrent-device limits are not included yet. Connect only content you are authorized to distribute.

## Assets

Alpine artwork is generated for this project. The demo clip is generated from that artwork. HLS.js and the local font packages retain their license texts under `static/vendor/`.

## Azerbaijan and languages

The welcome page, user interface, TV controls, subscription screens, administrator dashboard, validation messages, and activity labels support **Azərbaycanca, English, Русский, and 简体中文 (Simplified Chinese)**. Use the language selector before signing in or in the header/settings. The selection is saved on the current device. On first use, a supported browser language is selected; other browser languages fall back to Azerbaijani. Channel brand names, user-entered information, and stream URLs are preserved. Search understands translated channel categories. Dates, greetings, and activity timestamps use **Asia/Baku**, with formatting appropriate to the selected language. The six-day trial is still measured as six elapsed 24-hour periods by the server.

English message keys and complete Azerbaijani, Russian, and Chinese translations are maintained in `static/i18n.js`. New interface messages should be added there. Checkout receives the selected supported Stripe language; Azerbaijani uses English because Stripe Checkout does not support Azerbaijani. Built-in browser video controls and native form-validation messages follow the device/browser language.

The displayed subscription prices remain explicitly illustrative **USD** amounts, formatted for the selected language. They are not converted to AZN or represented as Azerbaijani prices. Localizing the interface does not establish payment-provider availability in Azerbaijan: select a provider that supports your merchant's country, banking arrangements, and required currencies before launch. The existing Stripe integration requires an eligible merchant account; a local acquiring provider may require a separate integration. Streaming availability, store distribution, and payment acceptance from Azerbaijan have not been tested on local networks or devices.
