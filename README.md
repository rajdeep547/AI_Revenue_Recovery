# Payment recovery, measured honestly

When a card or UPI payment fails, some customers retry and pay on their own and
some never come back. A recovery system sends a nudge — an email, an SMS, a
WhatsApp message, a live call — to move the second group. This repo is that
system **plus the apparatus to prove whether the nudges actually did anything**,
and a read-only dashboard that shows every decision and its reasoning.

## The premise

**Raw recovery rate is the wrong metric.** If you contact every failed payment
and then measure "what fraction recovered", you are mostly measuring how many
customers would have paid anyway. That number goes up when the economy is good,
when your customers are wealthier, when the failures are more transient — none
of which your outreach caused. A team optimising it will congratulate itself for
weather.

The number that matters is **incremental** recovery: how many extra payments
came back *because* you contacted them, over and above the ones that would have
recovered untouched. You cannot estimate that from the customers you contacted —
you need a group you deliberately *did not* contact. So this system holds back a
random **30% control arm** (assigned by a hash of the customer id, so one
customer is always in the same arm) and never messages them. Their recovery rate
is the baseline; everything above it is what the system can take credit for.

That single discipline drives the whole design: every failed payment is scored
by **expected value** — the probability that *a nudge* recovers it, times the
ticket, minus what the nudge costs — and the system acts only when that clears a
floor. Decisions it declines to act on are not swept away; they are shown, with
their reasons, on [`/not-chased`](#the-screens). Whether the extra recoveries
are worth more than they cost to chase is a separate, harder question, and
[`EVALUATION.md`](EVALUATION.md) is blunt that this project answers the first
question and not the second.

## What this is

Two things that share a database and nothing else:

1. **A decision pipeline** (`app/`). A Razorpay webhook comes in; the payment is
   normalized, its failure cause diagnosed, the customer assigned to an arm, and
   a decision recorded — act with a specific nudge, skip, or route to a human —
   through a pure decision function and seven safety guardrails. Every decision
   is written to an append-only table alongside an audit row, in one
   transaction.

2. **A read-only dashboard** (`app/dashboard/`). Five screens over that database,
   opened `mode=ro` so they physically cannot write. They render the aggregate
   measurement, every individual decision, and the reasoning behind any one of
   them.

The corpus outcomes used for measurement are **simulated** by `eval/`, which no
code under `app/` may import. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for why
that wall exists and what it buys.

## What it decides, per failed payment

| Step | Module | Result |
|---|---|---|
| Diagnose the cause | `app/diagnosis.py` (rules) | `error_code` → a root cause, at a flat 0.32 confidence |
| — ambiguous cases only | `app/llm_diagnosis.py` | an LLM guess, **recorded but barred from spending** (`TAIL_ACT_ENABLED=False`) → human queue |
| Assign the arm | `app/arms.py` | `treatment` or `control`, hashed on `customer_id` |
| Score the ladder | `app/decision/engine.py` | for each of 5 rungs: `P(recovery \| nudge) · ticket − cost`; best wins |
| Apply hard gates & the floor | same | `RISK_BLOCKED` / `ALREADY_RECOVERED` / `NO_CONTACT_CHANNEL` / `COOLDOWN` / `PRIOR_ZERO`, then `EV < floor` → skip; `control` → skip by design |
| Run guardrails (on ACT) | `app/guardrails.py` | 7 predicates per rung (kill switch, opt-out, attempt cap, contact limit, quiet hours, spend cap, dry-run); all block → `BLOCKED/<blocker>` |
| Record | `app/decision/store.py` | one `decisions` row + one `audit` row, append-only |

The five rungs, cheapest first: `retry_silent`, `email`, `sms`, `whatsapp`,
`agent_call` (`config/decision_policy.json`). The last three need a phone
number.

## The screens

Run the dashboard (see below) and open these. Every claim the project makes is
backed by one of them or by [`results/final_run.json`](results/final_run.json).

| Route | Shows | Backs |
|---|---|---|
| [`/metrics`](http://localhost:8000/metrics) | LIVE panel (real webhook deliveries) and CORPUS panel (the frozen simulated run), **side by side, never summed** | the headline uplift and its confidence interval |
| [`/decisions`](http://localhost:8000/decisions) | every recorded decision, grouped by outcome (`ACT` / `ROUTE_TO_HUMAN` / `SKIP` / `BLOCKED`), each linking to its trace | "there is a decision on record for every failure" |
| `/trace/{payment_id}` | one decision, top to bottom: the verdict as an English sentence, what failed, the EV arithmetic with this payment's numbers, all five ladder rungs, the arm, the 7 guardrail evaluations, and the append-only provenance | "you can explain any decision without asking us" |
| [`/not-chased`](http://localhost:8000/not-chased) | every payment **not** contacted, grouped by why — control hold-out, EV below floor, hard gates, guardrail veto — with count and total ticket value withheld; empty paths shown as explicit one-liners | the premise: why declining to contact 30% of customers is the point |
| [`/queue`](http://localhost:8000/queue) | `ROUTE_TO_HUMAN` decisions, read-only, no action controls; on current data an empty state that names the two paths that route here and why neither fired | "the LLM path is shipped barred from spending money" |

When the dashboard is reading anything other than the live webhook database,
every screen carries a non-dismissible **"Corpus run — outcomes are simulated —
not live traffic"** banner.

The older machine-facing route `GET /events/{payment_id}/trace` returns the
Slice 2 payment state + audit list as JSON and is unchanged; `/trace/{id}` is
its human-facing successor and reads the decision, arm and guardrail data that
route never exposed.

## Run it

```bash
python -m venv .venv
.venv/Scripts/activate                 # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env                    # set RAZORPAY_WEBHOOK_SECRET

# the live webhook receiver + dashboard (reads webhook_events.db)
uvicorn app.main:app --reload --port 8000

# build the demo database: the 2,000-event corpus through the REAL pipeline
python scripts/make_demo_db.py          # writes data/demo.db (git-ignored)

# point the dashboard at it (webhook writer unaffected; banner turns on)
DASHBOARD_DB_PATH=data/demo.db uvicorn app.main:app --port 8000

# tests, and the docs-vs-source check
python -m pytest -q
python scripts/render_readme_numbers.py --check
```

`make_demo_db.py` is deterministic — same seed, byte-identical `decisions` table
every run — and reuses `run_corpus.py`'s pipeline path, not a parallel one.

## The numbers

Read [`EVALUATION.md`](EVALUATION.md) first — it states plainly what these do
and do not establish. In one line: the intervention **works** (uplift CI
excludes zero) but is **not demonstrated to pay** (net-EV CI crosses zero).

The block below is rendered from
[`results/final_run.json`](results/final_run.json) and
[`results/split_comparison_500.json`](results/split_comparison_500.json) by
`scripts/render_readme_numbers.py`; CI fails the build if it drifts by a byte.

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

Provenance (the commit that carries this artifact is the rest of it; hashes are over newline-normalized content, not raw bytes): events.json `5999f0ea`, ground_truth.json `8dba9b00`, decision_policy.json `570ba0d2`, seed 20260826, corpus 2025-01-01T00:00:54+00:00 … 2025-01-20T23:45:50+00:00.
<!-- END GENERATED NUMBERS -->

## The pipeline, stage by stage

### Webhook — `POST /webhooks/razorpay`

1. Read raw request bytes.
2. Verify `X-Razorpay-Signature` = `HMAC_SHA256(secret, raw_body)`. Mismatch →
   `401`, nothing stored.
3. Insert `(event_id, headers, raw_body)` — `event_id` is `UNIQUE`, so a
   redelivery is ignored at the DB level (still `200`, no duplicate row).
4. **Only then** parse JSON. Malformed → `400`, raw row already stored, process
   stays up.

A payment that settles as failed is handed to the decision pipeline; a failure
there never makes the webhook non-2xx (Razorpay retries on non-2xx, and a
decision bug must not cause a redelivery storm).

### State & audit

The domain entity is the **payment**, not the delivery. `events` is keyed by
`payment_id` and holds `amount` / `method` / `status` / first & last
`event_id`. `audit` is an append-only trail keyed by `payment_id`, recording the
`event_id` that caused each row. Actions: `ingested`, `outcome_observed`
(`payment.captured` for a `failed` payment → `recovered`), `duplicate_delivery`,
`parse_failed`, `unhandled_event_type`, `decision`.

`audit`, `decisions` and `guardrail_evaluations` carry `BEFORE UPDATE` /
`BEFORE DELETE` triggers that `RAISE(ABORT)` — a recorded row cannot be
rewritten by app code, a bug, or a raw SQL console.

### Ingest — `app/ingest.py`

Three upstream shapes (`card_failure`, `abandoned_cart`, `mandate_failure`) →
one normalized row (`customer_id`, `email`, `phone`, `amount_paise`, `currency`,
`method`, `reason`, `occurred_at`, `reference`, plus the raw blob). A row with
**neither** email nor phone is rejected (`no_contact_channel`). Dedupe is
`event_id = f"{source}:{reference}"` on a `UNIQUE` column, so a redelivery with a
fresh envelope id still collapses. `Ingestor.ingest()` never raises on bad input
— it quarantines the payload in `rejected_events` with a `reason_code` and keeps
going.

### Diagnosis, arms, decision, guardrails

Covered in the table above and, in depth, in
[`ARCHITECTURE.md`](ARCHITECTURE.md). The decision function
(`app/decision/engine.py`) is pure: same inputs + same policy → identical
`Decision` and identical `inputs_hash`.

### Corpus & outcome resolution

`datagen.py` writes `data/events.json` (what the pipeline sees — no captures, no
arms, no probabilities) and `data/ground_truth.json` (the counterfactual, per
customer — **no module under `app/` may read it**). `eval/environment.py`
resolves whether a customer recovers, from one hash of
`"{seed}:{customer_id}"`, so a result depends only on `(seed, id, action)` and
never on call order. `grep -rn "ground_truth\|from eval\|import eval" app/` is
empty and stays empty.

## Repo layout

```
app/                FastAPI app + the decision pipeline (imports no eval, no ground_truth)
  decision/         the pure decision engine + append-only store
  dashboard/        the five read-only screens
config/             decision_policy.json, guardrails.json
datagen.py          deterministic corpus generator          — outside app/
eval/               outcome resolution + measurement        — outside app/
results/            final_run.json, split_comparison_500.json  (frozen, committed)
scripts/            run_corpus.py, make_demo_db.py, make_trace_fixture.py, render_readme_numbers.py
tests/              the suite
```

## Docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — data flow, and why each trust boundary
  is where it is.
- [`EVALUATION.md`](EVALUATION.md) — what the numbers establish, what they do
  not, and every known limitation.
- [`DECISIONS.md`](DECISIONS.md) — the full slice-by-slice rationale.

## Triggering a real test-mode webhook

1. Expose the local server: `ngrok http 8000`.
2. Razorpay Dashboard → **Settings → Webhooks** (in **Test Mode**) → **Add New
   Webhook**. URL: `https://<sub>.ngrok-free.app/webhooks/razorpay`.
3. Set a **Secret**; copy it into `RAZORPAY_WEBHOOK_SECRET` and restart the app.
4. Select `payment.captured` (and/or `payment.failed`), save, and use **Send
   Test Webhook** or a real test-mode payment.
5. Confirm the delivery shows `200`, the row is in `webhook_events`, and the
   process is still up (`curl localhost:8000/healthz`).

The signature check, storage-before-parse, the attack cases and process survival
are all covered by the test suite.
