# Decisions

## Slice 1 · webhook receiver (`app/main.py`, `app/db.py`, `app/security.py`)

`POST /webhooks/razorpay` is the single ingress, and the order of
operations *is* the design:

1. Read the raw request bytes.
2. Verify `X-Razorpay-Signature` as a hex HMAC-SHA256 of *those bytes*
   under `RAZORPAY_WEBHOOK_SECRET`, constant-time compared. Mismatch →
   `401`, nothing stored, `json` never touched.
3. `INSERT OR IGNORE` the raw `(event_id, headers, body)` into
   `webhook_events` *before* the body is parsed, so no delivery is lost to
   a parser bug and a redelivered `X-Razorpay-Event-Id` dedupes at the DB
   (`event_id` is `UNIQUE`) — still `200`, no second row.
4. Only now `json.loads`. Malformed JSON → `400`, the raw row already
   persisted, the process still serving (`/healthz` answers afterward).

Verify-before-store keeps attacker-supplied bytes out of the parser and
business logic until the signature checks out; store-before-parse keeps an
authentic delivery from being dropped because parsing choked. Covered by
`tests/test_webhook.py` (4 tests): bad signature → `401` with the row
count unchanged; duplicate `x-razorpay-event-id` → `200` twice, exactly
one row; `{"broken":` → `400` then `/healthz` still `200`.

### OPEN ITEM — the Pass condition has never been observed green

