# BrandBatch

BrandBatch is a SaaS for agencies and brand teams. You save a brand kit per client (logo or animated green-screen logo, position, size, intro, outro), upload a batch of videos, and download every video × brand × format. Output is rendered with ffmpeg, auto-reframed with a blur fill to 9:16, 1:1 or 16:9.

**Stack:** Flask · SQLAlchemy (Postgres or SQLite) · a Postgres-backed job queue (no Redis) · ffmpeg · Razorpay Subscriptions · gunicorn · Docker.

## Features

- **Accounts:** email/password sign-up with hashed passwords, CSRF protection, login rate limiting, and sessions ended on password change.
- **Brand kits:** image or video logo, 9 positions, size, margin, opacity, green-screen removal, optional intro/outro (≤30 s), live placement preview.
- **Bulk jobs:** upload many videos, select several kits and formats, see live per-file progress, cancel, download one file or a ZIP.
- **Video quality:** the source frame rate is preserved (a 60 fps clip stays 60 fps; with an intro/outro attached every segment is matched to the fastest clip), capped per plan. Two render qualities: Balanced (CRF 22) and High (CRF 18, Agency and above). Measured against the source with VMAF: Balanced 99.0, High 99.8 (100 = identical, above 95 = indistinguishable).
- **Plans:** Free, Creator, Agency, Agency Pro. Brand kits, render minutes, videos per job, resolution, frame-rate cap, render quality, queue priority and the "Made with" mark are all defined in `app/plans.py` and enforced server-side. The pricing table is generated from those same values, so the page can never promise more than the code allows. Quota is reserved when a job is queued.
- **Billing:** Razorpay Subscriptions with Checkout (UPI autopay and cards). Payment signatures are HMAC-verified, and webhooks are signature-checked and idempotent.
- **Worker:** atomic job claiming, so you can run as many workers as you like. Priority lanes per plan, crash recovery (stale renders are requeued) and automatic file deletion after 48 h.
- **Anti-abuse:** email confirmation required before the first render, disposable-email domains blocked (`app/disposable_domains.txt`), at most 3 accounts per IP per day, and a shared daily free-render budget per IP so one person can't farm the free plan across accounts. IP addresses are stored only as salted hashes.
- **Trust and safety:** Terms, Privacy and Content Policy pages, a public report/takedown form, and gambling brand names blocked on brand kits.
- **Admin CLI:** `manage.py` for set-plan, reset-password, list-users, list-reports and cleanup.

## Run locally

**Windows:** double-click `start.bat`.  **macOS / Linux:** run `./start.sh`.

The first run creates `.venv` and installs dependencies (needs Python 3.10+ and internet). It then starts the website and the render worker and opens http://127.0.0.1:8000. On Windows the worker runs in a second window, so keep both windows open. On macOS/Linux, Ctrl+C stops both.

Or start them manually:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m flask --app wsgi run --port 8000        # web app on http://127.0.0.1:8000
python worker.py                                   # in a second terminal: render worker
```

SQLite and a local `./storage` folder are used by default. ffmpeg is bundled through `imageio-ffmpeg`. Give yourself a paid plan with `python manage.py set-plan you@example.com agency`.

Run the tests with `pytest -q`. To run them against Postgres, add `TEST_DATABASE_URL=postgresql://user:pass@host/db`.

## Deploy

Video encoding needs long-running processes and a disk, so **this app can't run on Vercel**. Vercel functions have short timeouts, a ~4.5 MB request limit and no ffmpeg worker. Use one of the options below.

### Option A: Railway or Render, single container (simplest)

1. Push this folder to a GitHub repo.
2. Create a new project from the repo. The platform detects the `Dockerfile`.
3. Add a **PostgreSQL** database. The platform sets `DATABASE_URL`.
4. Attach a **persistent volume** mounted at `/data`.
5. Set these variables (see `.env.example`):
   `SECRET_KEY` (a long random string), `APP_ENV=production`, `TRUST_PROXY=1`, `EMBEDDED_WORKER=1`, `STORAGE_DIR=/data/storage`, `SUPPORT_EMAIL`, `PUBLIC_URL`.
