"""Deterministic treatment / control assignment.

A pure sha256 helper: given ``(run_seed, customer_id)`` it returns a stable
uniform draw and an arm. No dependency on generated data, on the datagen
module, or on any offline harness -- which is why it lives under ``app/``.
The runtime pipeline (``app.pipeline``) assigns arms from here; the
measurement layer under ``eval/`` re-imports these names, so exactly one
implementation exists and the two paths cannot drift.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

_TWO53 = float(1 << 53)
TREATMENT_FRACTION = 0.7


def _uniform_assign(run_seed: int, customer_id: str) -> float:
    """Deterministic uniform draw in [0, 1) for arm assignment only.

    SPEC DEVIATION (approved): the brief specified hashing
    ``sha256(f"{run_seed}:{customer_id}")`` for assignment. This hashes
    ``f"assign:{run_seed}:{customer_id}"`` instead -- sharing the exact hash
    the outcome-resolution draw uses would tie a customer's arm to the same
    uniform value used to decide whether they self-recover under "none",
    biasing the control-arm recovery rate downward and inflating every
    measured uplift. See DECISIONS.md "Slice 5" for the full rationale.

    Salted ``"assign:"`` and kept in its own namespace, deliberately
    *distinct* from the outcome-resolution draw
    (``sha256(f"{seed}:{customer_id}")``). Reusing that exact hash here would
    make a customer's arm deterministically correlated with whether they
    self-recover under "none" (both would compare the very same u against
    unrelated thresholds), biasing the control-arm recovery rate away from
    the population baseline. A different salt keeps the two draws independent
    while both stay fully deterministic in (seed, customer_id).
    """
    digest = hashlib.sha256(f"assign:{run_seed}:{customer_id}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / _TWO53


def assign_arm(
    run_seed: int, customer_id: str, treatment_fraction: float = TREATMENT_FRACTION
) -> str:
    """"treatment" or "control" -- deterministic in (run_seed, customer_id)
    alone. Never call with an event_id: two events from the same customer
    must land in the same arm, which only holds if the hash input is the
    customer_id."""
    return "treatment" if _uniform_assign(run_seed, customer_id) < treatment_fraction else "control"


def assign_arms(
    customer_ids: Iterable[str],
    run_seed: int,
    treatment_fraction: float = TREATMENT_FRACTION,
) -> dict[str, str]:
    return {cid: assign_arm(run_seed, cid, treatment_fraction) for cid in customer_ids}
