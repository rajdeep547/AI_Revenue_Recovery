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

---

## Slice 7 — Decision engine

**Built.** `decide(event, policy, arm, history) -> Decision`, a pure function
(no DB, no clock). EV per ladder rung: `p_rung × ticket − cost`, best rung by
EV with ties to lower cost. Terminals: ACT, SKIP (7 reasons), ROUTE_TO_HUMAN.
Every decision is a flat append-only row in `decisions` (Slice 2 trigger
pattern), written with its audit row in one transaction.

**Priors are stated guesses, not measurements.** `app/` may not read
`ground_truth.json`, so `p_incremental` comes from a hand-written table in
`config/decision_policy.json`. Six of seven entries are marked PLACEHOLDER FOR
HUMAN REVIEW with a written basis each. This is the point of the control arm:
Slice 5 measures whether these priors were right. `unknown` (0.10) is the
exception — it equals `population_incremental` by construction, and a test
enforces the coupling.

**Ticket is gross, not contribution margin.** A real merchant would use
margin; using gross inflates every EV by COGS. Recorded as an assumption
rather than silently chosen.

**Minimum EV floor (₹2.00), not zero.** Positive-but-tiny EV is
indistinguishable from zero at these sample sizes, so acting on it is
noise-chasing.

**ROUTE_TO_HUMAN is a policy override, not an EV bound.** First attempt gated
it on a negative lower-bound EV. That was wrong twice over: the literal form
scales *up* with ticket size (unreachable), and a population-subtracted form
is ticket-independent (always firing above the threshold). On pure EV a
low-confidence high-value case *should* be acted on — the downside of a wrong
`agent_call` is the wasted ₹42 and nothing more. The real reasons to escalate
(wrong script, brand damage, annoying a flagged customer) are outside the
model, so they are an explicit rule: best rung in `high_touch_rungs` AND
ticket ≥ ₹10,000 AND confidence < 0.55. `ev_lower_inr` remains on the record
as a diagnostic and gates nothing. `gate_basis` records which mechanism
decided: `expected_value` | `policy_override` | `hard_gate` | `experiment`.

**Control arm records a shadow decision.** A control-arm event still runs the
full ladder and records what it *would* have done, then terminates
`CONTROL_ARM`. Without this, uplift compares "treated events we chose to act
on" against "all control events including ones we'd have skipped" — which
silently corrupts the one number the project rests on.

**Rationale percentages must be the ones the EV was computed from.** The first
version rendered `p_effective` while `ev_inr` used `p_rung = p_effective ×
effectiveness`. A reader multiplying the sentence's own numbers got a
different answer (3.9% × ₹2,500 − ₹42 = ₹55.25 against a stated ₹97.56). Added
`p_action_basis`, rendered at 2dp; a test parses the numbers back out of the
rendered string and asserts `pct × ticket − cost == ev_inr` within the
display-rounding band, and a paired test proves substituting `p_effective`
breaks it.

**Cause vocabulary drifted and was reconciled.** The prior table was written
against invented cause names; four of six production causes had no reviewed
prior, and all four break cases ran on causes the rules engine cannot emit.
The totality test now derives its expected set at test time from
`rules/error_code_map.json` (`map` values + `default`) unioned with
`app.diagnosis.ROOT_CAUSES`, and asserts set equality in *both* directions —
a prior for an unreachable cause fails as loudly as a missing one.
`expired_card` (0.22) is reachable only via the LLM diagnoser; `invalid_card`
(0.16) is the broader blended cause the rules map emits. Both modules export
the same seven causes today; if they ever drift, the totality test would stop
covering the LLM path.

**Ladder non-degeneracy is tested.** `retry_silent` at ₹0.00 made EV
structurally non-negative for any positive prior, leaving `min_ev_inr` as the
only thing that could skip a cheap action, and dominating `email` everywhere.
Priced at ₹0.05 (gateway attempt slot, soft-decline penalty). A grid sweep
asserts every rung is argmax somewhere: retry_silent 13, email 17, sms 16,
whatsapp 109, agent_call 69 of 224 cells.

**`action` on ROUTE_TO_HUMAN is a proposal.** It carries the proposed rung, so
`action is not None` does not imply authorised spend. Consumers gate on
`terminal == ACT`.

Four break cases: risk-blocked ₹30,000 (skips before any EV arithmetic, ladder
monkeypatched to prove it); ₹30 ticket (EV ₹0.94 below floor — re-run at
`min_ev_inr=0` flips to ACT, proving the floor and not the arithmetic
skipped it); repeat customer with prior control-arm self-recovery (p_effective
×0.25, and at ₹2,000 the penalty moves the chosen rung, not just a number);
₹18,000 at confidence 0.40 (routes; controls at confidence 0.85 and at ₹900
each ACT, proving both conjuncts load-bearing).

## Slice 8 — Integration

**Built.** `app/pipeline.py::process_failure(payment_id, *, db_path, policy,
now_utc)`: load normalized row → rules diagnose → assign arm → decide →
record. No new decision or diagnosis logic, no action execution. Idempotent —
a second call returns the existing Decision without a second row. ACT means
*recorded*, not *sent*; the module exposes no send/execute/dispatch callable.
Wired into the webhook handler after the state machine settles a payment as
failed; any pipeline failure is logged and swallowed so the webhook still
returns 200 (Razorpay retries on non-2xx, and a decision bug must not cause a
redelivery storm).

**`assign_arm` moved to `app/arms.py`.** The pipeline initially did
`from eval import measurement`, which passed the isolation test only because
`"eval "` is not the `"eval."` substring it searched for — working around the
guard, not satisfying the rule. `assign_arm` is a pure sha256 helper, so it
now lives in `app/` and `eval/measurement.py` imports it from there. One
implementation, asserted by identity. Slice 5's output is byte-identical after
the move. The isolation test was hardened to catch any route from `app/` into
`eval/`.

**Seed provenance.** `experiment_seed` (20260826) must equal
`datagen.DEFAULT_SEED`; a test imports it rather than re-typing the literal,
and an arm-parity test confirms pipeline and measurement agree for 60
customers. `tools/label_harness.py` uses SEED 20260829 for blind sampling —
deliberately different, deliberately decorrelated from generation order, and
commented so nobody "fixes" one to match the other.

**Confidence is a flat 0.32, borrowed from a sibling classifier.** From Slice
6's blind audit (`slice6_score.txt` matrix B, n=100, per-class floor 15). Two
caveats, stated rather than buried: it is a corpus-wide figure used as a
per-event, per-cause confidence (not calibrated per cause), and it scored
`rules/error_code_map.json` (error_code + method) while the pipeline runs
`app.diagnosis` (error_code only). Consequence: priors spanning 5.5×
(0.04–0.22) compress to 1.7× in `p_effective` (0.081–0.138), all hugging the
0.10 population rate. The rules diagnosis barely moves the needle.

