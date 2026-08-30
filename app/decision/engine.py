"""Slice 7 - the decision engine.

``decide(event, policy, arm, history) -> Decision`` is a pure function: no
DB access, no clock reads (the caller passes ``now_utc`` inside ``event``),
no network. Same inputs + same policy => identical :class:`Decision` and
identical ``inputs_hash``.

What the engine reads
--------------------
``event`` (one diagnosed failure):

===================  ========================================================
``payment_id``       str
``cause``            str -- diagnosed root cause; MUST be a key in
                     ``policy["incremental_priors"]`` or :class:`MissingPrior`
                     is raised (an unknown cause is an upstream bug, not a
                     reason to guess).
``cause_confidence`` float in [0, 1]
``ticket_inr``       float gross ticket in INR. ``amount_paise`` (Slice 4
                     normalized) is accepted as a fallback and divided by 100.
``risk_blocked``     bool (default False) -- the risk engine blocked this
                     payment; no nudge is appropriate.
``already_recovered``bool (default False)
``email`` / ``phone``the Slice 4 normalized contact-channel fields; presence
                     is what a rung's ``requires_channel`` is checked against.
``now_utc``          ISO-8601 string -- supplied by the caller, never read
                     from the clock, so the function stays pure.
===================  ========================================================

``history`` (this customer, before now):

========================  ===================================================
``last_contact_at``       ISO-8601 string or None -- drives ``COOLDOWN``.
``prior_recoveries``      list of ``{"arm": "control" | "treatment"}``. A
                          recovery that happened while the customer was in
                          the CONTROL arm means they self-recovered with no
                          contact; step (d) then quarters ``p_effective``.
``already_recovered``     optional bool, same effect as the event flag.
========================  ===================================================

Evaluation order (load-bearing -- see DECISIONS.md "Slice 7"):

  a) hard gates, before ANY EV arithmetic and before the ladder is touched:
     RISK_BLOCKED, then ALREADY_RECOVERED, then NO_CONTACT_CHANNEL (only if
     no rung's channel requirement can be met), then COOLDOWN. PRIOR_ZERO
     (the cause's incremental prior is exactly 0) is also resolved here.
  b-f) compute ``p_effective``, the history adjustment, the ladder, the best
     rung and ``ev_lower_inr`` -- regardless of arm.
  g) control arm -> SKIP CONTROL_ARM, with ``shadow_action`` = the rung that
     would have fired in treatment (or None).
  h) best EV < ``min_ev_inr`` -> SKIP EV_BELOW_FLOOR.
  i) POLICY OVERRIDE (not an EV bound): the best rung is in
     ``high_touch_rungs`` AND ``ticket_inr >= human_review_ticket_inr`` AND
     ``cause_confidence < review_confidence_threshold`` -> ROUTE_TO_HUMAN.
     This is a standing rule about who may authorise an expensive action on
     a large, weakly-diagnosed ticket; it does not mean the EV is negative.
  j) otherwise ACT with the best rung.

``ev_lower_inr`` is a diagnostic only -- ``p_lower_bound_rung * ticket -
cost``, where ``p_lower_bound_rung = min(p_effective * max(confidence,
confidence_penalty_floor) * effectiveness, 0.95)``. It is recorded on every
post-ladder decision and gates nothing. (It previously subtracted
``population_incremental``; that subtracted a do-nothing baseline that
``p_incremental`` -- already lift over do-nothing -- never added.)

``gate_basis`` records WHY each decision landed where it did:
``expected_value`` (ACT / EV_BELOW_FLOOR), ``policy_override``
(ROUTE_TO_HUMAN), ``hard_gate`` (the pre-EV skips), ``experiment``
(CONTROL_ARM).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "decision_policy.json"

_P_RUNG_CAP = 0.95


class Terminal:
    """The three terminal states. Plain strings by design -- the brief pins
    the :class:`Decision` field to ``"ACT" | "SKIP" | "ROUTE_TO_HUMAN"``."""

    ACT = "ACT"
    SKIP = "SKIP"
    ROUTE_TO_HUMAN = "ROUTE_TO_HUMAN"


class SkipReason(enum.Enum):
    RISK_BLOCKED = "RISK_BLOCKED"
    NO_CONTACT_CHANNEL = "NO_CONTACT_CHANNEL"
    ALREADY_RECOVERED = "ALREADY_RECOVERED"
    COOLDOWN = "COOLDOWN"
    CONTROL_ARM = "CONTROL_ARM"
    EV_BELOW_FLOOR = "EV_BELOW_FLOOR"
    PRIOR_ZERO = "PRIOR_ZERO"


class MissingPrior(KeyError):
    """``event["cause"]`` has no entry in ``policy["incremental_priors"]``.

    A subclass of :class:`KeyError`. The prior table must be total over every
    cause the rules map (``rules/error_code_map.json``) can emit; a gap is a
    configuration bug and the engine refuses to guess a prior.
    """


@dataclasses.dataclass(frozen=True)
class Decision:
    """One decision, fully populated on every path.

    ``action`` SEMANTICS: on an ``ACT`` decision it is the rung that fired
    and money was authorised. On a ``ROUTE_TO_HUMAN`` decision it is the
    rung the engine PROPOSES, pending a person's authorisation -- no spend
    has been approved. So ``action is not None`` does NOT imply authorised
    spend. Consumers that execute a nudge must gate on
    ``terminal == Terminal.ACT``, never on ``action is not None``.
    """

    payment_id: str
    policy_version: str
    terminal: str                       # Terminal.ACT | .SKIP | .ROUTE_TO_HUMAN
    action: str | None                  # ACT: the rung that fired (spend authorised).
    #                                     ROUTE_TO_HUMAN: the PROPOSED rung, not authorised
    #                                     -- gate execution on terminal == Terminal.ACT.
    #                                     SKIP: None.
    skip_reason: SkipReason | None
    cause: str
    cause_confidence: float
    p_incremental_prior: float | None
    p_effective: float | None
    p_action_basis: float | None        # p_effective * effectiveness (capped 0.95); the
    #                                     rung-level probability ev_inr was computed from.
    #                                     Populated whenever a rung was chosen; None otherwise.
    p_lower_bound: float | None
    history_multiplier: float           # 1.0, or 0.25 after a control-arm self-recovery
    ticket_inr: float
    action_cost_inr: float | None
    ev_inr: float | None
    ev_lower_inr: float | None          # diagnostic only; gates nothing (see module docstring)
    shadow_action: str | None           # control-arm counterfactual rung
    gate_basis: str                     # expected_value | policy_override | hard_gate | experiment
    route_ticket_floor_inr: float | None    # policy_override only: human_review_ticket_inr in force
    route_confidence_ceiling: float | None  # policy_override only: review_confidence_threshold in force
    rationale: str
    inputs_hash: str

    # Fields beyond the brief's original 17 (`history_multiplier`,
    # `p_action_basis`, `gate_basis`, `route_ticket_floor_inr`,
    # `route_confidence_ceiling`) all exist for the same reason: the rationale
    # may cite only numbers that are on the record, and each of these is a
    # number (or basis) a required sentence has to state.

    # Column order for the flat `decisions` table (see app/decision/store.py).
    FLAT_FIELDS = (
        "payment_id", "policy_version", "terminal", "action", "skip_reason",
        "cause", "cause_confidence", "p_incremental_prior", "p_effective",
        "p_action_basis", "p_lower_bound", "history_multiplier", "ticket_inr",
        "action_cost_inr", "ev_inr", "ev_lower_inr", "shadow_action",
        "gate_basis", "route_ticket_floor_inr", "route_confidence_ceiling",
        "rationale", "inputs_hash",
    )

    def flat(self) -> dict:
        """A JSON/SQL-friendly dict: the enum becomes its name, everything
        else is already a scalar or None."""
        out = {name: getattr(self, name) for name in self.FLAT_FIELDS}
        out["skip_reason"] = self.skip_reason.name if self.skip_reason else None
        return out


class _RungEval(NamedTuple):
    name: str
    cost: float
    effectiveness: float
    p_rung: float
    ev: float


# --------------------------------------------------------------------- policy io
def load_policy(path: str | Path | None = None) -> dict:
    with open(path or POLICY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ small helpers
def _ticket_inr(event: dict) -> float:
    if event.get("ticket_inr") is not None:
        return float(event["ticket_inr"])
    if event.get("amount_paise") is not None:
        return float(event["amount_paise"]) / 100.0
    raise KeyError("event needs ticket_inr (or amount_paise)")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _channel_ok(requires_channel: str | None, event: dict) -> bool:
    if requires_channel is None:
        return True
    if requires_channel == "email":
        return bool(event.get("email"))
    if requires_channel == "phone":
        return bool(event.get("phone"))
    return False


def _any_channel_satisfiable(policy: dict, event: dict) -> bool:
    return any(
        _channel_ok(rung["requires_channel"], event)
        for rung in policy["action_ladder"]
    )


def _already_recovered(event: dict, history: dict) -> bool:
    return bool(event.get("already_recovered") or history.get("already_recovered"))


def _in_cooldown(event: dict, history: dict, policy: dict) -> bool:
    last = history.get("last_contact_at")
    if not last:
        return False
    elapsed = _parse_iso(event["now_utc"]) - _parse_iso(last)
    return elapsed < timedelta(hours=policy["cooldown_hours"])


def _self_recovered_in_control(history: dict) -> bool:
    return any(
        rec.get("arm") == "control"
        for rec in history.get("prior_recoveries", [])
    )


# ------------------------------------------------------------------- inputs hash
def _inputs_hash(event: dict, policy: dict, arm: str, history: dict) -> str:
    """sha256 of the canonical JSON of every input that influenced the
    decision: the whole policy, the arm, the event fields the engine reads,
    and the history fields it reads. Recomputable from the stored record."""
    canonical = {
        "policy": policy,
        "arm": arm,
        "event": {
            "payment_id": event.get("payment_id"),
            "cause": event.get("cause"),
            "cause_confidence": event.get("cause_confidence"),
            "ticket_inr": _ticket_inr(event),
            "risk_blocked": bool(event.get("risk_blocked", False)),
            "already_recovered": bool(event.get("already_recovered", False)),
            "has_email": bool(event.get("email")),
            "has_phone": bool(event.get("phone")),
            "now_utc": event.get("now_utc"),
        },
        "history": {
            "last_contact_at": history.get("last_contact_at"),
            "already_recovered": bool(history.get("already_recovered", False)),
            "prior_recovery_arms": sorted(
                rec.get("arm") for rec in history.get("prior_recoveries", [])
            ),
        },
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- the ladder
def _compute_ladder(
    policy: dict, event: dict, p_effective: float, ticket_inr: float
) -> tuple[_RungEval, list[_RungEval]]:
    """Evaluate every channel-eligible rung and return ``(best, all)``.

    ``p_rung = min(p_effective * effectiveness, 0.95)``;
    ``ev = p_rung * ticket_inr - cost_inr``.
    Best = max EV, ties broken to the lower-cost rung.

    Never called on any pre-ladder skip path (the hard gates and PRIOR_ZERO
    all return before this). BREAK 1 asserts exactly that by monkeypatching
    this function to blow up.
    """
    evals: list[_RungEval] = []
    for rung in policy["action_ladder"]:
        if not _channel_ok(rung["requires_channel"], event):
            continue
        p_rung = min(p_effective * rung["effectiveness"], _P_RUNG_CAP)
        ev = p_rung * ticket_inr - rung["cost_inr"]
        evals.append(
            _RungEval(rung["name"], rung["cost_inr"], rung["effectiveness"], p_rung, ev)
        )
    if not evals:  # pragma: no cover - NO_CONTACT_CHANNEL gate makes this unreachable
        raise RuntimeError("no channel-eligible rung; NO_CONTACT_CHANNEL gate missed")
    best = min(evals, key=lambda e: (-e.ev, e.cost))
    return best, evals


def _ev_lower(best: _RungEval, p_lower_bound: float, ticket_inr: float) -> float:
    """Confidence-haircut downside EV for the best rung. Recorded as a
    diagnostic; it gates nothing (see the module docstring)."""
    p_lower_rung = min(p_lower_bound * best.effectiveness, _P_RUNG_CAP)
    return p_lower_rung * ticket_inr - best.cost


# ------------------------------------------------------------------------ decide
def _finish(d: Decision) -> Decision:
    from app.decision.rationale import render  # late import: avoids an import cycle

    return dataclasses.replace(d, rationale=render(d))


def decide(event: dict, policy: dict, arm: str, history: dict) -> Decision:
    payment_id = event["payment_id"]
    policy_version = policy["policy_version"]
    cause = event["cause"]
    confidence = float(event["cause_confidence"])
    ticket_inr = _ticket_inr(event)
    inputs_hash = _inputs_hash(event, policy, arm, history)

    priors = policy["incremental_priors"]
    if cause not in priors:
        raise MissingPrior(
            f"cause {cause!r} has no incremental prior in policy "
            f"{policy_version!r}; incremental_priors must be total over every "
            f"cause the rules map can emit"
        )
    prior = float(priors[cause]["p_incremental"])

    def _pre_ladder_skip(reason: SkipReason, *, ev_inr=None) -> Decision:
        """Hard-gate / PRIOR_ZERO exit: no EV arithmetic, ladder untouched.
        Prior is a dict lookup so it is recorded; the rest is None."""
        return _finish(Decision(
            payment_id=payment_id, policy_version=policy_version,
            terminal=Terminal.SKIP, action=None, skip_reason=reason,
            cause=cause, cause_confidence=confidence,
            p_incremental_prior=prior, p_effective=None, p_action_basis=None,
            p_lower_bound=None, history_multiplier=1.0,
            ticket_inr=ticket_inr, action_cost_inr=None, ev_inr=ev_inr,
            ev_lower_inr=None, shadow_action=None, gate_basis="hard_gate",
            route_ticket_floor_inr=None, route_confidence_ceiling=None,
            rationale="", inputs_hash=inputs_hash,
        ))

    # --- (a) hard gates -- before any EV arithmetic, before the ladder ---
    if bool(event.get("risk_blocked", False)):
        return _pre_ladder_skip(SkipReason.RISK_BLOCKED)
    if _already_recovered(event, history):
        return _pre_ladder_skip(SkipReason.ALREADY_RECOVERED)
    if not _any_channel_satisfiable(policy, event):
        return _pre_ladder_skip(SkipReason.NO_CONTACT_CHANNEL)
    if _in_cooldown(event, history, policy):
        return _pre_ladder_skip(SkipReason.COOLDOWN)
    if prior == 0.0:
        return _pre_ladder_skip(SkipReason.PRIOR_ZERO)

    # --- (b-f) compute the ladder, best rung and bounds, regardless of arm ---
    pop = policy["population_incremental"]
    # (c) blend toward the population rate when the diagnosis is uncertain
    p_effective = confidence * prior + (1.0 - confidence) * pop
    # (d) a prior CONTROL-arm self-recovery => this customer comes back
    #     unaided; quarter the estimated nudge lift. TREATMENT-arm recoveries
    #     do NOT trigger this (they may have been caused by a past nudge).
    history_multiplier = 1.0
    if _self_recovered_in_control(history):
        history_multiplier = 0.25
        p_effective *= history_multiplier
    # (e) best rung
    best, _ladder = _compute_ladder(policy, event, p_effective, ticket_inr)
    # (f) confidence-haircut downside EV -- recorded as a diagnostic, gates nothing
    p_lower_bound = p_effective * max(confidence, policy["confidence_penalty_floor"])
    ev_lower_inr = _ev_lower(best, p_lower_bound, ticket_inr)

    common = dict(
        payment_id=payment_id, policy_version=policy_version,
        cause=cause, cause_confidence=confidence,
        p_incremental_prior=prior, p_effective=p_effective,
        p_action_basis=best.p_rung,
        p_lower_bound=p_lower_bound, history_multiplier=history_multiplier,
        ticket_inr=ticket_inr,
        action_cost_inr=best.cost, ev_inr=best.ev, ev_lower_inr=ev_lower_inr,
        inputs_hash=inputs_hash, rationale="",
    )

    # (i) POLICY OVERRIDE -- a standing rule, not an EV bound. All three
    #     conjuncts must hold: an expensive ("high touch") best rung, a large
    #     ticket, and a weak diagnosis.
    routed = (
        best.name in policy["high_touch_rungs"]
        and ticket_inr >= policy["human_review_ticket_inr"]
        and confidence < policy["review_confidence_threshold"]
    )
    would_act = best.ev >= policy["min_ev_inr"] and not routed

    # --- (g) control arm: never acts; records the shadow action ---
    if arm == "control":
        return _finish(Decision(
            terminal=Terminal.SKIP, action=None,
            skip_reason=SkipReason.CONTROL_ARM,
            shadow_action=best.name if would_act else None,
            gate_basis="experiment",
            route_ticket_floor_inr=None, route_confidence_ceiling=None, **common,
        ))

    # --- (h) EV floor ---
    if best.ev < policy["min_ev_inr"]:
        return _finish(Decision(
            terminal=Terminal.SKIP, action=None,
            skip_reason=SkipReason.EV_BELOW_FLOOR, shadow_action=None,
            gate_basis="expected_value",
            route_ticket_floor_inr=None, route_confidence_ceiling=None, **common,
        ))

    # --- (i) policy override -> a person must authorise ---
    if routed:
        # `action` carries the PROPOSED rung; `terminal` says it was not
        # executed automatically. Consumers gate on `terminal == ACT`.
        return _finish(Decision(
            terminal=Terminal.ROUTE_TO_HUMAN, action=best.name,
            skip_reason=None, shadow_action=None, gate_basis="policy_override",
            route_ticket_floor_inr=policy["human_review_ticket_inr"],
            route_confidence_ceiling=policy["review_confidence_threshold"], **common,
        ))

    # --- (j) act ---
    return _finish(Decision(
        terminal=Terminal.ACT, action=best.name,
        skip_reason=None, shadow_action=None, gate_basis="expected_value",
        route_ticket_floor_inr=None, route_confidence_ceiling=None, **common,
    ))
