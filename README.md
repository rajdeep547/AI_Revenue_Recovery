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

## Slice 11 — corpus freeze

`scripts/run_corpus.py` drives the whole existing pipeline over the full
corpus once and pins the result. It invents no behaviour: `Ingestor` →
`app.diagnosis.diagnose` → `app.arms.assign_arm` → `app.pipeline.process_failure`
(the real decision engine, with the guardrail walk-down) →
`eval.environment.Environment.resolve`. Two committed artifacts:

- `results/final_run.json` — STEP 1: all 2,000 events, locked 70:30 split, one
  decision per customer via `process_failure`, aggregates split into a
  `customer_level` block (the 1,611-customer arm/recovery/uplift denominator)
  and an `event_level` block (2,000 events). Every field name states its unit.
- `results/split_comparison_500.json` — STEP 2: the first 500 `customer_id`s in
  sorted order (624 events), swept at treatment fractions 70:30 and 90:10 via
  `decide_with_ladder`, each split cross-checked against
  `eval.measurement.run_policy`.

Determinism is the contract: `now_utc` for every event comes from that event's
own committed `created_at` (unix epoch, UTC) in `data/events.json`; events are
processed in ascending `(created_at, event_id)` order; the per-event output
block is independently sorted by `event_id`; floats are rounded to 6 dp; keys
are sorted; no wall-clock / uuid / duration sits in the artifact (those go to a
gitignored `.meta.json` sibling). Re-running is byte-identical:

```bash
python scripts/run_corpus.py final --out results/final_run.json
python scripts/run_corpus.py split --limit-customers 500 \
    --splits 70:30,90:10 --out results/split_comparison_500.json
python scripts/render_readme_numbers.py --check   # CI: fails on any drift
```

The interval named below is the Wilson score interval on the *difference*
(Newcombe 1998 hybrid), computed in `run_corpus.py`. `eval.measurement` still
prints a Wald interval and is unchanged — see `DECISIONS.md` "Slice 11".

<!-- BEGIN GENERATED NUMBERS -->
<!-- Generated by scripts/render_readme_numbers.py from results/final_run.json and results/split_comparison_500.json.
     Do not edit by hand. Regenerate: python scripts/render_readme_numbers.py --write
     CI verifies: python scripts/render_readme_numbers.py --check -->