Slice 1's Pass condition is a **live Razorpay test-mode payment landing in
`webhook_events.db`**, and that has not happened. `uvicorn.log` is the
only record of real inbound traffic, and every delivery from Razorpay's
egress (`52.66.75.174`, AWS Mumbai) is `401 Unauthorized` — the configured
secret never matched a real signature — so `store_raw_event` was never
reached and no webhook body from a real delivery has ever been persisted.
Everything below the signature check (store → parse → `200`, the
payment-entity extraction in `_extract_payment_entity`, and all of Slice
2's state machine) is exercised **only** by synthetic payloads in the test
suite. The `payload.payload.payment.entity` shape the code parses is taken
from Razorpay's webhook **documentation**, not from an observed delivery.
This is outstanding, not done — the end-to-end path from Razorpay's
servers to a stored row is unverified.

---

## Slice 2 · payment-keyed domain model + trace (`app/main.py`, `app/db.py`)

Slice 1 stored deliveries; Slice 2 makes the **payment** the domain
entity. `app/db.py` gains two tables: `events`, keyed by `payment_id`
(`payload.payload.payment.entity.id`) holding `amount`, `method`,
`status`, and the first/last `event_id` seen for that payment; and
`audit`, an append-only trail keyed by `payment_id` that records the
`event_id` of the delivery behind each row. Append-only is enforced *in
SQLite*, not in app code: `BEFORE UPDATE` and `BEFORE DELETE` triggers
`RAISE(ABORT, …)` on `audit`, so no code path, bug, or raw SQL console can
rewrite history.

The handler runs a small state machine over `{payment.failed,
payment.captured}` — any other event type, or a payload with no
`payment_id`, is `unhandled_event_type` logged against the delivery only,
with no `events` row touched:

- `ingested` — first sighting of a payment (`failed` on a new payment, or
  a `captured` with no prior row; the latter is `status=captured`, not a
  recovery, because nothing here ever saw it fail).
- `outcome_observed` — `captured` arriving for a payment currently
  `failed` → `status=recovered`. This is the recovery signal the domain
  model exists to capture (later slices measure recovery from the datagen
  corpus and `eval/environment.py`, not from this table).
- `duplicate_delivery` — a redelivery that changes no state (raw
  `event_id` seen before, caught before parsing; or a second `captured`
  on an already-recovered payment).

`GET /events/{payment_id}/trace` returns the `events` row plus its ordered
audit history — ordered by `created_at` then the autoincrement `id`, so
two entries written in the same instant still read back in insertion
order. An unknown `payment_id` → `404`, never a `500`. Covered by
`tests/test_trace.py` (9 tests), including the failed→recovered path
producing one `events` row and two audit rows, a redelivered `captured`
adding a duplicate but only one `outcome_observed`, timestamp-collision
ordering, and both the `UPDATE audit` and `DELETE FROM audit` triggers
firing at the DB level.

---

## Slice 3 · data generator (`datagen.py`)

### Seed and determinism

- Default seed: **`20260826`**. Everything is drawn from a single
  `random.Random(seed)` in a fixed per-customer order (error_reason → base prob
  → lift → ticket → method → arm → realisation → event plan → inter-event
  gaps). No `uuid4`, no wall clock: every `created_at` is `EPOCH`
  (`1_735_689_600`, 2025-01-01T00:00:00Z) plus deterministic offsets.
- Output JSON is `sort_keys=True`, `ensure_ascii=True`, `indent=2`, `\n`
  newlines, written with `newline="\n"`. Reruns at a fixed seed are
  byte-identical.

### Ticket sizes — lognormal

- `amount` (paise) `= lognormvariate(mu=10.9151, sigma=0.95)`, clamped to
  `[4_900, 5_000_000]` (₹49 … ₹50,000), then retail-rounded: 45% to charm
  prices (`…49` / `…99`), the rest to ₹1 / ₹10 / ₹100 steps that coarsen as the
  figure grows. `mu = ln(55_000)` puts the median near ₹550; `sigma = 0.95`
  gives the fat right tail.
- Verified with a KS distance of `log(amount)` against a normal fitted to the
  sample (Lilliefors style): **≈ 0.032** at the default seed — the claim is
  "lognormal", not merely "not uniform".

---

## Slice 3 amendment · the counterfactual half

### `error_reason` drives everything

Every `payment.failed` event carries `error_reason ∈ {bank_downtime,
gateway_timeout, expired_card, invalid_card, insufficient_funds, otp_timeout}`.
It is a permitted feature — it lands in `events.json` and is the Slice 7
classifier input. `error_code` / `error_description` are derived from it so the
webhook body keeps the shape Slices 1–2 parse.

Mix weights (plausible for an Indian D2C checkout — UPI-heavy, real 3DS /
UPI-PIN friction, frequent issuer/UPI outages):

| error_reason | weight |
|---|---|
| insufficient_funds | 0.28 |
| otp_timeout | 0.22 |
| bank_downtime | 0.18 |
| gateway_timeout | 0.14 |
| expired_card | 0.10 |
| invalid_card | 0.08 |

### Two probabilities per customer

`ground_truth.json` records, per customer: `error_reason`, `arm`,
`p_would_pay_anyway` (base), `p_pay_if_nudged`, `lift`, `realized`.

- `base = betavariate(base_α, base_β)` rounded to 6dp.
- `p_pay_if_nudged = min(1.0, base + betavariate(lift_α, lift_β))` rounded to
  6dp; `lift = p_pay_if_nudged − base`, stored as a derived field.
- Invariant asserted every run: `p_pay_if_nudged ≥ p_would_pay_anyway`, and
  `base + lift == p_pay_if_nudged` to 1e-9.

### Per-`error_reason` Beta parameters

`α = mean·κ`, `β = (1−mean)·κ`. Base κ = 16, lift κ = 14 — tight enough to keep
population calibration, loose enough that no bucket is a point mass
(base SD ≈ 0.07–0.12 per bucket).

| error_reason | base mean | base (α, β) | lift mean | lift (α, β) |
|---|---|---|---|---|
| bank_downtime | 0.52 | (8.32, 7.68) | 0.02 | (0.28, 13.72) |
| gateway_timeout | 0.48 | (7.68, 8.32) | 0.03 | (0.42, 13.58) |
| expired_card | 0.12 | (1.92, 14.08) | 0.22 | (3.08, 10.92) |
| invalid_card | 0.10 | (1.60, 14.40) | 0.24 | (3.36, 10.64) |
| insufficient_funds | 0.15 | (2.40, 13.60) | 0.09 | (1.26, 12.74) |
| otp_timeout | 0.30 | (4.80, 11.20) | 0.12 | (1.68, 12.32) |

### Why the anticorrelation is deliberate

A recovery model that can't separate *organic* recoveries from *caused* ones
will look accurate while measuring nothing. The synthetic world has to make
that failure visible, so base and lift are built to move in opposite
directions, keyed off `error_reason`:

- **bank_downtime, gateway_timeout** — a transient outage. The customer retries
  on their own and succeeds → **high base, ~zero lift**. A nudge here is the
  model claiming credit for a recovery that was going to happen anyway.
- **expired_card, invalid_card** — the card will never fix itself, but a single
  "update your card" nudge converts → **low base, high lift**. This is where
  real incremental revenue lives.
- **insufficient_funds** — low base, low-to-moderate lift (a well-timed retry
  after payday helps somewhat).
- **otp_timeout** — medium base, medium lift (a "complete your payment"
  reminder helps).

### Population calibration (default seed `20260826`)

Values below are post-follow-up (arm/realized removed from datagen, so the
per-customer draw order and the customer count changed):

| target | value |
|---|---|
| mean `p_would_pay_anyway` ≈ 0.29 (baseline not moved) | **0.280** |
| mean `lift` ∈ [0.08, 0.12] | **0.104** |
| Pearson corr(base, lift) ≤ ≈ −0.4 | **−0.432** |
| customers (to fill 2,000 failure-only events) | **1611** |

### Why the lift magnitude was chosen — power

Target mean lift ≈ 0.10 was picked for detectability by Slice 5. At a design
scale of 2,000 customers with a 30% control split → 600 control / 1,400 treated
and a baseline recovery rate ≈ 0.31, the SE of the control−treated difference in
proportions is `sqrt(0.31·0.69·(1/600 + 1/1400)) ≈ 0.0226` (**~2.2pp**). A true
10pp effect is then ≈ **4.4σ** — comfortably powered — while the near-zero lift
on the infra buckets means a naïve estimator that ignores `error_reason` will
understate or misattribute the effect. The 30% control share and the arm split
itself now live in the eval harness, not in datagen.

---

## Slice 3 follow-up · outcome resolution moved to `eval/environment.py`

### Why it moved

Datagen previously assigned an arm, flipped the recovery coin, and wrote a
`payment.captured` iff the flip won. Outcomes were baked at generation time, so:

- **No downstream policy could change them.** Slice 5 would measure zero uplift
  for *every* policy, a perfect one included — "measurement works" and
  "measurement is broken" would look identical.
- **Skipping a customer got their recovery for free.** A policy that took no
  action on a customer who was pre-flipped to "recovered" would still be
  credited with the capture.

So datagen now stops at failures (plus the existing authorized-never-captured
noise). Given a policy's action for a customer, whether the payment comes back
is resolved by `eval/environment.py` — which is *not* pipeline and *may* read
`ground_truth.json`.

### How resolution works

`Environment(ground_truth, run_seed=None).resolve(customer_id, action)` with
`action ∈ {"none", "nudge"}`:

- One uniform draw per customer, `u = sha256("{run_seed}:{customer_id}")[:8] >>
  11 / 2**53`. **Not** from a shared RNG stream — so the result depends only on
  `(run_seed, customer_id, action)`, never on call order or how many other
  customers were resolved first. `run_seed` defaults to `ground_truth.meta.seed`.
- `"none"` compares `u` to `p_would_pay_anyway`; `"nudge"` compares the *same*
  `u` to `p_pay_if_nudged`.
- Because `p_pay_if_nudged >= p_would_pay_anyway` and the draw is shared, the
  set of nudge-recoveries is a superset of the none-recoveries — a monotone
  coupling with no defiers. Policy comparisons are paired, replays are
  byte-identical, and across the population
  `P(resolve|nudge) − P(resolve|none)` equals the mean `lift`
  (observed at the default seed: **0.3824 − 0.2787 = +0.1037**, vs mean `lift`
  field 0.1042).

`eval/` is excluded from the pipeline: the datagen grep test asserts zero hits
under `app/` for both `ground_truth` and `eval.`.

### File hashes (SHA-256, default seed, `--events 2000`)

The follow-up changed the draw order again, so the amendment hashes
(`d0d39c07…` / `4999ccf5…`) are superseded by:

```
events.json        5d0f9bd5d96c82b91547a9d123c9b414efb1ad4555c701fec82321959cc89668
ground_truth.json  8dba9b00aa2c6b873c83742335361fb77a02417341d5a7a05ea6e69398182cad
```

### Slice 5 prerequisite · `notes.customer_id` added to `events.json`

Slices 1-4 and Slice 3's generator were never wired together: `events.json`
entities carried no `customer_id`, `email`, or `phone` anywhere, so every
generated event was rejected by `app/ingest.py`'s `card_failure` adapter with
`missing_required_field: customer_id`. `customer_id` lived only in
`ground_truth.json`, which `app/` may never read — so the fix could only be a
generator change (approved before writing code, per the Slice 5 brief).

Each event entity now carries a Razorpay-shaped `notes` object:
`{"customer_id": customer_id, "email": f"{customer_id}@example.test"}`. Both
values are deterministic functions of the `customer_id` already assigned
earlier in the per-customer draw order (`cust_n` is sequential, not drawn from
`rng`), so **no new `rng.*` call was added** — the fixed draw order
(error_reason → base → lift → ticket → method → event plan → gaps) and every
number in the population-calibration table above are unchanged. Only
`events.json`'s bytes (and therefore its hash) change; `ground_truth.json`'s
hash is confirmed identical:

```
events.json        5999f0ea6f48b1b3da9491f1471a84155e558f076ae8f249a81fc34b6003856a
ground_truth.json  8dba9b00aa2c6b873c83742335361fb77a02417341d5a7a05ea6e69398182cad  (unchanged)
```

---

## Ingest (`app/ingest.py`)

### One row shape, three adapters

Downstream stages (classifier, policy, eval) should never branch on where an
event came from, so ingest collapses three native shapes into `FIELDS`:
`event_id, source, customer_id, email, phone, amount_paise, currency, method,
reason, occurred_at, reference` (+ `raw`). Adapters live in the `ADAPTERS`
registry — adding a fourth source is a new entry, not a new code path elsewhere.

- **`email`, `phone`, `method`, `reason` are individually nullable.** An
  abandoned cart has no payment method and no failure reason; a card failure may
  carry only one contact channel. Every other field is required — a missing
  amount, customer id, reference or timestamp is an error.
- **Amounts normalize to integer paise.** `card_failure` / `mandate_failure`
  already send minor units (`int`); `abandoned_cart` sends a major-unit string
  (`"1299.00"`) converted with `Decimal(...) * 100`, `ROUND_HALF_UP` — no binary
  float rounding.
- **Timestamps normalize to ISO-8601 UTC.** Unix seconds and `...Z` strings
  both land as `2025-01-01T00:00:00+00:00`.
- **Phone → E.164-ish, email → lowercased/stripped, both idempotently.** Strip
  non-digits, drop a `00`/`0` international-or-trunk prefix, assume a bare
  10-digit number is `+91` (Indian mobile). Re-running the cleaner on its own
  output is a no-op — asserted by a test, because the same payload may be
  ingested more than once and must dedupe on an identical row.
- **`raw` keeps the original payload** for audit; nothing is lost.

### Why at least one of email / phone is required

The pipeline exists to *reach* a customer and nudge them. A normalized row with
neither email nor phone is a customer the decision engine can never act on — it
is noise, not an event. The choice is to reject it at ingest (`no_contact_channel`)
rather than let it flow through the classifier and the policy and only discover
at send time that there is nowhere to send. A contactless row also quietly
dilutes every measured rate (recovery, lift) with dead weight if it sits in the
population, so it is kept out of the population entirely.

### Why bad rows are quarantined instead of raising

A 2,000-event run must be able to report *"1,996 ingested, 4 rejected, with
reasons"* — not die on event 4. `Ingestor.ingest()` is called in a loop, and a
raised exception would unwind that loop and lose every good row after the bad
one. So `ingest()` never raises on input: it catches `AdapterError`, writes
`(source, reason_code, reason_detail, raw, rejected_at)` to `rejected_events`,
and returns `outcome=REJECTED`. Rejection becomes a *data outcome* — countable
via `stats()`, inspectable via `rejected()`, replayable from `raw` once the
upstream bug is fixed. `normalize()` still raises — it stays a pure function, so
unit code and tests can assert on the specific `reason_code`; only
`Ingestor.ingest()` catches. The `reason_code` set is small and closed
(`missing_required_field`, `unknown_source`, `bad_amount`, `bad_timestamp`,
`no_contact_channel`) so `stats()["rejected_by_reason"]` is a triage histogram,
not free text.

### Dedupe key is `source:reference`

`event_id = f"{source}:{reference}"`, where `reference` is the source's own
stable business id (payment id / checkout id / invoice id) — never a delivery or
envelope id, so a redelivery with a fresh envelope id still collapses onto the
first row. Prefixing with `source` means the *same* reference string arriving
from two different sources does **not** collide: `abandoned_cart:X1` and
`mandate_failure:X1` are two rows, not one. Enforced by a `UNIQUE` column with
`INSERT OR IGNORE`, so dedupe also holds across process restarts for a
file-backed `Ingestor`.

### `stats()` counts this ingestor's session

`stats()` returns `{inserted, duplicate, rejected, rejected_by_reason}` — all
derived from in-memory counters reset at construction. `rejected` is the total;
`rejected_by_reason` is the same total split by `reason_code` (a triage
histogram). `count()` still reports the true table size (which includes rows
from earlier sessions for a file-backed DB); `stats()` reports what *this*
`Ingestor` instance did. Duplicates can't be counted any other way — a dedupe
hit writes nothing.

### customer_id namespace (gate for Slice 3)

All three adapters emit `customer_id` as `str(<the source's own id>)` with **no
source prefix**, and Slice 3's `Environment.resolve` keys purely on that string
(`sha256(f"{run_seed}:{customer_id}")`). So the same customer arriving via
`card_failure` and via `abandoned_cart` lands on the same assignment — verified
by `test_customer_id_namespace_is_shared_across_adapters` and printed at
re-gate time (`cust_00001` → same `resolve` both ways).

### Not wired to HTTP yet

This slice is a library: `normalize()` for the pure transform, `Ingestor` for
transform + dedupe + quarantine + storage. No endpoint, and `app/db.py`'s
`init_db` is untouched — the ingestor owns its `normalized_events` /
`rejected_events` tables and creates them idempotently.

---

## Slice 5 · measurement (`eval/measurement.py`)

### Approved spec deviation: assignment salt differs from `Environment`'s

The brief specified hashing `sha256(f"{run_seed}:{customer_id}")` for arm
assignment. The build instead hashes `f"assign:{run_seed}:{customer_id}"` —
a one-line SPEC DEVIATION note sits at the top of `_uniform_assign` in
`eval/measurement.py` stating this explicitly, promoted from an implicit
choice to a named, approved deviation. Reusing the bare hash — the same one
`eval/environment.py`'s outcome-resolution draw uses — would tie a
customer's arm to the same uniform `u` used to decide whether they
self-recover under `"none"`: every customer landing in "control" (`u >=
0.7`) would be compared against `p_would_pay_anyway` values that are almost
always < 0.7, crushing the control-arm recovery rate toward zero and
inflating every measured uplift. The `"assign:"` prefix makes the two draws
independent while both stay fully deterministic in `(seed, customer_id)`.

### CI method: normal approximation (Wald), not bootstrap

Arm sizes run in the hundreds-to-low-thousands (treatment ≈1,100–1,400,
control ≈500–600 at the default 2,000-event dataset) and recovery rates sit
well away from 0/1 (≈0.28–0.38) — the binomial-proportion CLT already holds
comfortably at that scale, the same normal approximation DECISIONS.md's power
section uses to justify the chosen lift magnitude. A bootstrap would produce
the same interval for materially more compute, so Wald was used:
`uplift ± 1.96 * sqrt(p_t(1-p_t)/n_t + p_c(1-p_c)/n_c)`.

### Control is always `"none"`; policy only governs treatment

`run_policy` resolves every control-arm customer under action `"none"`
regardless of the policy under test, and only asks `policy(row)` for
treatment-arm customers. This is what makes "uplift" a causal-shaped
quantity rather than a comparison of two arbitrary groups: it is always
policy-under-treatment vs. the same population's do-nothing counterfactual.

### Population is normalized rows, not raw JSON

`load_population` runs `events.json` through the real `card_failure` adapter
and a real `Ingestor` (in-memory), never touching the raw event dicts
directly. Because every event for one customer in datagen shares the same
Razorpay payment id, `Ingestor`'s `source:reference` dedupe collapses a
customer's failure + retry + noise events into a single row for free — the
population handed to `run_policy` is naturally one row per customer.

### Population invariant: one row per `customer_id`, enforced not assumed

That "naturally one row per customer" claim was, until the Slice 5 hardening
pass, an unchecked accident of how datagen happens to reuse one payment
reference per customer — a **latent risk, not a live bug**: nothing in
today's dataset ever triggers it (every customer has exactly one payment
id), but nothing was stopping a future datagen change, a hand-built fixture,
or a real production feed from putting two distinct payment references on
one customer. Assignment (`assign_arm`) is customer-level; if that ever
happened, `Ingestor` would correctly *not* dedupe the two rows (different
`reference` ⇒ different `event_id`), both would land in the same arm
(assignment doesn't care how many rows a customer has), and the
recovery-rate denominator would silently become event-level while the Wald
CI is still computed as though every observation were independent —
narrowing it on correlated data, with every uplift number quietly wrong and
nothing failing loudly. `load_population` now counts rows per `customer_id`
with `Counter` after ingesting and raises `DuplicateCustomerRows` — a real
exception, not an `assert` (asserts vanish under `-O`, and this must hold in
production) — carrying both the offending `customer_id`s and their row
counts if the invariant doesn't hold. Covered by
`test_load_population_raises_on_duplicate_customer_id`, which constructs
exactly that two-distinct-payment-id customer through the real adapter +
`Ingestor` and asserts the invariant fires with the right customer_id and
count (`{"cust_dup": 2}`).

### Rejected events are excluded, not silently dropped

Before this pass, `rejected: 0` on the real dataset meant the
exclude-and-count path had never actually fired in a test.
`test_contactless_event_is_rejected_counted_and_excluded_from_experiment`
adds one synthetic event with a `customer_id` but no email/phone at all,
confirms it is (a) quarantined with `reason_code = no_contact_channel` and
counted in `stats()["rejected_by_reason"]`, (b) absent from `load_population`'s
returned rows, and (c) absent from either arm's denominator when the
resulting rows are run through `run_policy` — `treatment.n + control.n`
equals the count of *contactable* customers only.

### Slice 5 prerequisite, and why it needed sign-off

Before this slice, `events.json` carried no `customer_id`, `email`, or
`phone` anywhere — only `ground_truth.json` (which `app/` may never read)
had it — so every generated event was rejected by `card_failure` with
`missing_required_field: customer_id`. The generator change that fixed this
(`notes.customer_id` + a synthetic email, see the Slice 3 section above) was
proposed and approved before any Slice 5 code was written, per the brief's
explicit instruction to stop and ask rather than quietly patch around it.

### BREAK(b): assert against the treatment arm's true lift, not the population's

`recover_everything`'s CI is treatment-vs-control on whichever ~1,100
customers the hash happened to put in treatment for this seed. That
subset's true mean lift (from `ground_truth.json`, test-only) is itself a
sample from the population and will drift from the population-wide 0.1042
by chance — asserting the CI brackets 0.1042 asserts against the wrong
denominator. The test now computes `treatment_true_lift` by filtering
`ground_truth.json`'s `lift` field to exactly the customer_ids that
`assign_arm` puts in treatment, and asserts the CI brackets *that* value.
At the default seed the drift is small (population 0.1042 vs. treatment-arm
0.1039, drift −0.0003) but the test would still be correct if it weren't.

### BREAK(c) tolerance: ±3σ, justified from the binomial SE

Arm assignment is one independent Bernoulli(0.7) trial per customer (now
provably one trial per customer — see the population invariant above), so
the observed treatment ratio's binomial SE is `sqrt(p(1-p)/n)`. At n=1,611,
p=0.7: `sqrt(0.7·0.3/1611) ≈ 0.0114` (~1.1 percentage points). The test uses
a ±3σ band (`≈ 0.0343`) — wide enough that a correct hash essentially never
fails it, tight enough that a biased or broken assignment function (which
would shift the ratio by many SEs, as the do-nothing case demonstrates for
outcome resolution) is still caught.

### Report (default seed `20260826`, `--events 2000`)

```
ingest stats           {'inserted': 1611, 'duplicate': 389, 'rejected': 0, 'rejected_by_reason': {}}
population size        1611
unique customer_ids    1611  (must be equal -- enforced by load_population)
split                  treatment=1126 (0.699)  control=485 (0.301)
mean lift              population=0.1042  treatment-arm=0.1039  drift=-0.0003

do_nothing           uplift +0.0005  95% CI [-0.0472, +0.0482]  treatment n=1126 rate=0.2789  control n=485 rate=0.2784
recover_everything   uplift +0.0991  95% CI [+0.0502, +0.1480]  treatment n=1126 rate=0.3774  control n=485 rate=0.2784
targeted_card        uplift +0.0414  95% CI [-0.0069, +0.0897]  treatment n=1126 rate=0.3197  control n=485 rate=0.2784
```

`do_nothing`'s CI straddles zero (PASS gate, unchanged by this hardening
pass) and `recover_everything`'s CI (`[+0.0502, +0.1480]`) clearly excludes
zero and brackets both the population mean lift and the treatment-arm-only
true lift (0.1039). `targeted_card` — nudge only `expired_card` /
`invalid_card`, the high-lift buckets — recovers ~42% of
`recover_everything`'s uplift while nudging the same treatment population,
i.e. concentrating sends on the customers datagen built to actually respond;
its CI still touches zero at this sample size, which is expected and not a
failure of measurement. Population size (1,611) equals the unique
customer_id count (1,611) — the invariant holds on the real dataset.

