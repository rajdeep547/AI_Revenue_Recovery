# Decisions

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
