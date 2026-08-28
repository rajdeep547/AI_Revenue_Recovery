"""Slice 3 · Deterministic synthetic data generator.

Produces a fixed-size stream of Razorpay-shaped payment webhook events plus a
*held-out* ground-truth file of per-customer counterfactual labels.

    python datagen.py --seed 20260826 --events 2000 --out-dir data
    python datagen.py --hist        # print a ticket-size histogram, write nothing

Two outputs land in ``--out-dir``:

* ``events.json`` — everything the pipeline (``app/``) is allowed to see:
  ``payment.failed`` deliveries plus a little ``payment.authorized`` noise that
  never captures, a small cohort per customer, shaped like a real webhook body.
  Every failure carries an ``error_reason`` (a permitted Slice 7 classifier
  feature). No captures, no arm, no probabilities — whether a payment is ever
  recovered is not decided here.
* ``ground_truth.json`` — the counterfactual, per customer: ``error_reason``,
  ``p_would_pay_anyway`` (recovers with no nudge), ``p_pay_if_nudged`` (recovers
  with a nudge; always >= the former), the derived ``lift`` between them, plus
  ``amount`` / ``method``. **No module under app/ may import or read it** —
  tests / evaluation only. Resolving a policy's actions into recovered / not
  outcomes lives in ``eval/environment.py``.

Determinism: every random draw comes from one ``random.Random(seed)`` in a fixed
order, every timestamp is derived from a constant epoch (never the wall clock),
and the JSON is written sorted, ASCII, with ``\n`` newlines. Same seed in →
byte-identical files out.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random

DEFAULT_SEED = 20260826
DEFAULT_N_EVENTS = 2000

# 2025-01-01T00:00:00Z. All created_at values are EPOCH + deterministic offsets,
# so the output never depends on when the generator was run.
EPOCH = 1_735_689_600

# Ticket size, in paise. Log-normal: a dense pile of small tickets with a long,
# fat tail — not a flat uniform band. median ~= e**mu ~= Rs 550.
_TICKET_MU = 10.9151
_TICKET_SIGMA = 0.95
_TICKET_MIN = 4_900        # Rs 49
_TICKET_MAX = 5_000_000    # Rs 50,000

_METHODS = ("upi", "card", "netbanking", "wallet")
_METHOD_WEIGHTS = (0.46, 0.38, 0.11, 0.05)

# --- Slice 3 amendment: the counterfactual half ------------------------------
#
# error_reason is the spine of the synthetic world. It is the ONLY part of the
# counterfactual the pipeline may see: it lands in events.json and is the
# Slice 7 classifier's input feature. The arm and both probabilities never
# leave ground_truth.json.
#
# Mix weights — a plausible failed-payment breakdown for an Indian D2C checkout
# (UPI-heavy, real 3DS / UPI-PIN friction, frequent issuer/UPI outages):
#   insufficient_funds  0.28   hard declines, the single largest bucket
#   otp_timeout         0.22   3DS / UPI-PIN abandonment
#   bank_downtime       0.18   issuer or UPI network outage
#   gateway_timeout     0.14   PA/PG timed out before confirmation
#   expired_card        0.10
#   invalid_card        0.08   wrong number/CVV, stale saved card
_ERROR_REASONS = (
    "bank_downtime",
    "gateway_timeout",
    "expired_card",
    "invalid_card",
    "insufficient_funds",
    "otp_timeout",
)
_ERROR_REASON_WEIGHTS = (0.18, 0.14, 0.10, 0.08, 0.28, 0.22)

# Razorpay-style (error_code, error_description) derived from error_reason, so
# the webhook body keeps the shape Slices 1-2 already parse.
_REASON_DETAIL = {
    "bank_downtime": ("GATEWAY_ERROR", "issuer or UPI bank temporarily unavailable"),
    "gateway_timeout": ("GATEWAY_ERROR", "gateway timed out before confirmation"),
    "expired_card": ("BAD_REQUEST_ERROR", "card has expired"),
    "invalid_card": ("BAD_REQUEST_ERROR", "card number or CVV is invalid"),
    "insufficient_funds": ("BAD_REQUEST_ERROR", "payment failed due to insufficient funds"),
    "otp_timeout": ("BAD_REQUEST_ERROR", "3DS/OTP was not completed in time"),
}

# Per-error_reason Beta calibration as (mean, concentration) — once for the base
# probability p_would_pay_anyway, independently once for the incremental lift.
# alpha = mean * kappa, beta = (1 - mean) * kappa; higher kappa => tighter
# within-bucket spread (kept loose enough that no bucket is a point mass).
#
# The base/lift anticorrelation is DELIBERATE (see DECISIONS.md): transient
# infrastructure failures self-heal on the customer's own retry -> high base,
# ~zero lift; card problems never self-heal but one nudge fixes them -> low
# base, high lift; insufficient_funds is low/low; otp_timeout is mid/mid.
#                        base_mean base_k   lift_mean lift_k
_REASON_BETAS = {
    "bank_downtime":      (0.52,    16.0,    0.02,     14.0),
    "gateway_timeout":    (0.48,    16.0,    0.03,     14.0),
    "expired_card":       (0.12,    16.0,    0.22,     14.0),
    "invalid_card":       (0.10,    16.0,    0.24,     14.0),
    "insufficient_funds": (0.15,    16.0,    0.09,     14.0),
    "otp_timeout":        (0.30,    16.0,    0.12,     14.0),
}

_STATUS_FOR = {
    "payment.failed": "failed",
    "payment.captured": "captured",
    "payment.authorized": "authorized",
}


def _beta_params(mean: float, kappa: float) -> tuple[float, float]:
    return mean * kappa, (1.0 - mean) * kappa


def _ticket_amount(rng: random.Random) -> int:
    """One ticket size in paise: log-normal core, retail-style rounding."""
    raw = rng.lognormvariate(_TICKET_MU, _TICKET_SIGMA)
    raw = min(max(raw, _TICKET_MIN), _TICKET_MAX)
    rupees = raw / 100.0
    if rng.random() < 0.45:
        # charm pricing: 49, 99, 149, 199, 249, ... 2499, ...
        bucket = max(1, round(rupees / 50.0))
        amount = bucket * 5_000 - 100
    else:
        # rounder figures, coarser as the number grows: Rs 1 / Rs 10 / Rs 100
        step = 100 if rupees < 500 else 1_000 if rupees < 5_000 else 10_000
        amount = max(step, int(round(raw / step)) * step)
    return int(amount)


def _plan_events(rng: random.Random) -> list[str]:
    """The event sequence for one customer: failures only. Whether the payment
    is ever recovered depends on a downstream policy, not on this generator, so
    no captured event is emitted here. Always starts with a failure."""
    seq = ["payment.failed"]
    if rng.random() < 0.15:
        seq.append("payment.failed")            # a retry that also failed
    if rng.random() < 0.08:
        seq.append("payment.authorized")        # authorized, never captured — noise
    return seq


def generate_dataset(
    seed: int = DEFAULT_SEED, n_events: int = DEFAULT_N_EVENTS
) -> tuple[dict, dict]:
    """Return ``(events_doc, ground_truth_doc)`` for the given seed.

    Customers are generated whole, one after another, until exactly
    ``n_events`` events exist; the final customer's sequence is truncated to
    land on the target without ever overshooting.
    """
    rng = random.Random(seed)
    events: list[dict] = []
    customers: dict[str, dict] = {}
    clock = EPOCH
    cust_n = 0

    while len(events) < n_events:
        cust_n += 1
        remaining = n_events - len(events)
        customer_id = f"cust_{cust_n:05d}"
        payment_id = f"pay_{cust_n:06d}"

        # Fixed per-customer draw order — do not reorder, it defines the bytes:
        # error_reason, base prob, lift, ticket, method, event plan, gaps.
        error_reason = rng.choices(_ERROR_REASONS, _ERROR_REASON_WEIGHTS)[0]
        base_mean, base_k, lift_mean, lift_k = _REASON_BETAS[error_reason]

        base = round(rng.betavariate(*_beta_params(base_mean, base_k)), 6)
        raw_lift = rng.betavariate(*_beta_params(lift_mean, lift_k))
        p_nudged = round(min(1.0, base + raw_lift), 6)
        lift = round(p_nudged - base, 6)
        # Invariant: a nudge never hurts, and lift is exactly the gap.
        assert 0.0 <= base <= 1.0 and 0.0 <= p_nudged <= 1.0
        assert p_nudged >= base
        assert abs((base + lift) - p_nudged) < 1e-9

        amount = _ticket_amount(rng)
        method = rng.choices(_METHODS, _METHOD_WEIGHTS)[0]

        seq = _plan_events(rng)[:remaining]

        err_code, err_desc = _REASON_DETAIL[error_reason]
        for etype in seq:
            clock += 1 + min(7_200, int(rng.expovariate(1 / 900)))
            entity = {
                "id": payment_id,
                "amount": amount,
                "method": method,
                "status": _STATUS_FOR[etype],
            }
            if etype == "payment.failed":
                # error_reason is a permitted feature; error_code/description
                # keep the Razorpay body shape. No arm, no probabilities here.
                entity["error_reason"] = error_reason
                entity["error_code"] = err_code
                entity["error_description"] = err_desc
            events.append(
                {
                    "event_id": f"evt_{len(events) + 1:05d}",
                    "created_at": clock,
                    "payload": {
                        "event": etype,
                        "payload": {"payment": {"entity": entity}},
                    },
                }
            )

        customers[customer_id] = {
            "customer_id": customer_id,
            "payment_id": payment_id,
            "amount": amount,
            "method": method,
            "error_reason": error_reason,
            "p_would_pay_anyway": base,
            "p_pay_if_nudged": p_nudged,
            "lift": lift,
            "events_emitted": len(seq),
        }

    meta = {
        "seed": seed,
        "n_events": len(events),
        "n_customers": len(customers),
        "epoch": EPOCH,
    }
    events_doc = {"meta": meta, "events": events}
    ground_truth_doc = {
        "meta": {
            **meta,
            "note": (
                "Counterfactual labels for evaluation only. HELD OUT: no module "
                "under app/ may import or read this file."
            ),
        },
        "customers": customers,
    }
    return events_doc, ground_truth_doc


def _dump(path: str, doc: dict) -> None:
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_dataset(out_dir: str, events_doc: dict, ground_truth_doc: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    _dump(os.path.join(out_dir, "events.json"), events_doc)
    _dump(os.path.join(out_dir, "ground_truth.json"), ground_truth_doc)


def _percentile(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ks_stat_normal(values: list[float]) -> float:
    """One-sample KS distance between ``values`` and a normal fitted to them
    (mean/sd taken from the sample — Lilliefors style). Small => plausibly
    normal; for log(amount) that is the lognormal claim, made properly."""
    xs = sorted(values)
    n = len(xs)
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / n)
    if sd == 0:
        return 1.0
    d = 0.0
    for i, x in enumerate(xs):
        cdf = _norm_cdf((x - mu) / sd)
        d = max(d, cdf - i / n, (i + 1) / n - cdf)
    return d


def _summary(events_doc: dict, ground_truth_doc: dict) -> str:
    custs = list(ground_truth_doc["customers"].values())
    amounts = sorted(c["amount"] for c in custs)
    n = len(amounts)
    mean = sum(amounts) / n

    base = [c["p_would_pay_anyway"] for c in custs]
    nudged = [c["p_pay_if_nudged"] for c in custs]
    lift = [c["lift"] for c in custs]
    ks = _ks_stat_normal([math.log(a) for a in amounts])

    lines = [
        f"seed          {events_doc['meta']['seed']}",
        f"events        {events_doc['meta']['n_events']}",
        f"customers     {n}",
        "counterfactual (ground_truth.json):",
        f"  mean p_would_pay_anyway   {sum(base) / n:.4f}",
        f"  mean p_pay_if_nudged      {sum(nudged) / n:.4f}",
        f"  mean lift                 {sum(lift) / n:.4f}",
        f"  corr(base, lift)          {_pearson(base, lift):+.3f}",
        "ticket size (Rs):",
        f"  min {amounts[0] / 100:>10.0f}   median {_percentile(amounts, 0.50) / 100:>10.0f}"
        f"   mean {mean / 100:>10.0f}",
        f"  p90 {_percentile(amounts, 0.90) / 100:>10.0f}   p99 {_percentile(amounts, 0.99) / 100:>10.0f}"
        f"   max {amounts[-1] / 100:>10.0f}",
        f"  log(amount) KS vs fitted normal   {ks:.4f}",
    ]
    return "\n".join(lines)


def ascii_histogram(amounts_paise: list[int], bins: int = 24, width: int = 54) -> str:
    """Log-spaced histogram of ticket sizes — the honest way to eyeball a
    log-normal: a real merchant's takings, not a uniform slab."""
    rupees = sorted(a / 100.0 for a in amounts_paise)
    lo, hi = rupees[0], rupees[-1]
    counts = [0] * bins
    span = math.log(hi / lo) if hi > lo else 1.0
    for r in rupees:
        k = min(bins - 1, int(math.log(r / lo) / span * bins))
        counts[k] += 1
    peak = max(counts) or 1
    edges = [lo * (hi / lo) ** (i / bins) for i in range(bins + 1)]
    out = []
    for i in range(bins):
        bar = "#" * round(counts[i] / peak * width)
        out.append(f"Rs {edges[i]:>8.0f} - {edges[i + 1]:>8.0f} | {bar} {counts[i]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Slice 3 deterministic data generator")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--events", type=int, default=DEFAULT_N_EVENTS)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument(
        "--hist",
        action="store_true",
        help="print a ticket-size histogram and exit without writing files",
    )
    args = ap.parse_args(argv)

    events_doc, ground_truth_doc = generate_dataset(args.seed, args.events)
    amounts = [c["amount"] for c in ground_truth_doc["customers"].values()]

    print(_summary(events_doc, ground_truth_doc))
    if args.hist:
        print()
        print(ascii_histogram(amounts))
        return

    write_dataset(args.out_dir, events_doc, ground_truth_doc)
    print(f"wrote {args.out_dir}/events.json and {args.out_dir}/ground_truth.json")


if __name__ == "__main__":
    main()