---

## Slice 6 · rules diagnosis (`app/diagnosis.py`, `tools/label_harness.py`)

### The map is built from `error_code` alone, on purpose

`app/diagnosis.py`'s `diagnose()` reads **only** the raw Razorpay
`error_code` off the preserved payload. It deliberately ignores the
normalized row's `reason` field, which in this dataset simply *is*
`error_reason` (the generator's own label) and would make diagnosis a
trivial identity lookup — nothing to get wrong, nothing to learn. Re-deriving
from `error_code` is the way a real gateway integration that doesn't expose
a clean semantic enum would have to work, and the point is to measure how
far a coarse code alone gets you.

Not far, because `error_code` has exactly two values. Over the 1,863
`payment.failed` events in `data/events.json` (seed `20260826`, verified by
regenerating byte-identically):

| error_code | events | causes it covers |
|---|---|---|
| `GATEWAY_ERROR` | 566 | `bank_downtime` 287, `gateway_timeout` 279 — a near 1:1 pair |
| `BAD_REQUEST_ERROR` | 1,297 | `insufficient_funds` 520, `otp_timeout` 419, `expired_card` 202, `invalid_card` 156 — a four-way collision |

The map is built from that frequency table *before* touching a real event
and just picks each code's plurality: `GATEWAY_ERROR → bank_downtime`,
`BAD_REQUEST_ERROR → insufficient_funds`. So every one of the 1,297
`BAD_REQUEST_ERROR` events gets the single label `insufficient_funds`;
`expired_card`, `invalid_card` and `otp_timeout` — 777 events — are
structurally unrecoverable and score exactly 0.00, and `GATEWAY_ERROR` is a
coin toss between two near-equal causes. That ceiling, not any
implementation detail, is the whole reason the rules top out around **0.32**.

The blind-audit harness (`tools/label_harness.py score`, via
`rules/error_code_map.json`) additionally lets the map key on `method`
alongside `error_code`. It doesn't help. Splitting `BAD_REQUEST_ERROR` by
method still can't separate the four causes, because `method` is
independent of `error_reason` in datagen — the non-card `BAD_REQUEST_ERROR`
subset (812 events) carries the same four causes in nearly the same
proportions (`insufficient_funds` .41, `otp_timeout` .33, `expired_card`
.16, `invalid_card` .11) as the card subset (.39 / .32 / .15 / .14) —
and splitting `GATEWAY_ERROR` by method is just the coin toss made
explicit. With `method` added, the overall rules score is still 0.320.

### How the committed labels were made — blind stratified hand-labeling

An earlier harness, `eval/diagnosis_audit.py`, hand-labeled by mapping the
six fixed `error_description` strings to causes and called that "a
genuinely independent check on the code-only classifier." **That claim is
withdrawn.** `datagen._REASON_DETAIL` emits `error_reason →
error_description` strictly 1:1 across all 1,863 failures — six causes, six
frozen strings, zero variation (verified against `data/events.json`: every
`error_reason` maps to exactly one `error_description`, and no string is
shared between causes). A description lookup and an `error_reason` lookup
are the same table keyed differently, so `HAND_LABEL_BY_DESCRIPTION` was
reading the generator's answer key by another name — not an independent
signal at all.

The committed labels come from `tools/label_harness.py` instead, drawn
blind from the payload:

1. **Failure filter** — drop the 137 `payment.authorized` noise events
   (no `error_code`, not diagnosis targets), leaving 1,863.
2. **Retry dedupe** — collapse rows sharing a `notes.customer_id` to the
   earliest by `created_at`: 252 retry duplicates removed, 1,611 rows
   left (one per customer).
3. **Stratified sample** — join to `ground_truth.json` on payment id,
   then draw n=100 stratified by true cause with a per-class floor of 15
   (six classes × 15 = 90 deterministic floor rows + 10 random top-up),
   at sampling seed `20260829` — deliberately *not* the datagen seed, so
   the sample isn't correlated with generation order. The true-cause key
   and a digest go to `labels/_truth_manifest.json`, which the `label`
   subcommand never opens; the root-cause field is stripped from every
   sampled payload before it is written to `labels/blind_sample.json`.
4. **Label** — one keystroke per row, reading only `error_code`,
   `method`, `amount`, `status`, `created_at`, `error_description`; an
   `unknown` requires a written note. `tests/test_slice6_diagnosis.py`
   guards the integrity of this design, not any accuracy: no root-cause
   key leaks into a blind payload, every true class clears the floor, the
   file and manifest digests agree, no row is left unlabeled, and every
   abstention carries a note.

### The three matrices (blind sample, n=100, sampling seed `20260829`)

**A — human labels vs ground truth**, the payload ceiling (rows = true
cause, cols = blind human label):

```
                     bank_downtime  expired_card  gateway_timeout  insufficient_funds  invalid_card  otp_timeout |  n   acc
bank_downtime              14             0              0                 0                 0             1      | 15  0.93
expired_card                0            17              0                 0                 0             0      | 17  1.00
gateway_timeout             0             0             16                 0                 0             0      | 16  1.00
insufficient_funds          0             0              0                17                 0             0      | 17  1.00
invalid_card                0             0              0                 0                18             0      | 18  1.00
otp_timeout                 0             0              0                 0                 0            17      | 17  1.00
-> overall accuracy = 0.990
```

**B — rules vs human labels**, the real test (rows = blind human label,
cols = rules prediction, via `error_code` + `method`):

```
                     bank_downtime  expired_card  gateway_timeout  insufficient_funds  invalid_card  otp_timeout |  n   acc
bank_downtime               7             0              7                 0                 0             0      | 14  0.50
expired_card                0             0              0                 1                 9             7      | 17  0.00
gateway_timeout            10             0              6                 0                 0             0      | 16  0.38
insufficient_funds          0             0              0                 0                 6            11      | 17  0.00
invalid_card                0             0              0                 2                11             5      | 18  0.61
otp_timeout                 0             0              1                 1                 8             8      | 18  0.44
-> overall accuracy = 0.320
```

**Worst class: `expired_card`, 0/17**, collapsing into `invalid_card` —
both are `BAD_REQUEST_ERROR` on a card, and there is no key in `error_code`
+ `method` that tells them apart. `insufficient_funds` also scores 0.00
(its 17 rows scatter onto `invalid_card` / `otp_timeout` under the method
split). Matrix C (rules vs ground truth, not shown) is 0.320 as well: the
error codes were written *from* the cause so it over-credits the rules, and
the near-zero B↔C gap just says the blind human labels and the answer key
agree almost everywhere — which is the next problem.

### What the 0.99 ceiling actually is — a transcription ceiling, not a signal ceiling

Because `error_description` is 1:1 with `error_reason`, a labeler reading
the payload — which shows `error_description` in plain English — is
transcribing the generator's encoding, not extracting an independent
signal. So matrix A's **0.99 is labeler-vs-generator agreement**: it shows
a human copies the six fixed strings back correctly. It does **not**
establish that the payload carries recoverable root-cause information
beyond the answer key. It is a *transcription* ceiling, not a *signal*
ceiling.

That constrains what matrix B can be claimed to show. The 0.99 → 0.32 gap
is a fair measure of one thing — how little of the generator's own
encoding a coarse `error_code` (± `method`) rule can reproduce — and that
poverty is real. But it is not evidence about a corpus where descriptions
actually vary: there, the human ceiling could sit far below 0.99, and this
dataset gives no way to find out where. Slice 7 is where that limitation
bites.

---

## Slice 7 · LLM diagnosis tail (`app/llm_diagnosis.py`)

### The trigger — the whole `BAD_REQUEST_ERROR` cell

`is_ambiguous(entity)` fires on `error_code == "BAD_REQUEST_ERROR"` and
nothing else: **1,297 of the 1,863 failures (70%)**, split four ways —
`insufficient_funds` 520, `otp_timeout` 419, `expired_card` 202,
`invalid_card` 156. The pipeline rules (`app/diagnosis.py`, `error_code`
only) hand this whole cell the single label `insufficient_funds`, so three
of the four causes — 777 events — are never predicted correctly and the
fourth is right only because it is the constant guess. Nothing in
`error_code`, and per Slice 6 nothing in `error_code` + `method` either,
splits this cell. So the whole cell is what gets handed to the model.
Every non-`BAD_REQUEST_ERROR` event keeps its rules label untouched, with
`attempts = 0` and the transport never constructed.

### Why `method` was dropped from the gate

An earlier gate was `BAD_REQUEST_ERROR` **and** `method == "card"`. That
was wrong. `method` is independent of `error_reason` in datagen —
`pay_000001` is `method: "upi"` carrying `"card number or CVV is invalid"`
(`invalid_card`) — and the non-card `BAD_REQUEST_ERROR` subset, 812
events, shows the same four causes in nearly the same proportions as the
card subset (see the Slice 6 method table). The `method == "card"`
condition was excluding 812 equally ambiguous events for no reason, so it
was removed; `is_ambiguous` now reads `error_code` alone. The same fact
has a second edge: the `+method` half of the Slice 6 audit map contributes
noise, not signal, to the 0.32. That `method` carries no root-cause
information here — a UPI payment can't have an expired card, yet datagen
lets it — is a **known datagen weakness, out of frozen scope and not
fixed**.

### Why the accept path is disabled (`TAIL_ACT_ENABLED = False`)

`error_description` is 1:1 with `error_reason` (Slice 6), and the model is
handed `error_description` in its prompt. So any classifier that reads the
description — the model included — scores a fake ~1.00 by transcription,
exactly as a plain lookup table would. This corpus therefore **cannot
distinguish a real prose classifier from a dictionary**, which makes the
tail unvalidatable here. While the switch is `False`, every LLM-sourced
`Diagnosis` (`source` `llm` or `llm_failed`) is recorded with its proposed
cause and routed to the human queue — never to a money action. The rules
path (`source` `rules`) is unaffected and still acts.

### The consequence, stated plainly

While the tail is gated, **70% of failures take no automated action** —
they are diagnosed just far enough to know the rules can't diagnose them,
then parked for a person. This is deliberate. Acting on a label the rules
produce at 0.00 accuracy for three of the four causes in the cell is
spending nudge budget on a coin flip; a recorded-but-unactioned diagnosis
costs nothing and forecloses nothing. The finding *is* the contribution of
this slice: the rules layer cannot act on most of the failure volume, and
this synthetic corpus cannot validate the classifier that would fix that.

### The fail-closed contract

- **Parse** — the *entire* model response must be one JSON object (a lone
  ```` ```json ```` fence is stripped first; JSON embedded in prose is
  rejected) whose `root_cause` is in the six-cause enum plus `unknown`.
- **Retry** — `TailTimeout` and `OSError` retry up to `max_retries`
  (default 2, so 3 attempts total). A parse failure or an out-of-enum
  answer is **terminal, no retry**: the prompt is deterministic, so a
  retry would only triple the cost for the same output. Any other
  transport exception is terminal too.
- **Abstention** — a clean `{"root_cause": "unknown"}` is an honest
  abstention (`source` `llm`, one attempt), queued for a human; distinct
  from `llm_failed`.
- **Second layer** — `apply()` hard-gates any `source` in `{llm,
  llm_failed}` to the human queue *before* `is_money_eligible` is
  consulted, and `is_money_eligible` (route is `act` **and** the cause is
  a real cause) remains as an independent second check.

### What is not claimed

`tests/test_llm_diagnosis.py` (**35 tests**) verifies four adversarial
transports all fail closed to `unknown` + the human queue and never
spend: garbage text, a forced timeout, an out-of-enum cause, and the
network pulled (`OSError`). The **fifth** failure mode — a well-formed,
in-enum, confidently *wrong* answer — is the dangerous one, and it is
unscoreable on this corpus for the reason the accept path is disabled:
with no description variation, there is nothing against which a
confidently-wrong answer could be caught. It is documented here, not
claimed as handled.

### Test count

`tests/test_llm_diagnosis.py`: **35 passed**. Full suite
(`python -m pytest tests/ -v`): **160 passed**.
