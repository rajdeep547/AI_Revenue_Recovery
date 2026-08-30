"""Slice 5 · Measurement — the trust anchor.

NOT part of the pipeline (see ``eval/__init__.py``): this module may read
``ground_truth.json`` via :mod:`eval.environment`; nothing under ``app/`` may
import anything from here.

Answers one question: *did a policy cause more recoveries than doing
nothing?* Three pieces:

1. **Assignment** — every customer is deterministically hashed into
   ``"treatment"`` (70%) or ``"control"`` (30%), keyed on ``customer_id`` +
   ``run_seed``. No ``random()`` at runtime: same seed + same customer_id =
   same arm, forever, regardless of process, event order, or how many times
   that customer shows up in the event stream.
2. **Outcome** — control customers are always resolved under action
   ``"none"`` (the untouched baseline); treatment customers are resolved
   under whatever action ``policy(row)`` returns. Resolution itself is
   :func:`eval.environment.Environment.resolve`.
3. **Uplift** — treatment recovery rate minus control recovery rate, with a
   95% Wald (normal-approximation) confidence interval. Wald is justified
   here rather than a bootstrap because arm sizes run in the hundreds to low
   thousands and recovery rates sit well away from 0/1 — the binomial-
   proportion CLT already holds comfortably at that scale (the same normal
   approximation backs the power calculation in DECISIONS.md); a bootstrap
   would tell the same story for materially more compute.

Run over the real dataset::

    python -m eval.measurement --events data/events.json --ground-truth data/ground_truth.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.arms import (  # single implementation - see app/arms.py
    TREATMENT_FRACTION,
    _uniform_assign,
    assign_arm,
    assign_arms,
)
from app.ingest import Ingestor
from eval.environment import Environment

_Z_95 = 1.959963984540054  # scipy.stats.norm.ppf(0.975)

Action = str  # "none" | "nudge"
Policy = Callable[[dict], Action]

# Arm-assignment helpers (`_uniform_assign`, `assign_arm`, `assign_arms`,
# `TREATMENT_FRACTION`) moved to `app.arms` so the runtime pipeline can
# assign arms without any import route from app/ into eval/. They are
# re-exported here unchanged, so `eval.measurement.assign_arm(...)` still
# works and produces byte-identical results.


# ---------------------------------------------------------------------- policies


def do_nothing_policy(row: dict) -> Action:
    return "none"


def recover_everything_policy(row: dict) -> Action:
    return "nudge"


# error_reason buckets datagen built with low base / high lift (see
# DECISIONS.md) -- the ones where a nudge does the most good per send.
_HIGH_LIFT_REASONS = {"expired_card", "invalid_card"}


def targeted_card_policy(row: dict) -> Action:
    """A plausible real policy: nudge only the failure reasons with high
    incremental lift. Reads only `reason`, a normalized field that already
    flows through app/ingest -- never ground_truth."""
    return "nudge" if row.get("reason") in _HIGH_LIFT_REASONS else "none"


POLICIES: dict[str, Policy] = {
    "do_nothing": do_nothing_policy,
    "recover_everything": recover_everything_policy,
    "targeted_card": targeted_card_policy,
}


# ------------------------------------------------------------------ population


class DuplicateCustomerRows(RuntimeError):
    """Raised by :func:`load_population` when a customer_id appears in more
    than one normalized row.

    Everything downstream treats one row == one independent observation, and
    arm assignment is customer-level (:func:`assign_arm` hashes only
    ``customer_id``). If a customer ever carries two distinct payment
    references, Ingestor's ``source:reference`` dedupe -- which today
    collapses a customer's events for free only because datagen reuses one
    payment id per customer -- no longer collapses them, both rows land in
    the SAME arm (assignment doesn't care), and the recovery-rate
    denominator silently becomes event-level while the Wald CI is computed
    as if every observation were independent, narrowing it on correlated
    data. This is a contract, not an accident: fail loudly instead. A plain
    ``assert`` won't do -- asserts vanish under ``-O``, so this is a raise.
    """

    def __init__(self, counts: dict[str, int]):
        self.duplicate_counts = dict(counts)
        self.duplicate_customer_ids = sorted(counts)
        detail = ", ".join(f"{cid}×{n}" for cid, n in sorted(counts.items()))
        super().__init__(
            "load_population invariant violated: customer-level assignment "
            "requires a customer-level denominator, and these customer_ids "
            f"have more than one row (invalidates the Wald CI): {detail}"
        )


def load_population(events_path: str | Path) -> tuple[list[dict], dict]:
    """Ingest events.json through the real card_failure adapter + Ingestor
    (dedupe included) and return ``(rows, stats)``. This is the Slice 5
    prerequisite: measurement runs over normalized rows, never raw JSON.

    datagen gives every event for one customer the same payment reference,
    so Ingestor's ``source:reference`` dedupe already collapses a customer's
    failure + retry + noise events into one row -- ``rows()`` is naturally
    one entry per customer. That is an assumption, not a guarantee, so it is
    checked explicitly: see :class:`DuplicateCustomerRows`.

    Events the adapter can't reach a customer through (e.g. no email/phone,
    ``no_contact_channel``) are rejected by ``Ingestor`` and are excluded
    from the returned population -- they never enter either arm's
    denominator. ``stats["rejected_by_reason"]`` is the count-by-reason for
    triage; nothing is silently dropped.
    """
    with open(events_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    ingestor = Ingestor(":memory:")
    for ev in doc["events"]:
        payload = {**ev["payload"], "created_at": ev["created_at"]}
        ingestor.ingest("card_failure", payload)
    rows = ingestor.rows()
    stats = ingestor.stats()
    ingestor.close()

    counts = Counter(r["customer_id"] for r in rows)
    duplicates = {cid: n for cid, n in counts.items() if n > 1}
    if duplicates:
        raise DuplicateCustomerRows(duplicates)

    return rows, stats


# ------------------------------------------------------------------- measurement


@dataclass(frozen=True)
class ArmResult:
    arm: str
    n: int
    n_recovered: int

    @property
    def rate(self) -> float:
        return self.n_recovered / self.n if self.n else float("nan")


@dataclass(frozen=True)
class UpliftResult:
    policy: str
    treatment: ArmResult
    control: ArmResult
    uplift: float
    ci_low: float
    ci_high: float


def _wald_ci(treatment: ArmResult, control: ArmResult, z: float = _Z_95) -> tuple[float, float]:
    if treatment.n == 0 or control.n == 0:
        return float("nan"), float("nan")
    p_t, n_t = treatment.rate, treatment.n
    p_c, n_c = control.rate, control.n
    se = math.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)
    uplift = p_t - p_c
    return uplift - z * se, uplift + z * se


def run_policy(
    rows: Iterable[dict],
    ground_truth,
    policy: Policy,
    policy_name: str = "",
    run_seed: int | None = None,
    treatment_fraction: float = TREATMENT_FRACTION,
) -> UpliftResult:
    """Measure one policy's uplift over ``rows`` (normalized customer rows,
    e.g. from :func:`load_population`).

    Control is the untouched baseline: every control customer resolves under
    action "none" no matter what ``policy`` says -- policy only governs
    customers who land in treatment. That pairing is what makes "uplift"
    meaningful: treatment-under-policy vs. the same population's
    counterfactual do-nothing rate, not two arbitrary groups.
    """
    env = Environment(ground_truth, run_seed=run_seed)
    seed = env.run_seed
    control_outcomes: list[bool] = []
    treatment_outcomes: list[bool] = []
    for row in rows:
        cid = row["customer_id"]
        if assign_arm(seed, cid, treatment_fraction) == "control":
            control_outcomes.append(env.resolve(cid, "none"))
        else:
            treatment_outcomes.append(env.resolve(cid, policy(row)))

    control = ArmResult("control", len(control_outcomes), sum(control_outcomes))
    treatment = ArmResult("treatment", len(treatment_outcomes), sum(treatment_outcomes))
    uplift = treatment.rate - control.rate
    ci_low, ci_high = _wald_ci(treatment, control)
    return UpliftResult(policy_name, treatment, control, uplift, ci_low, ci_high)


def _fmt(r: UpliftResult) -> str:
    return (
        f"{r.policy:<20} uplift {r.uplift:+.4f}  95% CI [{r.ci_low:+.4f}, {r.ci_high:+.4f}]  "
        f"treatment n={r.treatment.n} rate={r.treatment.rate:.4f}  "
        f"control n={r.control.n} rate={r.control.rate:.4f}"
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Slice 5 measurement report")
    ap.add_argument("--events", default="data/events.json")
    ap.add_argument("--ground-truth", default="data/ground_truth.json")
    ap.add_argument("--seed", type=int, default=None, help="defaults to ground_truth.meta.seed")
    args = ap.parse_args(argv)

    rows, stats = load_population(args.events)
    with open(args.ground_truth, encoding="utf-8") as fh:
        ground_truth = json.load(fh)
    seed = args.seed if args.seed is not None else ground_truth["meta"]["seed"]

    unique_customers = {r["customer_id"] for r in rows}
    print(f"ingest stats           {stats}")
    print(f"population size        {len(rows)}")
    print(f"unique customer_ids    {len(unique_customers)}  (must be equal -- enforced by load_population)")

    arms = assign_arms((r["customer_id"] for r in rows), seed)
    n_treat = sum(1 for a in arms.values() if a == "treatment")
    n_ctrl = len(arms) - n_treat
    print(
        f"split                  treatment={n_treat} ({n_treat / len(arms):.3f})  "
        f"control={n_ctrl} ({n_ctrl / len(arms):.3f})"
    )

    customers = ground_truth["customers"]
    treatment_ids = [cid for cid, arm in arms.items() if arm == "treatment"]
    population_mean_lift = sum(c["lift"] for c in customers.values()) / len(customers)
    treatment_mean_lift = sum(customers[cid]["lift"] for cid in treatment_ids) / len(treatment_ids)
    print(
        f"mean lift              population={population_mean_lift:.4f}  "
        f"treatment-arm={treatment_mean_lift:.4f}  "
        f"drift={treatment_mean_lift - population_mean_lift:+.4f}"
    )
    print()
    for name, policy in POLICIES.items():
        result = run_policy(rows, ground_truth, policy, policy_name=name, run_seed=seed)
        print(_fmt(result))


if __name__ == "__main__":
    main()
