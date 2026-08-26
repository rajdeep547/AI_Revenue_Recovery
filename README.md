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

## Event trace

`GET /events/{payment_id}/trace`

The domain entity is the **payment**, not the webhook delivery. `events`
is keyed by `payment_id` (`payload.payload.payment.entity.id`) and holds
`amount`, `method`, `status`, and the first/last `X-Razorpay-Event-Id`
seen for that payment. `audit` is an append-only trail of what happened,
keyed by `payment_id` but recording the `event_id` of the delivery that
caused each row.

Action vocabulary (exactly these — more get added in later slices):
- `ingested` — first time this payment has been seen (`payment.failed` on
  a new payment → `status=failed`; `payment.captured` with no prior row →
  `status=captured`, since it never failed, it isn't a recovery).
- `outcome_observed` — `payment.captured` arrives for a payment currently
  `failed` → `status=recovered`.
- `duplicate_delivery` — a redelivery that doesn't change state: a raw
  webhook redelivery (same `event_id`, caught before parsing), or a second
  `payment.captured` for a payment already `recovered`.
- `parse_failed` — body didn't parse as JSON.
- `unhandled_event_type` — no `payment_id` in the payload, or an event
  type this slice has no transition for (e.g. `payment.authorized`).
  Logged against the delivery's `event_id` only; no `events` row is
  touched.

Guarantees:
- Unknown `payment_id` → `404`, not a `500`.
- Audit rows are ordered by `created_at`, then by the row's autoincrement
  `id` as a tiebreaker — so two entries written in the same instant still
  come back in the order they were inserted, every time.
- `audit` cannot be rewritten by application code, a bug, or a raw SQL
  console: `BEFORE UPDATE` / `BEFORE DELETE` triggers `RAISE(ABORT, ...)`
  on the table itself, so any `UPDATE audit SET ...` or `DELETE FROM
  audit` fails at the SQLite level regardless of who issues it.

`events`/`audit` schema changed in this refactor (from event-keyed to
payment-keyed); since the data is disposable local test data, old
`*.db` files were deleted rather than migrated.

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
- unknown `payment_id` on `/events/{id}/trace` → `404`
- `payment.failed` then `payment.captured` (two different `event_id`s) on
  one `payment_id` → one `events` row at `status=recovered`, two audit
  rows (`ingested`, `outcome_observed`) in order
- a redelivered `payment.captured` after that adds a `duplicate_delivery`
  row but leaves exactly one `outcome_observed`
- `payment.captured` with no prior row → `status=captured`, `ingested`
  (not a recovery)
- an event with no `payment_id`, or a type outside `payment.failed`/
  `payment.captured` → `200`, `unhandled_event_type`, no `events` row
- two audit rows with an identical timestamp still come back in a stable,
  insertion-consistent order
- `UPDATE`/`DELETE` against `audit`, issued directly over raw SQLite,
  fails with `sqlite3.IntegrityError` from the DB trigger

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