6. Deploy, then open `/api/health`. It should return `{"ok": true}`.
7. Add your custom domain in the platform settings.

Use 2+ vCPU and 2+ GB RAM, because encoding is CPU-heavy. Keep `WEB_CONCURRENCY=2`.

### Option B: VPS with docker compose (web, separate workers, Postgres)

```bash
cp .env.example .env      # fill in SECRET_KEY etc.
docker compose up -d --build
docker compose up -d --scale worker=3     # more render capacity
```

Put Caddy or nginx in front for HTTPS and set `TRUST_PROXY=1`. Web and workers share the `storage` volume. A Hetzner or DigitalOcean box with 4 vCPU is a good start.

> Web and worker must share the same storage. On platforms where a volume can attach to only one service, use Option A. Moving storage to S3/R2 is the next step for scaling out.

### Razorpay setup

1. In the Razorpay Dashboard, enable **Subscriptions**. Create three monthly plans: Creator ₹499, Agency ₹1,999, Agency Pro ₹4,999.
2. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_PLAN_CREATOR` / `_AGENCY` / `_AGENCY_PRO` to the plan IDs.
3. Add a webhook to `https://YOUR_DOMAIN/billing/webhook`, subscribed to `subscription.activated`, `subscription.charged`, `subscription.pending`, `subscription.halted`, `subscription.cancelled` and `subscription.completed`. Put its secret in `RAZORPAY_WEBHOOK_SECRET`.
4. Test end to end with test-mode keys before going live.

If the Razorpay variables are empty, the app runs normally and the billing page says payments aren't configured. You can upgrade users with `manage.py set-plan`.

## Email and anti-abuse

Confirmation links are sent either over an HTTPS API (`MAIL_FROM` + `BREVO_API_KEY`) or over SMTP
(`MAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`). The API path is preferred: many
hosts, including Railway on its Free/Trial/Hobby plans, block outbound SMTP ports. Check either with
`python scripts/check_email.py you@example.com`.
Any transactional provider works: Brevo, Resend, Amazon SES, Zoho, Gmail with an app password. **Without SMTP
the app still runs and writes the link into the server log**, and you can confirm accounts by hand with
`python manage.py verify-user someone@example.com`.

