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

| target | value |
|---|---|
| mean `p_would_pay_anyway` ≈ 0.29 (baseline not moved) | **0.2892** |
| mean `lift` ∈ [0.08, 0.12] | **0.1026** |
| Pearson corr(base, lift) ≤ ≈ −0.4 | **−0.437** |
| control arm share ≈ 0.30 | **0.292** (371 / 1272) |

Observed realised recovery rate: control **0.286**, treated **0.398**
(diff **+11.3pp**).

### Arm assignment and power

`arm = "control" if rng.random() < 0.30 else "treated"`, from the same seeded
RNG. Control customers realise against `p_would_pay_anyway`, treated against
`p_pay_if_nudged`. Arm and both probabilities live **only** in
`ground_truth.json`; `events.json` leaks neither (only the observable
consequence — a `payment.captured` for those who recovered).

Target mean lift ≈ 0.10 was chosen for detectability. At the design scale of
2,000 customers → 600 control / 1,400 treated, with a baseline recovery rate
≈ 0.31, the SE of the control−treated difference in proportions is
`sqrt(0.31·0.69·(1/600 + 1/1400)) ≈ 0.0226` (**~2.2pp**). A true 10pp effect is
then ≈ **4.4σ** — Slice 5 can cleanly tell "measurement works" from
"measurement is broken", while the near-zero lift on the infra buckets means a
naïve estimator that ignores `error_reason` will *understate* or misattribute
the effect. At this seed's realised split (371 / 901) the SE is ≈ 2.97pp and
the observed 11.3pp difference is ≈ 3.8σ.

### New file hashes (SHA-256, default seed, `--events 2000`)

The draw order changed with this amendment, so the Slice 3 hashes are
superseded:

```
events.json        d0d39c07afa324eabe68fb03d67d53d256413f74394b98abdd9a4de1f066a08e
ground_truth.json  4999ccf56e4c24eaefff2f95b07954caf9646187a61022c6c5337113cb6fdd08
```
