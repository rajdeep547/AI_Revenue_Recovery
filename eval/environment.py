"""Slice 3 follow-up · outcome resolution for the offline eval harness.

NOT part of the pipeline. This module may read ``ground_truth.json``; nothing
under ``app/`` may import anything from ``eval/``.

``datagen.py`` stops at failures — it no longer decides whether a payment is
recovered. That decision depends on the action a policy takes, so it lives
here: given a customer and that action, :meth:`Environment.resolve` says whether
the payment came back.

    env = Environment("data/ground_truth.json")
    env.resolve("cust_00042", "nudge")   # -> True / False
    env.resolve("cust_00042", "none")    # -> True / False

The coin for a customer is derived by hashing ``"{run_seed}:{customer_id}"`` —
NOT drawn from a shared RNG stream. Consequences:

* Same customer + same action always returns the same result, no matter the
  call order or how many other customers were resolved first. Policy
  comparisons are therefore *paired*, and replays stay byte-identical.
* The same uniform draw ``u`` backs both actions, compared against
  ``p_would_pay_anyway`` for ``"none"`` and ``p_pay_if_nudged`` for ``"nudge"``.
  Because ``p_pay_if_nudged >= p_would_pay_anyway``, a customer who self-recovers
  also recovers when nudged — a clean monotone coupling, no defiers — and across
  the population ``P(resolve|nudge) - P(resolve|none)`` equals the mean lift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_ACTIONS = ("none", "nudge")
_THRESHOLD_FIELD = {"none": "p_would_pay_anyway", "nudge": "p_pay_if_nudged"}
_TWO53 = float(1 << 53)


def _uniform(run_seed: int, customer_id: str) -> float:
    """A deterministic uniform draw in [0, 1) for one customer, with no
    dependence on call order or any shared stream."""
    digest = hashlib.sha256(f"{run_seed}:{customer_id}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / _TWO53


class Environment:
    """Resolves policy actions into recovered / not-recovered outcomes against a
    fixed ``ground_truth.json``."""

    def __init__(self, ground_truth: dict | str | Path, run_seed: int | None = None):
        if isinstance(ground_truth, (str, Path)):
            with open(ground_truth, encoding="utf-8") as fh:
                ground_truth = json.load(fh)
        self._customers: dict[str, dict] = ground_truth["customers"]
        self._run_seed = (
            run_seed if run_seed is not None else ground_truth["meta"]["seed"]
        )

    @property
    def run_seed(self) -> int:
        return self._run_seed

    def customer_ids(self) -> list[str]:
        return list(self._customers)

    def resolve(self, customer_id: str, action: str) -> bool:
        """Did this customer's payment come back, given ``action``
        (``"none"`` or ``"nudge"``)?"""
        if action not in _ACTIONS:
            raise ValueError(f"action must be one of {_ACTIONS}, got {action!r}")
        try:
            customer = self._customers[customer_id]
        except KeyError:
            raise KeyError(f"unknown customer_id {customer_id!r}") from None
        threshold = customer[_THRESHOLD_FIELD[action]]
        return _uniform(self._run_seed, customer_id) < threshold


def resolve(
    customer_id: str,
    action: str,
    ground_truth: dict | str | Path,
    run_seed: int | None = None,
) -> bool:
    """One-shot form of :meth:`Environment.resolve`."""
    return Environment(ground_truth, run_seed).resolve(customer_id, action)