**Known boundary — the synthetic corpus is narrower than production.** Across
all 1,611 ingested rows: 100% carry email, 0% carry phone (datagen's `notes`
object holds only `customer_id` and `email`). So `sms`, `whatsapp` and
`agent_call` can never be chosen, `agent_call` is the sole `high_touch_rungs`
entry, and ROUTE_TO_HUMAN is unreachable on synthetic data — test-covered but
not data-reachable. Live outcome distribution on the corpus: 69.9% ACT/email,
30.1% SKIP/CONTROL_ARM. This is a data-generation gap, not a policy defect,
and the corpus was deliberately *not* regenerated to hide it. The live webhook
below confirms real Razorpay payloads do carry `contact` — the first live
decision chose `sms`.

**`SkipReason.NO_CONTACT_CHANNEL` is unreachable in production.**
`retry_silent` requires no channel, so a contactless customer is always
reachable. The Slice 7 test that exercises it does so by deleting
`retry_silent` from the ladder. The live bridge therefore lets a contactless
payload through to the engine (`allow_missing_contact=True`) rather than
quarantining it.

**Slice 4 was modified during Slice 8** — flagged rather than passed quietly.
`allow_missing_contact: bool = False` threaded through `_contact`, the three
adapters, `normalize`, and `Ingestor.ingest`. Default `False`; all 63 existing
ingest tests pass unchanged.

**PASS — live end-to-end, 2026-08-30T19:32:26Z.** A real Razorpay test payment
failed, the webhook was signature-verified (an unsigned probe in the same log
was rejected 401), the payload bridged through the Slice 4 `card_failure`
adapter, and one decision was recorded.

---

## Slice 9 · Guardrails (`app/guardrails.py`, `config/guardrails.json`)

Seven guardrails sit between the Slice 7 EV ladder (which picks a best rung)
and a dispatch this codebase still does not perform. Each is a **pure
predicate** over `(event, chosen_rung, state, now)` returning
`GuardrailResult(name, blocked, reason, detail)`. `state` (a `GuardrailState`)
carries the config plus four pre-aggregated numbers the caller reads from the
DB (`load_state`): `opted_out`, `payment_action_count`,
`contact_actions_in_window`, `spent_today_inr` — so the predicates stay pure
and `evaluate_all` keeps its 4-arg shape.

### Evaluate-all-then-decide — the point of the slice

`evaluate_all(event, rung, state, now) -> GuardrailReport` runs **all seven**,
**no early return, no short-circuit**, even after the first one blocks. The
report holds all seven `GuardrailResult`s in a fixed tuple order.
`report.blocked_by` is every guardrail that blocked (length may be > 1), in
that fixed order. `report.terminal` is `BLOCKED/<PRIMARY>` where PRIMARY is
`blocked_by[0]` — the first blocker in the fixed order — while `blocked_by`
retains all of them.

