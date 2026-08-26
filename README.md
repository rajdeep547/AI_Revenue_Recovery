# Razorpay Webhook Receiver

Single endpoint that receives Razorpay webhooks, verifies the HMAC
signature, and persists the raw request before any parsing happens.

## Endpoint

`POST /webhooks/razorpay`

Order of operations:

1. Read raw request bytes.
2. Verify `X-Razorpay-Signature` = `HMAC_SHA256(secret, raw_body)`. Mismatch → `401`, nothing stored.
3. Insert `(event_id, headers, raw_body)` into SQLite. `event_id` is `UNIQUE`,
   so a redelivery of the same `X-Razorpay-Event-Id` is ignored at the DB
   level — still `200`, no duplicate row.
4. Only then parse the body as JSON. Malformed JSON → `400`, row already
   stored, process keeps running.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # fill in RAZORPAY_WEBHOOK_SECRET
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

## Automated tests (the three break cases)

```bash
pytest tests/ -v
```

Covers:
- bad signature → `401`, row count unchanged
- duplicate `x-razorpay-event-id` → `200` twice, exactly one row
- `{"broken":` → `400`, `/healthz` still responds afterward (process alive)

## Triggering a real test-mode webhook from the Razorpay dashboard

1. Expose the local server publicly, e.g. `ngrok http 8000`.
2. In the Razorpay Dashboard → **Settings → Webhooks** (make sure you're in
   **Test Mode**, toggle top-right), click **Add New Webhook**.
3. Webhook URL: `https://<ngrok-subdomain>.ngrok-free.app/webhooks/razorpay`
4. Set a **Secret** — copy the same value into `RAZORPAY_WEBHOOK_SECRET` in
   your `.env` (and restart the app so it picks it up).
5. Select at least one event (e.g. `payment.captured`) and save.
6. Use the dashboard's **Test Webhook** / **Send Test Webhook** button, or
   trigger a real test-mode payment, to fire a delivery.
7. Confirm:
   - Razorpay's dashboard shows the delivery succeeded (`200`).
   - `sqlite3 webhook_events.db "select event_id, received_at, parsed_ok from webhook_events;"`
     shows the row.
   - The app process is still running (check your terminal / `curl localhost:8000/healthz`).

I can't click through the Razorpay dashboard myself — that step needs your
account. Everything else (signature check, storage-before-parse, the three
attack cases, and process survival) is verified by the test suite above.
