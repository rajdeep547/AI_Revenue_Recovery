"""Slice 7 - the decision engine.

Given one diagnosed failure event, a frozen policy, the customer's
experiment arm and their contact history, decide whether to spend money on
a recovery nudge, which rung of the action ladder to use, or to skip / route
to a human -- and record exactly why, with every number reproducible from
the stored record.

Nothing here reads the counterfactual answer key; the engine sees only the
event, the policy file, the arm string and the history dict handed to it.
"""

from app.decision.engine import (
    Decision,
    MissingPrior,
    SkipReason,
    Terminal,
    decide,
    load_policy,
)
from app.decision.rationale import render

__all__ = [
    "Decision",
    "MissingPrior",
    "SkipReason",
    "Terminal",
    "decide",
    "load_policy",
    "render",
]
