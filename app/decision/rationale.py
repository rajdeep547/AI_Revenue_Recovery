"""One-sentence, on-the-record justification for a :class:`Decision`.

``render(decision)`` returns a single sentence whose every number is a
formatted read of a :class:`Decision` field -- never recomputed, never a
constant pulled from the policy (the policy-override thresholds are on the
record as ``route_ticket_floor_inr`` / ``route_confidence_ceiling`` for
exactly this reason). Phrasing differs per terminal and, for a SKIP, per
:class:`SkipReason`.

The percentage in an ACT / EV_BELOW_FLOOR sentence is ``p_action_basis``
(the rung-level probability ``ev_inr`` was computed from), NOT
``p_effective`` -- so a reader multiplying the sentence's own numbers gets
``pct * ticket - cost == ev_inr`` up to 2dp display rounding.
``tests/test_slice7_decision.py`` asserts both that (parsed from the
rendered string) and that no digit in a sentence is absent from the
record's numeric fields.
"""

from __future__ import annotations

from app.decision.engine import Decision, SkipReason, Terminal


def _money(x: float) -> str:
    return f"Rs {x:,.2f}"


def _pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def _pct0(p: float) -> str:
    return f"{p * 100:.0f}%"


def _pct2(p: float) -> str:
    return f"{p * 100:.2f}%"


def _mult(m: float) -> str:
    return f"x{m:g}"


def _history_clause(d: Decision) -> str:
    if d.history_multiplier == 1.0:
        return ""
    return (
        f" (nudge lift cut {_mult(d.history_multiplier)} because this customer "
        f"self-recovered a past failure with no contact)"
    )


def _ev_clause(d: Decision) -> str:
    """The shared EV arithmetic fragment. The percentage is
    ``p_action_basis`` -- the rung-level probability ``ev_inr`` was actually
    computed from -- NOT ``p_effective``, and is rendered to 2dp so a reader
    multiplying the sentence's own numbers lands within a hair of the stated
    ``ev_inr`` (``pct * ticket - cost == ev_inr`` to ~0.01% of the ticket)."""
    return (
        f"{_pct2(d.p_action_basis)} estimated recovery lift on a "
        f"{_money(d.ticket_inr)} ticket is worth {_money(d.ev_inr)} expected "
        f"against {_money(d.action_cost_inr)} to send"
    )


def render(d: Decision) -> str:
    cause = d.cause

    if d.terminal == Terminal.ACT:
        return (
            f"Acting with {d.action} on this {cause} failure{_history_clause(d)}: "
            f"{_ev_clause(d)}, with a {_money(d.ev_lower_inr)} worst-case."
        )

    if d.terminal == Terminal.ROUTE_TO_HUMAN:
        return (
            f"Routing to a human: policy holds back {d.action} on tickets at or "
            f"above {_money(d.route_ticket_floor_inr)} when diagnosis confidence "
            f"is under {_pct0(d.route_confidence_ceiling)} -- this "
            f"{_money(d.ticket_inr)} ticket is diagnosed at "
            f"{_pct(d.cause_confidence)}, and the {_money(d.ev_inr)} expected value "
            f"is not by itself sufficient to authorise the spend."
        )

    # --- SKIP, by reason ---
    if d.skip_reason == SkipReason.RISK_BLOCKED:
        return (
            f"Skipped: the risk engine blocked this {_money(d.ticket_inr)} {cause} "
            f"payment, so no recovery nudge is appropriate and no EV was computed."
        )

    if d.skip_reason == SkipReason.ALREADY_RECOVERED:
        return (
            f"Skipped: this {cause} payment is already recovered, so a "
            f"{_money(d.ticket_inr)} nudge would be spend on a solved problem."
        )

    if d.skip_reason == SkipReason.NO_CONTACT_CHANNEL:
        return (
            f"Skipped: no usable contact channel for this customer, so no rung of "
            f"the ladder can run on this {_money(d.ticket_inr)} {cause} failure."
        )

    if d.skip_reason == SkipReason.COOLDOWN:
        return (
            f"Skipped: this customer was contacted inside the cooldown window, so "
            f"this {_money(d.ticket_inr)} {cause} failure waits rather than "
            f"stacking another message."
        )

    if d.skip_reason == SkipReason.PRIOR_ZERO:
        return (
            f"Skipped: {cause} carries a zero incremental prior -- a nudge cannot "
            f"cause this recovery -- so the {_money(d.ticket_inr)} ticket is left "
            f"alone."
        )

    if d.skip_reason == SkipReason.CONTROL_ARM:
        if d.shadow_action is not None:
            return (
                f"Held out as control (no contact by policy); in treatment this "
                f"{cause} case would have fired {d.shadow_action} for "
                f"{_money(d.ev_inr)} expected on a {_money(d.ticket_inr)} ticket."
            )
        return (
            f"Held out as control (no contact by policy); in treatment this "
            f"{cause} case would still have been skipped, at {_money(d.ev_inr)} "
            f"expected on a {_money(d.ticket_inr)} ticket."
        )

    if d.skip_reason == SkipReason.EV_BELOW_FLOOR:
        return (
            f"Skipped: for this {cause} failure, {_ev_clause(d)}"
            f"{_history_clause(d)} -- under the minimum expected value to act."
        )

    # Exhaustive above; kept so a new SkipReason fails loudly in tests.
    raise ValueError(f"no rationale template for {d.terminal!r} / {d.skip_reason!r}")