> **SYNTHETIC CORPUS -- read before the numbers. Outcomes are resolved by eval/environment.py from the latent per-customer parameters in data/ground_truth.json (written by our own datagen.py); they are NOT observed from a payment processor. The 9.91 pp count-basis uplift is therefore a property of that generator. What this run validates is the MEASUREMENT APPARATUS -- blind arm assignment hashed on customer_id, counterfactual subtraction of the control rate, the attribution window (degenerate 'none' here), and Wilson/Newcombe + seeded-bootstrap interval estimation -- it is NOT a claim about real-world recovery performance. The one real-world datapoint in the project is Slice 8's live Razorpay test-mode webhook: a genuine end-to-end decision (insufficient_funds, Rs 10 ticket, best rung sms, terminal SKIP / EV_BELOW_FLOOR). To make the uplift a real claim you would run this same pipeline against live webhooks with an observed recovery feed and a control arm large enough to bound the value-weighted estimate: this run's net_incremental_ev_ci_95 lower bound is Rs -17,710 at n_control = 485, and pushing it above zero at the observed effect size needs the sampling error to shrink by roughly 1.3x, i.e. a control arm on the order of ~810 customers (about 2x today's 485) -- a few thousand customers total at the locked 70:30 split, order 1e3. That assumes the effect size holds and the heavy-tailed value distribution does not worsen, so treat it as a floor.**

**Full corpus — 2,000 events, 1,611 customers, locked 70:30 split. Real `pipeline.process_failure` (guardrail walk-down included).**

| # | Headline | Value |
|---|----------|-------|
| 1 | Incremental uplift, count basis (treatment − control recovery rate) | **9.91 pp** (37.74% − 27.84%), ≈ 112 extra recoveries |
| 2 | 95% CI on uplift — Wilson score on the difference (Newcombe 1998 hybrid) | **[4.91, 14.67] pp**, width 9.76 pp |
| 3 | Arm sizes (customers) | **1,126 treatment / 485 control** of 1,611 (0 guardrail-blocked) |
| 4 | Distinct actions (unique payment_id per rung; process_failure is idempotent) | **1,126 email nudges** (1,388 across events); 0 EV-floor skips, 0 routed to human, 0 guardrail-suppressed (skip reasons seen: CONTROL_ARM) |
| 5 | Incremental recovered value, value-weighted (treatment recovered − control rate × treatment headcount) | **₹60,528.90** (₹53.76 per treated customer, 95% bootstrap [₹-15.63, ₹121.20]) |
| 6 | Net incremental EV (row 5 − ₹112.60 distinct action cost) | **₹60,416.30**, 95% bootstrap [₹-17,710.12, ₹136,362.60] |

**The row-6 lower bound is ₹-17,710.12 — at or below zero.** On this corpus the value-weighted incremental EV is not distinguishable from zero at 95% (seeded stratified bootstrap, 10,000 resamples). The count-basis uplift (row 1) stays significant; the value-basis figure does not. Treat the ₹ point estimates as directional, not bankable.

Rows 5–6 are both on the treated-customer basis (1,126 customers); 369 customers have more than one event, which is why distinct actions (row 4) sit below the event count.

**Count basis vs value basis.** (1) Count basis: 111.6 incremental recoveries (425 minus 0.2784 x 1126 expected at the control rate); at the pooled mean recovered ticket of Rs 857 that implies Rs 95,605. Value basis: Rs 60,529 (63% of it), a gap of Rs 35,076. (2) The arms are balanced on ticket (+2.2%: Rs 878 treatment vs Rs 859 control; assignment is a pure sha256 on customer_id), so the gap is not arm imbalance. (3) The incremental band skews to smaller tickets (mean Rs 786 vs population Rs 872, ratio 0.90), and raw self-recovery probability has ~no linear ticket correlation (Pearson -0.01) -- so it is lift, not baseline recovery, that concentrates in small tickets. But that skew accounts for only Rs 7,893 of the Rs 35,076 gap (implied mean ticket per incremental recovery is Rs 542, below the band mean of Rs 786). (4) That leaves Rs 27,182 unexplained by ticket skew. The stratified bootstrap 95% interval on incremental recovered value is [Rs -17,598, Rs 136,475] and contains the count-implied Rs 95,605, so the count and value estimates are NOT statistically distinguishable at this sample size (n=485 control): the apparent gap is consistent with sampling noise in a heavy-tailed, zero-inflated value estimator. This run is underpowered on value at n=485 control. (5) Headline: net_incremental_ev_inr = Rs 60,416, 95% bootstrap [Rs -17,710, Rs 136,363] -- the lower bound is at or below zero, so on this corpus the value-weighted incremental EV is NOT distinguishable from zero at 95%. It is the value-weighted, counterfactual-subtracted figure, robust to the raw-recovery fallacy; the count-basis uplift (9.91 pp, Wilson [4.91, 14.67] pp) is reported beside it, not instead of it.

**Policy selectivity.** On this corpus the policy is behaviourally identical to the recover_everything baseline: across 2000 events there were zero EV_BELOW_FLOOR skips, zero ROUTE_TO_HUMAN, zero guardrail blocks, and exactly one rung fired (email). Reason: the email rung costs Rs 0.10 and EV = p_incremental_effective * ticket - cost clears the Rs 2.00 floor for every event in the observed ticket distribution. The rules classifier emits only 2 cause(s) here -- bank_downtime Rs 47.25 (n=488); insufficient_funds Rs 39.44 (n=1123) -- where the Rs-value is that cause's email-rung break-even ticket (EV = floor). The smallest ticket in the corpus is Rs 49.00, above every break-even; the tightest margin is Rs 1.75 for bank_downtime. Phone coverage is 0%, so the sms / whatsapp / agent_call rungs (requires_channel=phone) are unreachable and the five-rung ladder collapses to email-only -- the higher-cost rungs where the EV floor and the human-review route actually bind cannot be exercised by this corpus. Slice 8's live webhook DID produce SKIP / EV_BELOW_FLOOR on a Rs 10 ticket (pay_TW67GAczusj3yl, EV Rs 0.46 vs the Rs 2.00 floor), which is the standing evidence the gate works.

**Sensitivity — first 500 customers (624 events), treatment-fraction sweep. `decide_with_ladder` direct, cross-checked against `eval.measurement.run_policy`.**

| Split | Treatment / control customers | Uplift | 95% CI width (Wilson-on-difference) |
|-------|-------------------------------|--------|------------------------------------|
| 70:30 (locked) | 344 / 156 | 13.83 pp | 17.60 pp |
| 90:10 | 457 / 43 | 14.54 pp | 27.50 pp |

Reading: The locked decision is 70:30. This artifact does NOT change it. Moving to 90:10 buys more treated customers (344 -> 457) but shrinks the control arm (156 -> 43), so the control self-recovery estimate gets noisier and the 95% interval on incremental uplift widens from 17.60 pp to 27.50 pp. 90/10 is a measurement-cost trade, not a lift improvement; 70:30 is kept.

Provenance: HEAD `70e54057`, events.json `5999f0ea`, ground_truth.json `8dba9b00`, decision_policy.json `570ba0d2`, seed 20260826, corpus 2025-01-01T00:00:54+00:00 … 2025-01-20T23:45:50+00:00.
<!-- END GENERATED NUMBERS -->
