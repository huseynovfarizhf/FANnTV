# Payment integration

FANnTV owns package prices, order quotes, household entitlements, and activation. A payment provider owns the hosted card-entry page and processes the payment. **No card number, expiry date, or CVV is collected or stored by FANnTV.** The provider's card page must use HTTPS, which encrypts card details in transit. Card masking and storage/processing security are the provider's responsibility. Do not add card details to adapter requests, webhooks, receipt references, or application logs.

The actual acquiring bank has not yet been identified, so its bank-specific API integration, merchant onboarding, and live charge validation remain outstanding. The contract below is FANnTV's own adapter interface, not documentation of an existing bank API. An adapter must translate this contract into the selected bank's documented flow and verify the bank's signed callbacks or authenticated payment-status API before notifying FANnTV.

## Runtime configuration

Set through secure deployment configuration; `.env.example` is documentation only, not automatically loaded.

| Name | Purpose |
| --- | --- |
| `PUBLIC_URL` | External HTTPS origin for checkout return and callback URLs |
| `FANNTV_BANK_ADAPTER_URL` | HTTPS endpoint of your trusted bank-specific checkout adapter |
| `FANNTV_BANK_ADAPTER_KEY` | Bearer credential authenticating FANnTV to that adapter |
| `FANNTV_PAYMENT_WEBHOOK_SECRET` | Shared signing secret for adapter callbacks; must be available as a real runtime secret for local HMAC verification |

Preserve TLS certificate verification. Allow outbound access to the configured adapter host in your deployment. Permit incoming HTTPS requests from the adapter to `/api/payments/webhook`. Secrets must not be placed in Git, chat, browser JavaScript, or callback query strings. Proxy-placeholder secret bindings are not sufficient for the local webhook-signing secret: the server must have the actual secret value in secure runtime configuration.

## Checkout adapter request

An account owner calls `POST /api/checkout` with `{"plan":"six_month","language":"az"}`. The server looks up that household's effective price, creates an immutable AZN order, and sends the adapter an authenticated JSON POST:

```json
{
  "order_id": "server-generated-id",
  "amount_minor": 2499,
  "currency": "AZN",
  "description": "FANnTV 6 month package",
  "language": "az",
  "return_url": "https://your-app.example/?payment=success&order=server-generated-id",
  "cancel_url": "https://your-app.example/?payment=cancelled",
  "webhook_url": "https://your-app.example/api/payments/webhook"
}
```

Headers: `Authorization: Bearer <adapter key>`, `Content-Type: application/json`, `Idempotency-Key: <order_id>`. Adapter response:

```json
{"checkout_url":"https://your-provider.example/secure-session","session_id":"provider-session-id"}
```

The URL must be HTTPS. FANnTV redirects the customer there to enter card details. **This response and the return URL do not activate service.** A failed checkout attempt grants no access. The adapter must preserve the exact order ID, amount and currency, and must ensure a bank transaction is attributed to exactly one order. It must not trust a browser-submitted success flag as proof of payment.

## Verified paid notification

After the provider confirms successful payment, the trusted adapter sends:

```http
POST /api/payments/webhook
Content-Type: application/json
X-FANNTV-Timestamp: <current Unix seconds>
X-FANNTV-Signature: <hex HMAC-SHA256>
```

```json
{
  "id": "unique-bank-event-id",
  "order_id": "server-generated-id",
  "transaction_id": "unique-bank-transaction-id",
  "status": "paid",
  "amount_minor": 2499,
  "currency": "AZN"
}
```

Compute the signature over **the timestamp string, a literal dot, and the exact raw request body bytes**, using `FANNTV_PAYMENT_WEBHOOK_SECRET`. The timestamp must be within five minutes. Retries can use a fresh timestamp/signature with the same event and transaction identifiers. Only a verified `paid` event with an integer amount and currency matching the frozen order can activate service. Transaction references are unique across orders; successful repeats do not extend service again. Wrong amounts, replay against another order, and invalid signatures are rejected.

The server atomically records the receipt, marks the order paid, extends service, and grants six-month family benefits when applicable. The customer can inspect their own `GET /api/orders`; the return screen polls paid status instead of granting access locally. The platform admin sees the payment ledger.

## Manual confirmed receipts

An administrator may record an independently verified bank/cash payment from the dashboard. The confirmation includes the household, plan, exact current AZN amount, and a unique receipt reference. A changed price is rejected rather than silently changing the amount being verified. The same receipt cannot pay for multiple households or activate the same package twice. This operation does not charge a card or confirm a bank transfer automatically.

## Optional Stripe path

An optional Stripe implementation remains available for an eligible merchant account, but it does not establish Azerbaijani bank acquiring support. Configure `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and HTTPS `PUBLIC_URL`. No Stripe Price IDs are needed: Checkout uses the frozen server-side AZN amount and one-time payment mode. Configure `/api/stripe/webhook` for `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Activation additionally checks the stored Checkout session ID, paid status, amount and currency. The six-month and annual terms are prepaid; the app does not set up automatic recurring charges. Checkout uses the selected supported Stripe language; Azerbaijani falls back to English.

Configure one payment route consistently. Bank adapter configuration takes precedence. Existing old invoice-only Stripe callbacks are not sufficient for new orders; the order-verified checkout flow is required. Live provider calls, payment settlement, refunds, disputes and 3-D Secure behavior require validation with the actual provider before release. Automated tests use signed fixtures and mocked provider responses, not real cards or charges.