`record_evaluation` persists the **whole** report to the append-only
`guardrail_evaluations` table (same `BEFORE UPDATE`/`BEFORE DELETE` →
`RAISE(ABORT, …)` pattern as Slice 2's `audit`): columns `event_id,
customer_id, rung, ts, guardrail_name, blocked, reason, detail_json`. **Seven
rows per evaluation, always** — the five that did not fire are written with
`blocked = 0`, not omitted. `test_quiet_hours_and_spend_cap_both_recorded` is
the canary: an event at 22:30 IST with the ledger already at the daily cap
must produce `terminal == BLOCKED/QUIET_HOURS` (precedence), `len(blocked_by)
== 2`, and exactly 7 persisted rows with **both** `quiet_hours` and
`spend_cap` at `blocked = 1`. Anyone who adds a short-circuit later fails that
test.

### Precedence order (fixed)

```
kill_switch → opt_out → attempt_cap → contact_limit → quiet_hours → spend_cap → dry_run
```

1. **kill_switch** — global config bool. Blocks every rung, `retry_silent`
   included.
2. **opt_out** — `customer_id` in the `suppression_list` table. Blocks contact
   rungs (`email`/`sms`/`whatsapp`/`agent_call`); `retry_silent` still runs
   because no message reaches the customer.
3. **attempt_cap** — max lifetime **dispatched** actions per `payment_id`
   (default 3), counted as real debits in `spend_ledger`, any rung.
4. **contact_limit** — max **dispatched** contact-rung actions per
   `customer_id` in a rolling 24h window (default 2), counted **across
   channels**. `retry_silent` is not a contact rung, so it never trips this.
5. **quiet_hours** — blocks `sms`/`whatsapp`/`agent_call` between 21:00 and
   09:00 **IST**; `email` and `retry_silent` pass. Boundaries: blocked at
   exactly 21:00, allowed at exactly 09:00. A `now` in any offset is converted
   to IST first.
6. **spend_cap** — daily ₹ ceiling on `action_cost`. Blocks when
   `(sum of real debits for the current IST day) + rung_cost > cap` (default
   ₹500, strict `>` so exactly-at-cap is allowed).
7. **dry_run** — global config bool. **Never blocks.** Records `dispatched =
   False` so the resulting `spend_ledger` row is amount 0.00 / status
   `dry_run`.

### IST is a fixed +05:30 offset, not `ZoneInfo`

The target interpreter ships no `tzdata`, so `ZoneInfo("Asia/Kolkata")` raises.
Asia/Kolkata has been a DST-free fixed +05:30 since 1945, so
`timezone(timedelta(hours=5, minutes=30))` is exact and adds no dependency.
This is a deliberate implementation choice, recorded here — not a missing
upstream field.

### Ladder interaction — walk down, log every rung

`walk_ladder(event, ranked_rungs, state, now)` takes the rungs **best-first**
(the engine's EV order), calls `evaluate_all` on each, and stops at the first
rung whose report is not blocked → `terminal = "ACT"`, that rung chosen. Every
rung tried keeps its **own full `GuardrailReport`** in `outcome.attempts` —
`record_ladder_walk` writes one seven-row block per attempt, never collapsed.
If every rung is blocked, `terminal = BLOCKED/<PRIMARY of the highest rung
tried>` (`attempts[0]`), `chosen_rung = None`.

### Spend ledger

`spend_ledger`, append-only (same trigger pattern). `record_spend` writes one
row per outcome: a real dispatch (`dispatched and not dry_run`) →
`status='debit'`, amount = the rung's cost; a dry-run → `status='dry_run'`,
amount 0.00; a blocked action → `status='blocked'`, amount 0.00. The spend-cap
window (`spent_today_inr`) sums **only** `debit` rows for the current IST day
(keyed on a stored `ist_day` column). Since nothing in the codebase dispatches
yet, a real `debit` only ever appears in tests that pass `dispatched=True`
explicitly — consistent with Slice 8's "no send/execute/dispatch callable".

### Arm integrity — `treatment_blocked` as a third outcome class

Guardrails **never** touch arm assignment. A treatment-arm event that gets
fully blocked stays treatment arm and becomes a **third outcome class**,
`treatment_blocked`, distinct from `control` and `treatment_acted`. In
`eval/measurement.py`, `run_policy` gains a `blocked_fn(row) -> bool`
predicate: a treatment-arm customer for whom it returns True is resolved under
the **untouched baseline** (`"none"` — no nudge reached them), counted in a new
`UpliftResult.treatment_blocked` `ArmResult`, and **excluded from the uplift
denominator**. Control is never consulted for these customers — a blocked
customer is never reclassified as control. `uplift` and the Wald CI are
computed on `treatment_acted` vs `control` only; `treatment_blocked` is
reported separately (`_fmt` appends it when non-empty).
`test_treatment_blocked_excluded_from_uplift_denominator` pins that blocking 3
treatment customers leaves `control.n` / `control.n_recovered`
byte-identical, moves exactly 3 out of `treatment_acted`, and yields a
`(uplift, ci_low, ci_high)` identical to physically dropping those 3 rows.
`blocked_fn` defaults to `None`, so every pre-Slice-9 caller and all Slice 5
numbers are unchanged (the measurement report is identical bar the
`treatment` → `treatment_acted` label).

**No `decisions`-table schema change was needed** (the brief said to STOP if it
were). The arm is already inferable from the recorded `Decision`
(`skip_reason == CONTROL_ARM` ⟺ control, else treatment, per Slice 8), and a
full block is completely described by the `guardrail_evaluations` rows for that
`event_id` plus the `decisions` row. `treatment_blocked` is a *measurement-layer
classification* over those two append-only logs, not a new column.

### Not yet wired into the runtime pipeline

Slice 9 delivers the guardrail library, its config, the two new append-only
tables and the measurement support — mirroring the Slice 7→8 split where the
decision engine landed before the pipeline consumed it. `process_failure` and
the webhook handler are untouched; wiring `walk_ladder` in after `decide`
returns is the next integration step.

### Tests

`tests/test_guardrails.py` (9 tests, all clocks frozen explicitly via an IST
`datetime`, no wall-clock read anywhere):
`test_kill_switch_blocks_everything`, `test_opt_out_blocks_all_contact_rungs`,
`test_attempt_cap_blocks_after_max`,
`test_contact_limit_blocks_within_rolling_window` (inserts one in-window and
one 30h-old contact debit, asserts the old one has rolled out),
`test_quiet_hours_blocks_sms` (incl. the 09:00/21:00 boundaries and a UTC
timestamp converted to IST), `test_spend_cap_blocks_when_exhausted` (incl. a
debit on another IST day not counting), `test_dry_run_does_not_debit_spend`,
`test_quiet_hours_and_spend_cap_both_recorded` (the anti-short-circuit canary),
`test_treatment_blocked_excluded_from_uplift_denominator`. Full suite: **211
passed** (202 prior + 9), prior counts unchanged.

---

## Slice 9b · wiring the guardrails in (`app/decision/engine.py`, `app/pipeline.py`, `app/guardrails.py`, `eval/measurement.py`)

### 1. Caps count *commitments*, not debits

`attempt_cap` and `contact_limit` were counting `spend_ledger` rows with
`status='debit'`. Nothing dispatches and there is no transport, so every
committed action is written `status='dry_run'` — both caps were dead in every
runnable mode. `_count_payment_actions` and `_count_contact_actions_since` (and
therefore `load_state`) now count `status IN ('debit','dry_run')` — every
action the policy *committed to*, whether or not money moved. `status='blocked'`
stays uncounted: a blocked action consumed no attempt and reached no customer.
The money `spend_cap` is unchanged — `spent_today_inr` still sums `debit` rows
only. `test_attempt_cap_blocks_after_max` and
`test_contact_limit_blocks_within_rolling_window` were extended to assert the
commitment semantics and that a `blocked` row does **not** count.

### 2. `walk_ladder` in the decision path

`app/decision/engine.py` gains `decide_with_ladder(event, policy, arm, history)
-> (Decision, list[dict] | None)`. `decide()` is now a thin wrapper over the
same internal `_decide()`; every call site is unchanged. The second element is
the EV-ranked, channel-eligible `action_ladder` entries (best-EV first) **only**
on the ACT terminal — for the pre-ladder hard gates and for `CONTROL_ARM`,
`EV_BELOW_FLOOR`, `ROUTE_TO_HUMAN` it is `None`. So a skip or a route
structurally cannot reach the guardrail walk; `process_failure` also guards on
`decision.terminal == Terminal.ACT`.

`process_failure` calls `_apply_guardrails` after `decide_with_ladder` and
before `record_decision`:

- **acted on the EV-best rung** — Decision returned unchanged; one `dry_run`
  `spend_ledger` commitment row is appended.
- **walked to a lower rung** — `action` becomes the walked-to rung; the
  rationale gets a ` | GUARDRAILS: EV-best rung <X> unavailable (<rung>←<primary
  blocker>; …); walked down to <Y>.` clause; one `dry_run` commitment row for
  the chosen rung. `ev_inr` / `p_action_basis` still describe the EV-best rung —
  the walked-to rung and every higher rung's primary blocker are recoverable
  from the rationale and from `guardrail_evaluations`.
- **every rung blocked** — `terminal` becomes `BLOCKED/<PRIMARY of the highest
  rung tried>` (a plain string; the `decisions.terminal` column already takes
  it), `action` cleared, `gate_basis="guardrail"`, a ` | GUARDRAILS: every rung
  blocked (…)` rationale clause, and **no** `spend_ledger` row.

`record_ladder_walk` runs for every walk — seven `guardrail_evaluations` rows
per rung tried, never collapsed (a 3-rung walk-down writes 21 rows). Idempotency
is unchanged: the second `process_failure` for a payment returns early at
`_existing_decision`, before the walk, so no duplicate guardrail or ledger rows.

**No `decisions`-table column was added.** `ev_best_rung` is (a) the first rung
in that `event_id`'s `guardrail_evaluations` walk (insertion order = walk order
= EV order) and (b) named in the rationale; `chosen_rung` is `decisions.action`.
`gate_basis` gains one value, `"guardrail"` — a value addition, not a schema
change, matching how Slice 9 added `treatment_blocked` without a column.
`_REAL_SPEND_ENABLED = False` in `pipeline.py` is the one flag to flip when a
real transport lands: it turns the commitment row from `dry_run` into a real
`debit`.

### 3. `treatment_blocked` fed from real data

`eval/measurement.py` gains `blocked_customers_from_guardrail_log(db_path) ->
set[str]`: the `customer_id`s whose walk, for at least one `event_id`, blocked
**every** rung it recorded (no actionable rung found). Missing table → empty
set, so the caller degrades cleanly to `blocked_fn=None`. `main()` takes
`--guardrail-db`; when given, that set becomes the `blocked_fn` passed to
`run_policy`, so a real run reports a real `treatment_blocked` bucket instead of
an always-empty one. Without the flag, Slice 5's numbers are byte-identical.

### 4. Regression pinned

`test_ev_below_floor_skips_before_the_ladder_and_never_reaches_guardrails`:
`insufficient_funds`, ₹10.00, best rung `sms`, EV ≈ ₹0.46 < the ₹2.00 floor →
`SKIP/EV_BELOW_FLOOR` at engine stage (h), before the walk. Asserts the
terminal is unchanged and that **no** `guardrail_evaluations` or `spend_ledger`
rows exist for that event. `test_control_arm_skip_never_reaches_guardrails`
pins the same for `CONTROL_ARM`.

### Tests

`tests/test_slice9b_wiring.py` (6 new): clean-ACT records the walk but leaves
the Decision untouched; walk-down to a lower rung records both rungs and the
why (+ idempotency); every-rung-blocked → `BLOCKED/OPT_OUT` with no action and
no commitment row (+ idempotency); the two pre-ladder-skip regressions; and
`blocked_fn` derived from the guardrail log feeding a real `treatment_blocked`
bucket. `tests/test_guardrails.py` keeps its 9 (two extended for
commitment-counting). Full suite: **217 passed** (211 prior + 6), prior counts
unchanged.

---

## Slice 9c · verification (`tests/test_slice9c_verify.py`)

Read-only pass over the 9b wiring; one test added,
`test_walk_down_produces_lower_rung_and_full_log`. It builds a treatment-arm
`insufficient_funds` event, ₹200, with a phone, at `2026-08-31T22:30:00+05:30`
(22:30 IST, inside quiet hours). EV order is whatsapp > sms > email >
retry_silent > agent_call, so the engine's EV-best rung is `whatsapp` and it
terminates `ACT`; the guardrail walk then blocks whatsapp and sms on
`quiet_hours` and lands on `email`. Asserts: `decisions.action == 'email'`;
exactly **21** `guardrail_evaluations` rows for the event_id, in three
seven-row groups `{whatsapp, sms, email}` (walk not collapsed); the
`quiet_hours` row is `blocked=1` in both the whatsapp and the sms group while
email's group has zero blocks; the rationale contains `"EV-best rung whatsapp"`
and `"walked down to email"` (so `ev_best_rung` is recoverable with no column);
and `spend_ledger` holds exactly `[("email", "dry_run", 0.0)]` — the commitment
row is for the walked-to rung, not the EV-best one. Full suite: **218 passed**
(217 prior + 1), prior counts unchanged.

### Walk call site and its guards

`walk_ladder` is called at exactly one place, `app/pipeline.py` inside
`_apply_guardrails` (`outcome = guardrails.walk_ladder(event, ranked_rungs,
gstate, now_utc)`). `_apply_guardrails` itself is invoked from one place,
guarded by `if decision.terminal == Terminal.ACT and ranked_rungs:` — two
independent conditions, and `decide_with_ladder` returns a non-`None`
`ranked_rungs` *only* on the ACT return (every other terminal returns
`(decision, None)`). The call also sits after the `_existing_decision` early
return, so a redelivery never re-walks.

### No path reaches the walk on a CONTROL_ARM (or any skip / route) event

A `CONTROL_ARM` decision has `terminal == "SKIP"` (first guard fails) *and*
`decide_with_ladder` returns `ranked_rungs = None` for the control branch
(second guard fails). Same for the pre-ladder hard gates, `EV_BELOW_FLOOR` and
`ROUTE_TO_HUMAN`. The `BLOCKED/<PRIMARY>` terminal is produced only *inside*
`_apply_guardrails`, after the walk, so it cannot re-trigger the guard.
Guardrails never turn a skip or a route into an act. Pinned by
`test_ev_below_floor_skips_before_the_ladder_and_never_reaches_guardrails` and
`test_control_arm_skip_never_reaches_guardrails`.

### Known gap: non-atomic ladder-walk logging

`record_ladder_walk` commits per-rung (connect -> 7 rows -> commit -> close per
rung), so a crash mid-walk leaves 7 or 14 orphaned rows for an event whose
decision was never recorded. Append-only triggers mean they cannot be removed.
A retry appends a fresh walk on top, so an `event_id` may hold 21 + a partial
group.

Fail-closed on what matters: **no `decisions` row, no `spend_ledger` row**
(`record_ladder_walk` runs before `record_spend`, which runs before
`store.record_decision`, and nothing between `decide_with_ladder` and
`record_decision` catches). No rewrite of existing rows, so the append-only and
arm-integrity guarantees hold. The gap is row-count arithmetic for auditors,
not correctness of any recorded outcome.

Fix (deferred, post-deadline): wrap the whole walk in one transaction so all 21
rows commit or none do. Not taken before 3 Sep -- changing the audit write path
under deadline is a worse risk than the documented gap.

---

## Slice 10 · real execution (`app/execution/`, `audit/slice10_idempotency.py`)

Standalone library, deliberately NOT wired into `process_failure` (same
staging as Slice 9 before 9b): the decision path is untouched, `_apply_guardrails`
still writes its `dry_run` `spend_ledger` row. The executor is driven by
`tests/` and the `python -m app.execution.{run_once,reconcile}` CLIs. The
runtime state on entry was verified first: there was no prior execution client
or caller to preserve, so "fake_client = existing behaviour" resolves to a
deterministic no-network `SENT` that mirrors the "recorded, nothing left the
process" stance the spend_ledger has held since 9b.

### The seam

`ActionClient` Protocol = `send(req) -> ExecutionResult` **plus** `lookup(idem_key)`
(added beyond the brief's one method: `reconcile` needs the provider's own view
keyed by `reference_id`, and one object should serve both paths).
`ExecutionStatus` = `SENT | DUPLICATE | FAILED_RETRIABLE | FAILED_TERMINAL`;
`SENT`, `DUPLICATE`, `FAILED_TERMINAL` are terminal, `FAILED_RETRIABLE` is not. Selection is config-only via
`app.execution.config.build_client`: `EXECUTION_MODE` (`fake` default |
`razorpay_test`), gated by `LIVE_EXECUTION_ENABLED` (must be truthy) and, at
`RazorpayClient` construction, `RAZORPAY_KEY_ID.startswith("rzp_test_")` or
`ValueError`. The key secret is never logged (these modules emit no logs) and
every provider error string is run through `redact(text, key_secret)` before it
reaches an `ExecutionResult` or the ledger's `error_redacted` column.

### `attempt_n` (business attempt) vs transport retry

`idem_key = sha256(f"exec:{event_id}:{action_type}:{attempt_n}").hexdigest()[:32]`.

`attempt_n` is the **business attempt** — a human or a policy deliberately
deciding to re-contact the customer. It is the only input that moves the
idem_key, so a genuine second nudge gets a fresh key and a fresh provider
object. A **transport retry** — what `executor._call_with_retries` does after a
5xx / timeout / connection error (3 retries, exponential 0.5/1/2 s + ≤25%
jitter) — does **not** touch `attempt_n` and reuses the exact same idem_key on
every HTTP attempt, so the provider sees one logical operation. This is stated
in `app/execution/client.py` and enforced by B2 (four HTTP attempts, one
idem_key, one terminal outcome row).

### The `exec:` hash prefix

The idem_key input is prefixed `exec:` so its hash space cannot collide with
the two existing sha256 spaces: `assign:{run_seed}:{customer_id}` (arm
assignment, `app/arms.py`) and the bare `{run_seed}:{customer_id}`
outcome-resolution draw (`eval/environment.py`). Same discipline as Slice 5's
`assign:` deviation. Only the 32-hex digest ever crosses to the provider (as
`reference_id`); the prefix is an internal namespace guard.

### Intent-before-send ordering

`execute()`:
`(a)` compute idem_key + `request_fingerprint` (sha256 of the canonicalised
payload); `(b)` intent exists with a terminal outcome → return it, **zero**
provider calls; `(c)` intent exists, same idem_key, different fingerprint →
`FAILED_TERMINAL` `"idem_key_payload_mismatch"`, no send; `(d)` `insert_intent`
**COMMITs** before `_call_with_retries` makes any network call; `(e)` append
the outcome. `insert_intent` is a plain `INSERT` (not `OR IGNORE`): a second
writer on the same idem_key gets `sqlite3.IntegrityError` on the PRIMARY KEY —
that IS the concurrency lock (B7: two threads, one provider call). An intent
that exists with a non-terminal / missing outcome and a matching fingerprint is
**not re-sent** by `execute()` (double-send risk across workers); it returns the
last known non-terminal state and leaves recovery to `reconcile`. Both ledger
tables are append-only via `BEFORE UPDATE` / `BEFORE DELETE` → `RAISE(ABORT)`,
same as Slice 2's `audit` (B8).

`reconcile` (startup, `python -m app.execution.reconcile`): for every intent
whose latest outcome is missing or `FAILED_RETRIABLE`, `client.lookup(idem_key)`;
found → append `SENT`/`DUPLICATE` with the real `provider_ref` (adopted); not
found → left non-terminal, **no auto-resend this slice**. B5 (SIGKILL between
the provider recording the POST and us writing the outcome) proves the stub
received exactly one request, the intent was committed, and reconcile converges
the ledger to one terminal outcome with no second POST. B6 (SIGKILL inside the
pre-commit hook) proves zero intent rows and zero provider requests persist.

### Provider-side dedup: which mechanism Razorpay actually honours — LIVE-CONFIRMED

Target object = a **Payment Link** (`POST /v1/payment_links`); a nudge creates
a link for the failed amount. Confirmed by a live Razorpay **test-mode** run
(not just the docs):

- **`reference_id` uniqueness is the SOLE provider-side mechanism.** Payment
  Links accept `reference_id` (≤ 40 chars — the 32-hex idem_key fits) and
  **reject a duplicate with HTTP 400** rather than returning the original
  object. The client maps that 400 → `DUPLICATE` and recovers `provider_ref`
  via `GET /v1/payment_links?reference_id=<idem_key>`.
- **`X-Razorpay-Idempotency-Key` was never sent.** That header is a RazorpayX
  Payouts / idempotent-Refunds feature; it does nothing on Payment Links or the
  core Payments API. `reference_id` uniqueness carried the whole dedup.

`idem_key` is sent as `reference_id` verbatim; test mode accepted the 32-hex
string (the docs' "must be a unique *number*" wording is stale). `provider_ref`
= the Payment Link `id` (`plink_...`).

**Live evidence.**

```
Run 1  (exec.db, fresh ledger)
       idem_key      e24bd74614cb28242eb218deb1863409
       provider_ref  plink_TWNoPH228Xf5KF
       status        SENT

Run 2  (exec.db, replay — identical event_id/action_type/attempt_n)
       idem_key      e24bd74614cb28242eb218deb1863409   (same)
       provider_ref  plink_TWNoPH228Xf5KF               (same)
       status        SENT                               (NOT DUPLICATE)
       -> local-ledger short-circuit (executor step b). audit shows outcomes=1,
          i.e. ZERO provider calls on this run.

Run 3  (exec_lost.db, FRESH ledger, identical business key)
       idem_key      e24bd74614cb28242eb218deb1863409   (same input -> same key)
       provider_ref  plink_TWNoPH228Xf5KF               (resolved, not created)
       status        DUPLICATE
       -> a real HTTP POST left the process; Razorpay rejected it on
          reference_id uniqueness (HTTP 400); the original link was resolved via
          GET /v1/payment_links?reference_id=<idem_key>.

audit exec.db       intents 1 · outcomes 1 · terminal 1 · refs 1 · duplicates 0 · OK
audit exec_lost.db  intents 1 · outcomes 1 · terminal 1 · refs 1 · duplicates 0 · OK
```

**Reading the ledger: `SENT` on replay vs `DUPLICATE` are different signals.**
A `SENT` outcome returned on a replay (Run 2) is the **original** outcome
handed back by the local-ledger short-circuit — no provider call happened, and
the status is whatever the first send recorded. `DUPLICATE` (Run 3) means the
**provider itself** rejected a POST that actually went out, because the
`reference_id` already existed on their side; `provider_ref` was then recovered
by lookup, not minted. So: `SENT` twice for one idem_key with `outcomes = 1`
means the ledger absorbed the replay; a `DUPLICATE` row means an HTTP call left
the process and Razorpay was the thing that deduped it. When auditing later,
`DUPLICATE` is the marker that the local ledger was lost/bypassed and
provider-side dedup was the backstop that held.

### Tests / audit

`tests/test_slice10_execution.py` — 11 tests, B1–B9 (B9 is three: live-flag
gate, non-test-key gate, TAIL_ACT never reaching the executor). B5/B6 spawn a
real subprocess + a real `http.server` stub and `Popen.kill()` the child
(TerminateProcess on Windows = uncatchable, SIGKILL-equivalent). Executor
`sleep` is stubbed everywhere — no wall-clock dependence.
`audit/slice10_idempotency.py` re-derives three invariants straight from the
ledger (≤1 terminal outcome per idem_key; ≤1 distinct `provider_ref` per
`(event_id, action_type, attempt_n)`; every `provider_ref` appears exactly
once) and exits non-zero on any violation.

Full suite: **229 passed** (218 prior unchanged + 11).

### Status

PASS condition met. The live test-mode run (Runs 1–3 above) confirmed the
provider-side dedup mechanism; `audit/slice10_idempotency.py` is clean on both
ledgers. Full suite **229 passed** (218 prior unchanged + 11 new). The executor
is **not** wired into `process_failure` yet — that is a later slice.

## Slice 11 · corpus freeze (`scripts/run_corpus.py`, `scripts/render_readme_numbers.py`, `.github/workflows/ci.yml`)

Runs the whole existing pipeline over the full 2,000-event corpus once and
pins the result to two committed artifacts. Invents no behaviour: `Ingestor` →
`app.diagnosis.diagnose` → `app.arms.assign_arm` →
`app.pipeline.process_failure` (the real decision engine, guardrail walk-down
included) → `eval.environment.Environment.resolve`. `scripts/` is offline
tooling; nothing under `app/` imports it.

### The two artifacts

- **`results/final_run.json`** — STEP 1. All 2,000 events, locked 70:30 split,
  driven through `process_failure` (idempotent per `payment_id`, so the
  earliest event for a customer sets the decision). Aggregates are split by
  unit and every field name carries its unit:
  - `customer_level` (`n_customers` = 1611): arm counts, `recovery_rate_*`,
    `recovery_count_*`, `incremental_uplift_pp`, `uplift_ci_95`,
    `incremental_recoveries_count`, `total_recovered_value_inr`
    (+ `_treatment_inr` / `_control_inr`), and — added in the corrections pass
    below — `counterfactual_recovered_value_treatment_inr`,
    `incremental_recovered_value_inr`,
    `incremental_value_per_treated_customer_inr`, **`net_incremental_ev_inr`**
    (the headline), the `mean_ticket_*` fields, `ticket_balance_check`, and the
    generated `value_vs_count_note`.
  - `event_level` (`n_events` = 2000): `action_counts_by_rung` (events),
    `distinct_actions_by_rung` (unique `payment_id` per rung),
    `skip_counts_by_reason`, `route_to_human_count`,
    `suppressed_by_guardrail_count`, `total_action_cost_inr` (over DISTINCT
    actions), `total_action_cost_events_inr` (kept for comparison).
  - top-level `synthetic_provenance_note` (FIX 7, rendered first in the README
    block) and `policy_selectivity_note`; aggregate-level
    `gross_recovered_value_all_arms_inr` (demoted — see below) with its
    warning sibling; `multi_failure_customers` = 369.
  - `per_event`: 2,000 rows, one per event, **sorted by `event_id`**.
- **`results/split_comparison_500.json`** — STEP 2. The **first 500
  `customer_id`s in sorted order** (not 500 events); the resulting event count
  is recorded as `n_events_in_slice` = 624 and `"scope": "500 customers"` is
  stated inside the file. Treatment fraction swept 70:30 vs 90:10 via
  `app.decision.engine.decide_with_ladder` — the only existing seam that takes
  a `treatment_fraction` override without touching `config/` or
  `eval.measurement`. Each split is cross-checked against
  `eval.measurement.run_policy` (same arm hash, same resolver); a mismatch on
  `(t_rec, t_n, c_rec, c_n)` is a hard `SystemExit`.

### `now_utc` — per-event, from committed data (spec correction)

Not a single pinned constant. For each event `now_utc` is derived from that
event's own **top-level integer `created_at`** (unix epoch, UTC) in
`data/events.json` — the only timestamp anywhere in the committed payload.
`require_timestamps()` hard-stops the run if any event lacks a usable one; no
wall-clock or constant fallback exists. Events are **processed** in ascending
`(created_at, event_id)` order so guardrail/cooldown state accumulates in
corpus order (`processing_order` string in the artifact); the `per_event`
output block is **independently sorted by `event_id`** — processing order and
output order are separate concerns. `provenance` carries `corpus_time_min` /
`corpus_time_max` (2025-01-01T00:00:54+00:00 … 2025-01-20T23:45:50+00:00).

Note: with this corpus each customer has exactly one `payment_id` and one
decision row, so `_history`'s cooldown lookup never has a sibling row to fire
against; the per-event `now_utc` still feeds the guardrail walk's time
predicates and the provenance window.

### Determinism contract

`sort_keys`, every float rounded to 6 dp (`-0.0` normalised to `0.0`),
`per_event` sorted by `event_id`, newline-terminated. No wall-clock / uuid /
duration / run_id in the artifact — those go to a **gitignored** sibling
`<name>.meta.json`. Every random draw keys off the committed seed 20260826
(`assign:{seed}:{cid}` for the arm, `{seed}:{cid}` for outcome resolution) —
no new unseeded source. Provenance block records `sha256_*_json_normalized` for
`events.json`, **`ground_truth.json`** (the dominant input to `recovered` —
added per spec correction 3), `decision_policy.json`, `error_code_map.json`,
`guardrails.json` — each hashed over **newline-normalized, BOM-stripped UTF-8
content, not raw bytes** (FIX 9: git `core.autocrlf` makes raw bytes
platform-dependent). It does **not** record a commit sha (FIX 8): the git
commit that carries the artifact is its commit provenance.

**Gate B evidence:** STEP 1 run three times (once to the committed path, twice
to gitignored scratch) → all three byte-identical, and the seeded bootstrap
(FIX 5) is inside that determinism. Successive frozen shas as the corrections
landed: first draft `7dc982ff…` → FIX 1–4 `40c139da…` → FIX 5–6 `2c6c2d27…` →
FIX 7 `6cf58521…` → FIX 8 `24bbd765…` → FIX 9 **STEP 1
`604ca17d59f526619938d056c28a4060952131f2b773e8e36d0d9758bce6f068`**, **STEP 2
`579c2c51452268ce053df4b232a840e3abed04b4617a68287a5f57782d5d3562`** (FIX 9
renames the five sha fields to `_normalized` and recomputes them over
newline-normalized content, so both artifact shas move). Only the `.meta.json`
sibling varies between runs.

### Headline numbers (full corpus, locked 70:30) — corrected set

| # | Headline | Value |
|---|----------|-------|
| 1 | Incremental uplift, **count basis** | 9.91 pp (37.74% − 27.84%), ≈ 112 extra recoveries; Wilson [4.91, 14.67] pp |
| 2 | 95% CI on uplift (Wilson-on-difference) | [4.91, 14.67] pp, width 9.76 pp |
| 3 | Arm sizes | 1,126 treatment / 485 control of 1,611 (0 guardrail-blocked) |
| 4 | **Distinct** actions (unique `payment_id` per rung) | 1,126 email nudges (1,388 across events); 0 EV-floor skips, 0 routed, 0 suppressed |
| 5 | **Incremental** recovered value (value-weighted, counterfactual-subtracted) | ₹60,528.90 (₹53.76 per treated customer); 95% bootstrap **[−₹17,598, ₹136,475]** |
| 6 | **Net incremental EV** (row 5 − ₹112.60 distinct action cost) | ₹60,416.30; 95% bootstrap **[−₹17,710, ₹136,363]** |

**Row 6's lower bound is below zero.** The count-basis uplift (row 1) is
significant; the value-weighted EV is **not distinguishable from zero at 95%**
on this corpus. The ₹ point estimates are directional, not bankable — see FIX 5
and the corrected FIX 6 reconciliation below.

Rows 5–6 are both on the treated-customer basis (1,126). Every treatment ACT
lands on the `email` rung and all seven guardrails pass, so the guardrail
walk-down ran for real on all 1,126 treatment customers and was **inert**
(`n_treatment_blocked_customers` = 0). `render_readme_numbers.py` renders these
six plus the `value_vs_count_note` and `policy_selectivity_note` (verbatim from
the artifact) into README.md between the generated-numbers markers; `--check`
re-derives them and exits non-zero on any drift.

### Corrections before freeze — the first frozen draft was wrong on the headline

The first draft (sha `7dc982ff…`) quoted **`net_ev_realised_inr` = ₹479,695.20**
as the headline EV. That figure summed recovered value across **both arms**,
control included, and subtracted only action cost — it counted self-recovery
the policy did not cause. That is the raw-recovery fallacy this project exists
to refute, quoted as if it were an uplift number. Nine fixes across five review
rounds (1–4, then 5–6, then 7, then 8 and 9 after two successive pushes failed
CI), applied and re-frozen:

1. **Net EV demoted.** `net_ev_realised_inr` → `gross_recovered_value_all_arms_inr`
   (value unchanged, ₹479,695.20) with a warning sibling string that it is NOT
   an uplift figure and must not be quoted as one; it does **not** appear in
   the README. The headline is now **`net_incremental_ev_inr` = ₹60,416.30** =
   `incremental_recovered_value_inr` (treatment recovered value − control
   recovered-value-per-capita × treatment headcount = ₹60,528.90) − distinct
   action cost ₹112.60. `incremental_value_per_treated_customer_inr` = ₹53.76.

2. **Count basis vs value basis surfaced.** `incremental_recoveries_count`
   = 425 − 0.2784 × 1126 = **111.6** extra recoveries. At the pooled mean
   recovered ticket (₹857) that implies ₹95,605 of value; the value-based
   figure is ₹60,529 — **63%** of it, a ₹35,076 gap. `ticket_balance_check`
   shows the arms are balanced on ticket (mean ₹877.92 treatment vs ₹859.03
   control, **+2.2%**; assignment is a pure sha256 on `customer_id`), so the
   gap is **not** arm imbalance. The nudge's incremental band does skew to
   smaller tickets (incremental-band mean ₹786 vs population ₹872, ratio 0.90;
   raw self-recovery probability has ~no linear ticket correlation, Pearson
   −0.01, so it is *lift* not baseline recovery that concentrates in small
   tickets) — **but** (corrected under FIX 6) that skew explains only ₹7,893 of
   the ₹35,076 gap. The residual ₹27,182 is not a mechanism; the bootstrap
   (FIX 5) shows it is sampling noise. See FIX 6.

3. **Distinct vs event action counts.** `process_failure` is idempotent per
   `payment_id`, so the 1,388 email *events* are **1,126** distinct sends.
   `distinct_actions_by_rung` and a DISTINCT-basis `total_action_cost_inr`
   (₹112.60) are new; the event-weighted figures stay as
   `action_counts_by_rung` / `total_action_cost_events_inr` (₹138.80) for
   comparison. README quotes the distinct count.

4. **EV gate never fires on this corpus — recorded.** New top-level
   `policy_selectivity_note`: zero `EV_BELOW_FLOOR`, zero `ROUTE_TO_HUMAN`,
   zero guardrail blocks, one rung across 2,000 events, so the policy is
   behaviourally identical to `recover_everything` here. Reason: email costs
   ₹0.10; EV = p_incremental_effective × ticket − cost clears the ₹2.00 floor
   for every event. The rules classifier emits only two causes —
   `bank_downtime` (break-even ticket **₹47.25**, solved from the engine's own
   arithmetic) and `insufficient_funds` (**₹39.44**). The smallest ticket in
   the corpus is **₹49.00**, above both — the tightest margin is **₹1.75**
   (bank_downtime). Phone coverage is **0%**, so the sms / whatsapp /
   agent_call rungs are unreachable and the ladder collapses to email-only;
   the higher-cost rungs where the floor and the human-review route bind
   cannot be exercised by this corpus. Slice 8's live webhook *did* produce
   SKIP / `EV_BELOW_FLOOR` on a ₹10 ticket (`pay_TW67GAczusj3yl`, EV ₹0.46 vs
   the ₹2.00 floor) — the standing evidence the gate works.

5. **`net_incremental_ev_inr` now carries an error bar.** It is the headline
   money figure and was quoted bare while the count-basis uplift had a Wilson
   interval — an asymmetry. Per-customer recovered value is zero-inflated and
   heavy-tailed, so the interval is a **seeded stratified percentile
   bootstrap**, not a normal approximation: 10,000 resamples, treatment
   (n=1126) and control (n=485) resampled independently, with replacement, at
   observed n; per-resample RNG seeded `sha256("bootstrap:{seed}:{i}")` — a
   **third** namespace, non-overlapping with `assign:` and the bare
   `{seed}:{cid}` resolve draw (recorded in `provenance.bootstrap_seed_basis`);
   2.5 / 97.5 linear-interpolated percentiles. Deterministic — Gate B stayed
   three-way byte-identical with the bootstrap in. New `customer_level` fields:
   `incremental_value_per_treated_customer_ci_95` **[−₹15.63, ₹121.20]**,
   `incremental_recovered_value_ci_95` **[−₹17,598, ₹136,475]**,
   `net_incremental_ev_ci_95` **[−₹17,710, ₹136,363]**. **The lower bound is
   below zero: the value-weighted incremental EV is not distinguishable from
   zero at 95% on this corpus.** Stated plainly in `value_vs_count_note` and in
   the README generated block, not buried.

6. **`value_vs_count_note` no longer overclaims the mechanism.** The earlier
   note pinned the whole 37% count-vs-value shortfall on the small-ticket skew
   of the incremental band, but the magnitudes do not support that:
   `implied_mean_ticket_per_incremental_recovery_inr` = ₹60,528.90 / 111.577 =
   **₹542**, whereas `mean_ticket_incremental_band_inr` = ₹786 — the 0.90 band
   ratio only accounts for `band_explained_shortfall_inr` = 111.577 × (857 −
   786) = **₹7,893** of the **₹35,076** gap. The
   `residual_unexplained_shortfall_inr` = **₹27,182** is not a mechanism: the
   FIX 5 bootstrap interval on incremental recovered value ([−₹17,598,
   ₹136,475]) **contains** the count-implied ₹95,605, so the count and value
   estimates are **not statistically distinguishable at n=485 control** — the
   apparent gap is consistent with sampling noise in a heavy-tailed,
   zero-inflated value estimator. The note now states, in order: both
   estimates and the gap; arms balanced (+2.2%), so not arm imbalance; band
   skew real but explains only ₹7,893; residual ₹27,182 and the interval
   containing the count figure ⇒ underpowered on value at n=485 control;
   headline is `net_incremental_ev_inr` with its bootstrap interval, count-basis
   uplift reported beside it. No mechanism is claimed that the interval cannot
   separate from noise.

7. **The uplift is a property of the generator — said so, above the numbers.**
   Outcomes come from `eval/environment.py` resolving the latent parameters our
   own `datagen.py` wrote into `data/ground_truth.json`; they are not observed
   from a payment processor. New top-level `synthetic_provenance_note` (rendered
   into the README block **above** the headline table, as a blockquote, so the
   caveat is read first) states: (a) outcomes are resolved, not observed; (b)
   this run validates the **measurement apparatus** — blind arm assignment on
   `customer_id`, counterfactual subtraction, attribution window, interval
   estimation — not real-world recovery; (c) the one real-world datapoint is
   Slice 8's live Razorpay webhook (`insufficient_funds`, ₹10 ticket, best rung
   sms, terminal SKIP / `EV_BELOW_FLOOR`); (d) what would make it a real claim —
   the same pipeline on live webhooks with an observed recovery feed and a
   control arm large enough to bound the value-weighted estimate. From the
   bootstrap: the `net_incremental_ev_ci_95` lower bound is −₹17,710 at
   n_control = 485; SE ∝ 1/√n, and pushing the lower bound above zero at the
   observed effect size needs SE to shrink ~1.3×, i.e. n_control on the order of
   **~800** (≈2× today's 485) — a few thousand customers total at 70:30, **order
   10³** — and that is a floor (assumes the effect size holds and the tail does
   not worsen).

8. **`head_commit_sha` removed from the provenance block.** The first push
   (`af6ae4e`) failed CI. `provenance.head_commit_sha` was
   `70e54057…` — the commit *before* Slice 11 — because the artifact was frozen
   before it was committed. CI checks out the pushed commit, re-runs, writes
   the pushed sha into that field, and the re-run-and-diff fails on that one
   line. It is a fixed point: recording HEAD inside a file you are about to
   commit is unsatisfiable, since writing the file moves HEAD. Fix: **delete
   the field** (and the now-dead `git_head()` / `subprocess` import). Not: an
   `--ignore` flag, a diff exclusion, or a non-blocking step. The commit that
   carries `results/final_run.json` *is* its commit provenance —
   `git log --oneline -- results/final_run.json` answers the question the field
   was trying to answer, and answers it correctly. The five input sha256s
   (`events`, `ground_truth`, `decision_policy`, `error_code_map`,
   `guardrails`) are what actually pin the run and are not self-referential;
   they stay. Same check applied to `results/split_comparison_500.json` — it
   used the same `provenance()` helper, so the one deletion fixes both.

9. **Input hashes are over normalized content, not raw bytes.** The second push
   failed CI: local sha256 of `rules/error_code_map.json` = `eea8b90f…`, the
   Linux runner's = `b756bb05…`, `git status` clean, `core.autocrlf = true`.
   Git stores LF and checks out CRLF on Windows, so `provenance` was hashing
   the raw bytes of a text file whose bytes are platform-dependent — every
   text-file sha in the block was only valid on the OS that computed it.
   `events.json` / `ground_truth.json` matched by luck (CI regenerates them
   locally with LF); `error_code_map.json` is git-tracked and did not. Fix, in
   `run_corpus.py`: `sha256_text_normalized()` — decode UTF-8, `\r\n` and bare
   `\r` → `\n`, strip a BOM, re-encode UTF-8, sha256 that. Applied to all five
   inputs. Fields **renamed** `sha256_*_json` → `sha256_*_json_normalized` so a
   normalized hash is never diffed against an old raw one by mistake; a
   `hash_basis` string in `provenance` explains why. A `crlf_selfcheck()` prints
   one line to **stdout** (never into the artifact) for any input whose raw and
   normalized hashes differ — on this Windows checkout it fires for
   `error_code_map.json`, making the platform gap visible instead of silent.
   `.gitattributes` at repo root (`* text=auto eol=lf`, `*.json text eol=lf`)
   is belt-and-braces; the normalized hash is the actual fix and does not
   depend on it being honoured. **The bug was only observable on a non-Windows
   runner — which is the argument for having CI at all.** Not done: excluding
   provenance from the CI diff, hashing on one platform only, or a non-blocking
   step.

### CI on the difference is Wilson, not Wald — and `eval.measurement` was NOT changed

The Slice 11 spec asks for **Wilson on the difference** for both artifacts.
`run_corpus.py` computes the Newcombe (1998) hybrid score interval — the two
single-proportion Wilson intervals combined into a difference interval — and
names it in the file (`"method": "Newcombe 1998 hybrid score (Wilson-based) on
the difference, 95%"`).

`eval.measurement._wald_ci` still computes a plain **Wald** interval on the
difference and is **left untouched** — changing it would move numbers baked
into earlier slices' committed outputs and tests. The two disagree, as
expected, on the full run:

| Interval | 95% CI on uplift |
|---|---|
| Wald (`eval.measurement`, unchanged) | [5.02, 14.80] pp |
| Wilson / Newcombe (`run_corpus.py`, this slice) | [4.91, 14.67] pp |

Both exclude zero; Wilson is the slightly tighter, boundary-respecting one and
is what the artifacts and README report. On the 90:10 sensitivity split the
Wilson lower bound crosses zero ([−0.90, 26.61] pp) — the 43-customer control
arm can no longer separate the uplift from noise; that is the whole point of
the sweep and `70:30` is kept.

### Tests

**229, unchanged** (same suite as Slice 10). This slice adds only offline
tooling under `scripts/` and a CI workflow — no `app/` behaviour changed, so no
new unit tests. `run_corpus.py` instead self-checks at runtime: STEP 1 asserts
every ingested payment has a decision row and that the engine's own
`_compute_ladder` reproduces each persisted best-rung EV; STEP 2 asserts each
swept split matches `eval.measurement.run_policy` exactly.

`.github/workflows/ci.yml` (`test-and-freeze`): regenerate `data/` from the
seed (it is gitignored), run the full suite, then the **re-run-and-diff break
condition** — a fresh `run_corpus.py final` / `split` must reproduce the two
committed artifacts byte-for-byte (`diff -u`), so any drift in datagen, the
pipeline, or the engine fails the build — and finally
`render_readme_numbers.py --check`.

### Status

Two frozen artifacts, corrected and re-frozen five times (FIX 1–4, FIX 5–6,
FIX 7, then FIX 8 and FIX 9 after two successive pushes failed CI — a
self-referential commit sha, then platform-dependent raw-byte input hashes).
Final shas: STEP 1
`604ca17d59f526619938d056c28a4060952131f2b773e8e36d0d9758bce6f068`, STEP 2
`579c2c51452268ce053df4b232a840e3abed04b4617a68287a5f57782d5d3562`;
byte-identical across three runs each (seeded bootstrap included), and — after
FIX 9 — across OS/line-ending too. README
generated block regenerated and `--check`-clean. Full suite **229 passed**. The
re-run-and-diff break condition, `render_readme_numbers.py --check`, and
`pytest` all exit 0.

Bottom line on the numbers: the **count-basis** uplift is solid — 9.91 pp,
Wilson [4.91, 14.67] pp, ~112 extra recoveries. The **value-weighted**
incremental EV (₹60,416) has a bootstrap 95% interval of [−₹17,710, ₹136,363]
and is **not distinguishable from zero** at n=485 control; it is reported with
that interval, never bare.

Not committed in this session (no-commit mode in force): the corrected files,
diffs, test run and exit codes were produced and nothing was committed. The
first frozen draft's gross-as-net-EV error and the FIX 6 over-claim are both
recorded above, not rewritten away. Commit message pending separate approval.