Tunable with environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_ACCOUNTS_PER_IP_PER_DAY` | 3 | New accounts allowed from one IP in 24 h |
| `FREE_MINUTES_PER_IP_PER_DAY` | 30 | Free render minutes shared by all free accounts on one IP |
| `DISPOSABLE_EXTRA` | — | Extra blocked email domains, comma separated |
| `IP_HASH_SALT` | dev value | **Set this in production.** Salt for hashing IP addresses |

Set `TRUST_PROXY=1` behind a platform proxy, otherwise every visitor looks like one IP and the per-IP limits
apply to all of them at once.

## Plans

All limits live in `app/plans.py`. Edit the `PLANS` dict to change pricing or limits: the pricing page, the
comparison table, the dashboard, the upload form and the enforcement code all read from it.

| | Free | Creator | Agency | Agency Pro |
|---|---|---|---|---|
| Price / month | ₹0 | ₹499 | ₹1,999 | ₹4,999 |
| Brand kits | 1 | 2 | 10 | 40 |
| Render minutes / month | 10 | 120 | 600 | 2,000 |
| Videos per job | 5 | 25 | 50 | 100 |
| Resolution | 720p | 1080p | 1080p | 1080p |
| Frame rate | 30 fps | 60 fps | 60 fps | 60 fps |
| Render quality | Balanced | Balanced | + High | + High |
| "Made with" mark | Yes | No | No | No |
| Queue priority | Standard | Standard | Higher | Highest |

## Razorpay: two flows

| Flow | What it is | Code |
| --- | --- | --- |
| **Subscriptions** | Monthly plans (Creator/Agency/Agency Pro), UPI autopay or card, kept in sync by webhooks | `billing.create_subscription`, `/app/billing/subscribe/<plan>`, `/app/billing/verify`, `/billing/webhook` |
| **Standard Checkout (Orders)** | One-time render-minute top-up packs bought from the Billing page | `billing.create_order`, `POST /api/create-order`, `POST /api/verify-payment` |

Top-up packs are defined in `app/plans.py` (`TOPUP_PACKS`). Purchased minutes are added to the current
month's allowance and expire at month end; change that rule in `services.usage_seconds` if you prefer
credits that roll over. Every order is recorded in the `minute_topups` table, and minutes are credited only
after the signature check passes, exactly once per order.

Signature algorithms (both HMAC-SHA256 with the key secret):

- Orders: `order_id + "|" + payment_id`
- Subscriptions: `payment_id + "|" + subscription_id`
- Webhooks: the raw request body

Check your keys at any time:

```bash
python scripts/check_razorpay.py        # creates a test-mode order and prints the result
```

## Testing Razorpay

**1. Test mode.** In the Razorpay dashboard switch to Test Mode, enable Subscriptions, and create your
three monthly plans there. Copy the test key id/secret and the three plan ids into `.env`
(`RAZORPAY_KEY_ID=rzp_test_...`). Restart the app; the billing page stops saying payments aren't configured.

**2a. Top-up checkout (Orders).** Sign up, open Billing, scroll to "Need more minutes this month?" and click
**Buy minutes**. The Razorpay modal opens. Pay with a test card from your dashboard's test-card list, and the
page should say "Payment successful" and reload with a higher minute limit. Cancelling the modal must say
"Payment cancelled. Nothing was charged."

**2b. Subscription checkout.** Sign up, go to Billing, choose a plan. Razorpay Checkout opens. Use a test card from your
dashboard's test-card list (they differ by region, so copy them from your own account), any future expiry
and any CVV, then click **Success** on the mock bank page. You should land back on the dashboard with the
new plan active, and the plan's limits should appear on the upload page.

**3. Webhooks, without a public URL.** Razorpay can't reach `127.0.0.1`, so use the bundled signer:

```bash
python scripts/send_test_webhook.py --user-email you@example.com --event subscription.activated --plan agency
python scripts/send_test_webhook.py --user-email you@example.com --event subscription.charged   --plan agency
python scripts/send_test_webhook.py --user-email you@example.com --event subscription.halted
python scripts/send_test_webhook.py --user-email you@example.com --event subscription.cancelled
python scripts/send_test_webhook.py --user-email you@example.com --bad-signature   # must answer 400
```

Check the plan after each with `python manage.py list-users`. Expected: activated/charged → plan active,
halted → Free with a payment-issue notice, cancelled → Free, repeated event → ignored, bad signature → 400.

**4. Real webhooks.** After deploying, add the webhook in Razorpay pointing at
`https://YOUR_DOMAIN/billing/webhook` with the six subscription events, put its secret in
`RAZORPAY_WEBHOOK_SECRET`, and run one live test-mode checkout end to end. Razorpay's dashboard shows each
delivery and its response code; they should all be 200.

**5. Before going live:** switch to live keys, redo one real ₹1 test if you can, and confirm the cancel
button in the app marks the subscription as cancelling in the Razorpay dashboard.

## Admin

```bash
python manage.py list-users
python manage.py set-plan client@agency.com agency_pro
python manage.py reset-password client@agency.com
python manage.py list-reports
python manage.py cleanup          # also runs automatically inside workers every 5 minutes
```

## Before you launch

- [ ] Have a lawyer review `templates/legal/*` (Terms, Privacy/DPDP, Content Policy).
- [ ] Set `SUPPORT_EMAIL` to a monitored inbox, since password resets are handled through support for now.
- [ ] Complete Razorpay test-mode checkout and webhook testing on the deployed domain.
- [ ] Back up Postgres daily (most platforms have one-click backups).

Because the schema is created with `create_all`, changing a model after you have live data needs a migration
(Alembic). Before launch, just drop and recreate the database.

## Next up (not in this MVP)

Self-serve password reset email · team members per workspace · S3/R2 storage · Instagram / YouTube publishing via official APIs · Alembic migrations.
