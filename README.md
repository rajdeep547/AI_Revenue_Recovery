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

## Slice 3 — synthetic data generator

`datagen.py` is a standalone, deterministic generator. It is **not** part of the
pipeline (`app/`): nothing under `app/` imports it.

```bash
python datagen.py --seed 20260826 --events 2000 --out-dir data
python datagen.py --hist        # print a ticket-size histogram, write nothing
```

It writes two files to `--out-dir` (default `data/`, gitignored — it's a
reproducible artifact):

- `events.json` — Razorpay-shaped webhook deliveries filling exactly 2,000
  events: `payment.failed` plus a little `payment.authorized` noise that never
  captures, a small cohort per customer. Every failure carries an `error_reason`
  (a permitted Slice 7 classifier feature). This is all the pipeline sees —
  **no captures, no arm, no probabilities**; whether a payment is recovered is
  not decided here.
- `ground_truth.json` — the counterfactual, per customer: `error_reason`,
  `p_would_pay_anyway` (recovers with no nudge), `p_pay_if_nudged` (recovers
  with a nudge; always `>=` the former), the derived `lift`, plus `amount` /
  `method`. **No module under `app/` may import or read this file** — tests /
  evaluation only.

Base and lift are negatively correlated, keyed off `error_reason`: transient
infra failures (`bank_downtime`, `gateway_timeout`) self-heal → high base, ~zero
lift; card problems (`expired_card`, `invalid_card`) → low base, high lift. This
is what lets Slice 5 tell "uplift measurement works" from "it's broken". Beta
parameters, mix weights, calibration numbers, power reasoning and the output
hashes are in [`DECISIONS.md`](DECISIONS.md). A `generate` run prints the three
calibration figures (mean base ≈ 0.28, mean lift ≈ 0.10, corr ≈ −0.43).

Ticket sizes are log-normal (`mu = ln 55000`, `sigma = 0.95`; median ≈ ₹550,
mean ≈ ₹870) with retail-style rounding. `--hist` prints the distribution.

Determinism: one `random.Random(seed)` drawn in a fixed order, timestamps
derived from a constant epoch (never the wall clock), JSON written sorted /
ASCII / `\n`-newline. Same seed in → byte-identical files out:

```bash
python datagen.py --out-dir run1
python datagen.py --out-dir run2
diff -r run1 run2            # no output
grep -rn ground_truth app/   # no output
```

### Outcome resolution — `eval/environment.py`

Outcomes are **not** baked into `events.json` — otherwise no policy could change
them and Slice 5 would measure zero uplift for everything. Instead the eval
harness resolves them:

```python
from eval.environment import Environment
env = Environment("data/ground_truth.json")
env.resolve("cust_00042", "nudge")   # -> True / False  (vs p_pay_if_nudged)
env.resolve("cust_00042", "none")    # -> True / False  (vs p_would_pay_anyway)
```

The coin is one hash of `"{run_seed}:{customer_id}"` per customer — not a shared
RNG stream — so a customer's result depends only on `(seed, id, action)`, never
on call order. The same draw backs both actions, so nudge-recoveries are a
superset of none-recoveries and `P(resolve|nudge) − P(resolve|none)` equals the
mean lift. `eval/` is not pipeline; nothing under `app/` may import it.

`tests/test_datagen.py` and `tests/test_environment.py` cover all of this:
exactly 2,000 events, byte-identical reruns, a different seed changes the output,
`p_pay_if_nudged >= p_would_pay_anyway` for every customer, mean lift inside
`[0.08, 0.12]`, base/lift negatively correlated, `events.json` has no capture
events and leaks no arm / probability / lift, `ground_truth.json` has no arm or
realized field, `log(amount)` passes a KS test against a fitted normal, `resolve`
is order-independent and stable across process restarts, its nudge−none rate
matches the mean lift, and `grep -rn 'ground_truth\|eval.' app/` is empty.

## Ingest

`app/ingest.py` turns three upstream event shapes into one normalized row:

| source | native shape | `reference` |
|---|---|---|
| `card_failure` | Razorpay-style `payment.failed` webhook | `payment.entity.id` |
| `abandoned_cart` | storefront checkout, total as a major-unit string | `checkout_id` |
| `mandate_failure` | recurring e-mandate / UPI-Autopay charge that bounced | `invoice_id` |

```python
from app.ingest import Ingestor, normalize

normalize("card_failure", payload)          # -> dict, or raises AdapterError
with Ingestor("ingest.db") as ing:
    res = ing.ingest("abandoned_cart", payload)   # -> IngestResult(outcome, row, reason_code)
    ing.stats()   # {"inserted": N, "duplicate": M, "rejected": K, "rejected_by_reason": {...}}
```

The normalized row has the keys in `FIELDS` — `event_id`, `source`,
`customer_id`, `email`, `phone`, `amount_paise` (integer paise, whatever unit
the source used), `currency` (defaults `INR`), `method`, `reason`, `occurred_at`
(ISO-8601 UTC), `reference` — **plus `raw`**, the untouched payload blob that
deliberately sits outside the `FIELDS` column contract. `email`, `phone`,
`method`, `reason` are individually nullable — but a row with **neither** email
nor phone is rejected (`no_contact_channel`): it's unreachable by any nudge.
`phone` is normalized to E.164-ish (`+91XXXXXXXXXX` for a bare 10-digit number),
`email` is lowercased and stripped; both idempotently. Every other field is
required.

Dedupe is `event_id = f"{source}:{reference}"` on a `UNIQUE` column, where
`reference` is the source's own business id (payment / checkout / invoice), not
a delivery id — so a redelivery with a fresh envelope id still collapses,
writing nothing and returning the stored row. The `source` prefix means the same
`reference` string from two different sources (`abandoned_cart:X1` vs
`mandate_failure:X1`) does not collide. Being a DB constraint, dedupe holds
across restarts for a file-backed ingestor.

**Two entry points, different contracts.** `normalize()` is a pure function
(no clock, no network) and *raises* `AdapterError` on bad input — good for unit
use. `Ingestor.ingest()` *never raises* on input: it catches the error, writes
the offending payload to a `rejected_events` table with a short `reason_code`
(`missing_required_field` / `unknown_source` / `bad_amount` / `bad_timestamp` /
`no_contact_channel`), and returns `outcome=REJECTED`. A batch loop keeps going;
`stats()` counts what happened.

`tests/test_ingest.py` and `tests/test_ingest_slice4.py` cover: three shapes in
→ one shape out (with email-only / phone-only / both / neither per source),
dedupe holds and survives reopen, same `reference` from two sources does not
collide, a missing optional field still yields the one shape, a missing required
field / bad amount / bad timestamp / unknown source / no-contact row is
quarantined (not raised) and the loop continues, a batch with bad rows reports
`inserted` / `rejected` / `rejected_by_reason` instead of dying, phone/email
normalization is idempotent, and one `customer_id` fed via `card_failure` and
via `abandoned_cart` lands on the same Slice 3 assignment.

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

Covers (Slices 1–2):
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
