"""Slice 7 - decision engine. The four break cases plus supporting tests.

Cause vocabulary is the UNION of what ``rules/error_code_map.json`` can emit
(``bank_downtime, gateway_timeout, insufficient_funds, invalid_card,
otp_timeout, unknown``) and what ``app.diagnosis.ROOT_CAUSES`` (the LLM
diagnoser) can emit (those six plus ``expired_card``) - seven in all.
``expired_card`` is reachable only through the LLM path; the rules map
collapses card expiry into ``invalid_card``. No other cause string appears
in this file except ``meteor_strike`` in ``test_unknown_cause_fails_loudly``
(deliberately not a real cause - that is the point of the test).

Break-case inputs deviate from the brief's illustrative numbers where those
numbers do not actually cross the threshold under test (documented per
test). The mechanism under test is unchanged; only the fixture value that
makes the branch fire was tuned:

  BREAK 2 - brief says Rs 99. With otp_timeout (prior 0.05), confidence 0.9
    and this ladder, the best rung (whatsapp) nets ~Rs 4.57 on Rs 99, above
    the Rs 2 floor -> ACT. A Rs 30 ticket puts the best rung's EV at
    ~Rs 0.94 (below the Rs 2.00 floor), and the min_ev_inr=0.0 rerun still
    proves it was the floor, not the arithmetic, that skipped it.

  BREAK 4 - step (i) is a POLICY OVERRIDE, not an EV bound (Fix A):
    best rung in high_touch_rungs AND ticket >= human_review_ticket_inr AND
    confidence < review_confidence_threshold -> ROUTE_TO_HUMAN. Cause is
    invalid_card. At Rs 18,000 / confidence 0.40 all three conjuncts hold and
    it routes; raising confidence to 0.85, or dropping the ticket to Rs 900,
    breaks one conjunct and it acts. ev_lower_inr no longer gates anything.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.decision import engine, store
from app.decision.engine import (
    Decision,
    MissingPrior,
    SkipReason,
    Terminal,
    decide,
    load_policy,
)
from app.decision.rationale import _money, _mult, _pct, _pct0, _pct2, render

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def policy() -> dict:
    return load_policy()


def make_event(**overrides) -> dict:
    ev = {
        "payment_id": "pay_slice7_0001",
        "cause": "invalid_card",
        "cause_confidence": 0.90,
        "ticket_inr": 2500.0,
        "risk_blocked": False,
        "already_recovered": False,
        "email": "buyer@example.test",
        "phone": "+919000000001",
        "now_utc": "2026-08-30T12:00:00+00:00",
    }
    ev.update(overrides)
    return ev


NO_HISTORY: dict = {}


def _zero_prior_policy(policy: dict, cause: str = "invalid_card") -> dict:
    """No cause in the real six carries a zero incremental prior, so a
    PRIOR_ZERO check pins one to zero in a policy copy."""
    return dict(policy, incremental_priors={
        **policy["incremental_priors"],
        cause: {"p_incremental": 0.0, "basis": "test fixture: forced zero"},
    })


# ------------------------------------------------------------------ NUMERIC AUDIT
_NUMERIC_FIELDS = (
    "cause_confidence", "p_incremental_prior", "p_effective", "p_action_basis",
    "p_lower_bound", "history_multiplier", "ticket_inr", "action_cost_inr",
    "ev_inr", "ev_lower_inr", "route_ticket_floor_inr", "route_confidence_ceiling",
)
_NUMBER_TOKEN = re.compile(r"\d[\d,]*\.?\d*")


def _allowed_number_haystack(d: Decision) -> str:
    parts: list[str] = []
    for name in _NUMERIC_FIELDS:
        v = getattr(d, name)
        if v is None:
            continue
        parts += [_money(v), _pct(v), _pct0(v), _pct2(v), _mult(v), str(v), repr(v)]
    return " ".join(parts)


def assert_rationale_numbers_are_on_the_record(d: Decision) -> None:
    """No number in the sentence that is not a formatted read of a numeric
    Decision field, and (the brief's literal check) no digit in the sentence
    absent from the record's numeric fields."""
    haystack = _allowed_number_haystack(d)
    for token in _NUMBER_TOKEN.findall(d.rationale):
        assert token in haystack, (
            f"rationale number {token!r} is not derivable from the record\n"
            f"rationale: {d.rationale}\nallowed: {haystack}"
        )
    stray = {c for c in d.rationale if c.isdigit()} - {c for c in haystack if c.isdigit()}
    assert not stray, f"rationale has digits not on the record: {stray}"


def assert_one_sentence(text: str) -> None:
    assert text and text.strip() == text
    assert "\n" not in text
    assert text.endswith(".")


# ============================================================ BREAK 1 - RISK
def test_break1_risk_blocked_short_circuits_before_any_ev_arithmetic(policy, monkeypatch):
    """risk-blocked Rs 30,000, invalid_card, confidence 0.95 -> SKIP
    RISK_BLOCKED, ev_inr None, action None, and the ladder is never touched.
    (Cause is irrelevant here - the risk gate fires before it is used.)"""
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("_compute_ladder must not be called on a hard gate")

    monkeypatch.setattr(engine, "_compute_ladder", _boom)

    ev = make_event(
        payment_id="pay_break1", cause="invalid_card",
        cause_confidence=0.95, ticket_inr=30000.0, risk_blocked=True,
    )
    d = decide(ev, policy, arm="treatment", history=NO_HISTORY)

    assert d.terminal == Terminal.SKIP
    assert d.skip_reason is SkipReason.RISK_BLOCKED
    assert d.ev_inr is None
    assert d.action is None
    assert d.p_effective is None and d.p_lower_bound is None
    assert d.ev_lower_inr is None and d.action_cost_inr is None
    assert d.shadow_action is None
    # the cheap prior lookup is still recorded, straight from the table
    assert d.p_incremental_prior == policy["incremental_priors"]["invalid_card"]["p_incremental"]
    assert_one_sentence(d.rationale)
    assert_rationale_numbers_are_on_the_record(d)
    print("BREAK 1:", d.terminal, d.skip_reason.name, "|", d.rationale)


# ============================================================ BREAK 2 - EV FLOOR
def test_break2_ev_below_floor_is_the_floor_not_the_arithmetic(policy):
    """Rs 30 ticket (see module note), otp_timeout, confidence 0.9,
    phone+email present. Best rung is cheap, EV below the floor -> SKIP
    EV_BELOW_FLOOR. Same inputs with min_ev_inr=0.0 -> ACT with the same
    cheap rung, same EV."""
    ev = make_event(
        payment_id="pay_break2", cause="otp_timeout",
        cause_confidence=0.90, ticket_inr=30.0,
    )

    skipped = decide(ev, policy, arm="treatment", history=NO_HISTORY)
    assert skipped.terminal == Terminal.SKIP
    assert skipped.skip_reason is SkipReason.EV_BELOW_FLOOR
    assert skipped.ev_inr is not None
    assert skipped.ev_inr < policy["min_ev_inr"]        # the relationship under test
    assert skipped.ev_inr > 0                            # ...and it is genuinely positive
    assert skipped.action is None
    assert_one_sentence(skipped.rationale)
    assert_rationale_numbers_are_on_the_record(skipped)

    no_floor = decide(
        ev, dict(policy, min_ev_inr=0.0), arm="treatment", history=NO_HISTORY
    )
    assert no_floor.terminal == Terminal.ACT
    assert no_floor.action in {"retry_silent", "email", "sms"}  # a cheap rung
    assert no_floor.ev_inr == pytest.approx(skipped.ev_inr)     # EV did not move; only the floor did
    print(f"BREAK 2: {skipped.terminal} {skipped.skip_reason.name} "
          f"ev_inr={skipped.ev_inr:.4f} (rendered {_money(skipped.ev_inr)}, "
          f"floor {_money(policy['min_ev_inr'])}) | {skipped.rationale}")
    print("BREAK 2 (min_ev_inr=0):", no_floor.terminal, no_floor.action, "|", no_floor.rationale)


# ============================================================ BREAK 3 - HISTORY
def _unadjusted_p_effective(policy: dict, cause: str, confidence: float) -> float:
    prior = policy["incremental_priors"][cause]["p_incremental"]
    pop = policy["population_incremental"]
    return confidence * prior + (1.0 - confidence) * pop


def test_break3_control_arm_self_recovery_changes_the_chosen_rung(policy):
    """repeat customer, prior CONTROL-arm self-recovery, Rs 2,000,
    invalid_card, confidence 0.9. The 4x cut to p_effective now moves the
    decision across a rung boundary (at Rs 2,500 it did not -- both sides
    stayed agent_call), so the two decisions differ in `action`, not merely
    in a probability. Rung names are not hardcoded: the assertion is that the
    penalised customer gets a *cheaper* rung."""
    ev = make_event(
        payment_id="pay_break3", cause="invalid_card",
        cause_confidence=0.90, ticket_inr=2000.0,
    )
    hist_control = {"prior_recoveries": [{"arm": "control"}]}

    d_hist = decide(ev, policy, arm="treatment", history=hist_control)
    d_none = decide(ev, policy, arm="treatment", history=NO_HISTORY)

    unadj = _unadjusted_p_effective(policy, "invalid_card", 0.90)
    assert d_none.p_effective == pytest.approx(unadj)
    assert d_hist.p_effective == pytest.approx(0.25 * unadj)   # relationship, not a literal
    assert d_hist.p_effective == pytest.approx(0.25 * d_none.p_effective)
    assert d_hist.history_multiplier == 0.25
    assert d_hist != d_none
    # the outcome changed, not just a number
    assert d_none.terminal == Terminal.ACT and d_hist.terminal == Terminal.ACT
    assert d_hist.action != d_none.action, (
        "Rs 2,000 was chosen so the history penalty flips the rung; if this "
        "fails the flip threshold moved -- report it, do not chase a ticket"
    )
    assert d_hist.action_cost_inr < d_none.action_cost_inr  # penalised -> cheaper rung
    assert "self-recovered" in d_hist.rationale
    assert_one_sentence(d_hist.rationale)
    assert_one_sentence(d_none.rationale)
    assert_rationale_numbers_are_on_the_record(d_hist)
    assert_rationale_numbers_are_on_the_record(d_none)
    print(f"BREAK 3 (penalised -> {d_hist.action}): {d_hist.rationale}")
    print(f"BREAK 3 (no history -> {d_none.action}): {d_none.rationale}")


def test_break3_paired_treatment_arm_recovery_does_not_trigger_multiplier(policy):
    """Same customer, but the prior recovery happened in the TREATMENT arm
    (it may have been caused by a past nudge) -> no multiplier."""
    ev = make_event(cause="invalid_card", cause_confidence=0.90, ticket_inr=2000.0)
    hist_treatment = {"prior_recoveries": [{"arm": "treatment"}]}

    d_treat = decide(ev, policy, arm="treatment", history=hist_treatment)
    d_none = decide(ev, policy, arm="treatment", history=NO_HISTORY)

    assert d_treat.history_multiplier == 1.0
    assert d_treat.p_effective == pytest.approx(_unadjusted_p_effective(policy, "invalid_card", 0.90))
    assert d_treat.p_effective == pytest.approx(d_none.p_effective)


# ============================================================ BREAK 4 - ROUTE
def test_break4_policy_override_routes_high_value_low_confidence(policy):
    """Rs 18,000, invalid_card, confidence 0.40. Best rung is agent_call (in
    high_touch_rungs), ticket >= human_review_ticket_inr, confidence <
    review_confidence_threshold -> ROUTE_TO_HUMAN via gate_basis
    'policy_override'. EV still clears the floor - the route is a standing
    policy rule, not a negative-EV decision. Two controls, each breaking one
    conjunct, both ACT."""
    routed = decide(
        make_event(payment_id="pay_break4", cause="invalid_card",
                   cause_confidence=0.40, ticket_inr=18000.0),
        policy, arm="treatment", history=NO_HISTORY,
    )
    assert routed.terminal == Terminal.ROUTE_TO_HUMAN
    assert routed.gate_basis == "policy_override"
    assert routed.skip_reason is None
    assert routed.ev_inr >= policy["min_ev_inr"]      # EV cleared the floor
    assert routed.action in policy["high_touch_rungs"]
    assert routed.route_ticket_floor_inr == policy["human_review_ticket_inr"]
    assert routed.route_confidence_ceiling == policy["review_confidence_threshold"]
    # the rationale must NOT claim the EV was negative
    assert "negative" not in routed.rationale.lower()
    assert "policy" in routed.rationale.lower()
    assert_one_sentence(routed.rationale)
    assert_rationale_numbers_are_on_the_record(routed)

    # control 1 - same event, confidence lifted above the ceiling -> ACT
    hi_conf = decide(
        make_event(payment_id="pay_break4_conf", cause="invalid_card",
                   cause_confidence=0.85, ticket_inr=18000.0),
        policy, arm="treatment", history=NO_HISTORY,
    )
    assert hi_conf.terminal == Terminal.ACT
    assert hi_conf.cause_confidence >= policy["review_confidence_threshold"]

    # control 2 - same event, ticket below the review floor -> ACT
    small = decide(
        make_event(payment_id="pay_break4_small", cause="invalid_card",
                   cause_confidence=0.40, ticket_inr=900.0),
        policy, arm="treatment", history=NO_HISTORY,
    )
    assert small.terminal == Terminal.ACT
    assert small.ticket_inr < policy["human_review_ticket_inr"]

    print("BREAK 4:", routed.terminal, routed.gate_basis, "|", routed.rationale)
    print("BREAK 4 (conf 0.85):", hi_conf.terminal, hi_conf.action)
    print("BREAK 4 (Rs 900):", small.terminal, small.action)


# ============================================================ SUPPORTING
def test_determinism_same_inputs_same_hash_and_decision(policy):
    ev = make_event()
    hist = {"prior_recoveries": [{"arm": "control"}], "last_contact_at": None}
    a = decide(ev, policy, arm="treatment", history=hist)
    b = decide(ev, policy, arm="treatment", history=hist)
    assert a == b
    assert a.inputs_hash == b.inputs_hash
    # a material input change moves the hash
    c = decide(make_event(ticket_inr=2501.0), policy, arm="treatment", history=hist)
    assert c.inputs_hash != a.inputs_hash


def test_inputs_hash_covers_policy_version(policy):
    ev = make_event()
    a = decide(ev, policy, arm="treatment", history=NO_HISTORY)
    b = decide(ev, dict(policy, policy_version="s7.1-x"), arm="treatment", history=NO_HISTORY)
    assert a.inputs_hash != b.inputs_hash
    assert a.policy_version == "s7.1"


def _map_cause_vocabulary() -> set[str]:
    m = json.loads((REPO / "rules" / "error_code_map.json").read_text(encoding="utf-8"))
    return set(m["map"].values()) | {m["default"]}


def _llm_cause_vocabulary() -> set[str]:
    from app.diagnosis import ROOT_CAUSES
    return set(ROOT_CAUSES)


def _expected_prior_causes() -> dict[str, list[str]]:
    """cause -> the source(s) that require a prior for it. A cause must have a
    prior if EITHER the rules map or the LLM diagnoser can emit it."""
    map_v, llm_v = _map_cause_vocabulary(), _llm_cause_vocabulary()
    out: dict[str, list[str]] = {}
    for c in map_v | llm_v:
        out[c] = (["error_code_map"] if c in map_v else []) + \
                 (["ROOT_CAUSES"] if c in llm_v else [])
    return out


def test_prior_table_matches_union_of_map_and_llm_vocabularies_exactly(policy):
    """Set equality in BOTH directions against
      set(m['map'].values()) | {m['default']} | set(ROOT_CAUSES)
    derived at test time. A missing prior names which source needs it; a
    prior for a cause no source can emit fails just as loudly. This is the
    check that would have caught both the original drift
    (authentication_timeout / abandoned_cart / ...) and the LLM-only
    expired_card gap."""
    expected = _expected_prior_causes()
    keys = set(policy["incremental_priors"])
    missing = {c: srcs for c, srcs in expected.items() if c not in keys}
    phantom = sorted(keys - set(expected))
    assert not missing and not phantom, (
        "incremental_priors is out of sync with the cause vocabulary:\n"
        + "".join(f"  missing '{c}'  (required by: {', '.join(s)})\n"
                  for c, s in sorted(missing.items()))
        + (f"  prior for a cause neither source can emit: {phantom}\n" if phantom else "")
    )


def test_llm_root_causes_is_a_superset_of_the_rules_map_vocabulary():
    """ROOT_CAUSES must cover everything the map can emit; the reverse need
    not hold. The delta is reported, not silently tolerated - a new LLM-only
    cause, or (worse) a map cause the LLM cannot produce, surfaces here as a
    named difference instead of a MissingPrior at runtime."""
    map_v, llm_v = _map_cause_vocabulary(), _llm_cause_vocabulary()
    only_map = sorted(map_v - llm_v)
    only_llm = sorted(llm_v - map_v)
    assert only_map == [], f"the rules map can emit causes the LLM diagnoser cannot: {only_map}"
    assert only_llm == ["expired_card"], f"LLM-only causes changed, was ['expired_card']: {only_llm}"
    print(f"vocabulary delta -> map-only: {only_map}   LLM-only: {only_llm}")


def test_unknown_cause_fails_loudly(policy):
    with pytest.raises(MissingPrior):
        decide(make_event(cause="meteor_strike"), policy, arm="treatment", history=NO_HISTORY)
    with pytest.raises(KeyError):  # MissingPrior is a KeyError
        decide(make_event(cause="meteor_strike"), policy, arm="treatment", history=NO_HISTORY)


def test_control_arm_shadow_action_populated_when_it_would_have_acted(policy):
    would_act = make_event(cause="invalid_card", cause_confidence=0.90, ticket_inr=2500.0)
    d = decide(would_act, policy, arm="control", history=NO_HISTORY)
    assert d.terminal == Terminal.SKIP
    assert d.skip_reason is SkipReason.CONTROL_ARM
    assert d.shadow_action is not None
    # the shadow is whatever rung treatment would have fired
    treat = decide(would_act, policy, arm="treatment", history=NO_HISTORY)
    assert treat.terminal == Terminal.ACT
    assert d.shadow_action == treat.action
    assert d.action is None
    # every probability / EV field still populated on the control path
    for f in ("p_incremental_prior", "p_effective", "p_lower_bound",
              "ticket_inr", "action_cost_inr", "ev_inr", "ev_lower_inr"):
        assert getattr(d, f) is not None
    assert_rationale_numbers_are_on_the_record(d)


def test_control_arm_shadow_action_none_when_it_would_have_skipped(policy):
    would_skip = make_event(cause="otp_timeout", cause_confidence=0.90, ticket_inr=30.0)
    d = decide(would_skip, policy, arm="control", history=NO_HISTORY)
    assert d.skip_reason is SkipReason.CONTROL_ARM
    assert d.shadow_action is None
    assert d.ev_inr is not None                    # still computed, just below the floor
    assert d.ev_inr < policy["min_ev_inr"]


def test_prior_zero_cause_skips_before_the_ladder(policy, monkeypatch):
    monkeypatch.setattr(engine, "_compute_ladder",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ladder ran")))
    d = decide(make_event(cause="invalid_card", ticket_inr=5000.0),
               _zero_prior_policy(policy), arm="treatment", history=NO_HISTORY)
    assert d.terminal == Terminal.SKIP
    assert d.skip_reason is SkipReason.PRIOR_ZERO
    assert d.ev_inr is None and d.action is None
    assert d.p_incremental_prior == 0.0


def test_already_recovered_hard_gate(policy):
    d = decide(make_event(already_recovered=True), policy, arm="treatment", history=NO_HISTORY)
    assert d.skip_reason is SkipReason.ALREADY_RECOVERED
    assert d.ev_inr is None


def test_cooldown_hard_gate_uses_history_and_passed_now(policy):
    ev = make_event(now_utc="2026-08-30T12:00:00+00:00")
    recent = {"last_contact_at": "2026-08-30T02:00:00+00:00"}  # 10h < 24h
    stale = {"last_contact_at": "2026-08-28T02:00:00+00:00"}   # > 24h
    assert decide(ev, policy, "treatment", recent).skip_reason is SkipReason.COOLDOWN
    assert decide(ev, policy, "treatment", stale).skip_reason is not SkipReason.COOLDOWN


def test_no_contact_channel_only_fires_without_a_channel_free_rung(policy):
    contactless = make_event(email=None, phone=None)
    # the real ladder keeps retry_silent (requires_channel = null), so it acts
    d_full = decide(contactless, policy, "treatment", NO_HISTORY)
    assert d_full.skip_reason is not SkipReason.NO_CONTACT_CHANNEL
    # drop the channel-free rung and the gate fires
    trimmed = dict(policy, action_ladder=[r for r in policy["action_ladder"]
                                          if r["requires_channel"] is not None])
    d_trim = decide(contactless, trimmed, "treatment", NO_HISTORY)
    assert d_trim.skip_reason is SkipReason.NO_CONTACT_CHANNEL
    assert d_trim.ev_inr is None


def test_every_terminal_and_skip_reason_has_a_distinct_rationale(policy):
    zeroed = _zero_prior_policy(policy)
    seen: dict[str, str] = {}
    cases = [
        ("risk", policy, make_event(risk_blocked=True), "treatment", NO_HISTORY),
        ("recovered", policy, make_event(already_recovered=True), "treatment", NO_HISTORY),
        ("cooldown", policy, make_event(), "treatment", {"last_contact_at": "2026-08-30T11:00:00+00:00"}),
        ("prior_zero", zeroed, make_event(cause="invalid_card"), "treatment", NO_HISTORY),
        ("floor", policy, make_event(cause="otp_timeout", ticket_inr=30.0), "treatment", NO_HISTORY),
        ("control", policy, make_event(ticket_inr=2500.0), "control", NO_HISTORY),
        ("route", policy, make_event(cause="invalid_card", cause_confidence=0.40, ticket_inr=18000.0), "treatment", NO_HISTORY),
        ("act", policy, make_event(cause="invalid_card", ticket_inr=2500.0), "treatment", NO_HISTORY),
    ]
    for label, pol, ev, arm, hist in cases:
        d = decide(ev, pol, arm, hist)
        assert_one_sentence(d.rationale)
        assert_rationale_numbers_are_on_the_record(d)
        seen[label] = d.rationale
    assert len(set(seen.values())) == len(seen), seen


# ----------------------------------------------------- Fix A - gate_basis
def test_gate_basis_records_why_each_decision_landed(policy):
    hard = decide(make_event(risk_blocked=True), policy, "treatment", NO_HISTORY)
    exp = decide(make_event(cause="invalid_card", ticket_inr=2500.0), policy, "treatment", NO_HISTORY)
    floor = decide(make_event(cause="otp_timeout", ticket_inr=30.0), policy, "treatment", NO_HISTORY)
    control = decide(make_event(ticket_inr=2500.0), policy, "control", NO_HISTORY)
    override = decide(make_event(cause="invalid_card", cause_confidence=0.40, ticket_inr=18000.0),
                      policy, "treatment", NO_HISTORY)
    pz = decide(make_event(cause="invalid_card"), _zero_prior_policy(policy), "treatment", NO_HISTORY)

    assert hard.gate_basis == "hard_gate" and hard.skip_reason is SkipReason.RISK_BLOCKED
    assert pz.gate_basis == "hard_gate" and pz.skip_reason is SkipReason.PRIOR_ZERO
    assert exp.gate_basis == "expected_value" and exp.terminal == Terminal.ACT
    assert floor.gate_basis == "expected_value" and floor.skip_reason is SkipReason.EV_BELOW_FLOOR
    assert control.gate_basis == "experiment" and control.skip_reason is SkipReason.CONTROL_ARM
    assert override.gate_basis == "policy_override" and override.terminal == Terminal.ROUTE_TO_HUMAN


def test_ev_lower_no_longer_gates_and_is_a_plain_diagnostic(policy):
    """Fix A: with population_incremental removed from ev_lower_inr, a
    high-value ticket has a positive worst-case and still is NOT routed -
    routing is the policy override, not this number."""
    d = decide(make_event(cause="invalid_card", cause_confidence=0.90, ticket_inr=18000.0),
               policy, "treatment", NO_HISTORY)
    # confidence 0.90 >= review_confidence_threshold -> no override -> ACT
    assert d.terminal == Terminal.ACT
    assert d.ev_lower_inr == pytest.approx(
        min(d.p_lower_bound * 1.45, 0.95) * d.ticket_inr - d.action_cost_inr
    )
    assert d.ev_lower_inr > 0  # and it did not route despite being a huge ticket


def test_route_to_human_action_is_a_proposal_not_authorised_spend(policy):
    """Residual 3: `action` on a ROUTE_TO_HUMAN decision is the PROPOSED
    rung, pending a person -- no spend is authorised. Consumers must gate on
    `terminal == Terminal.ACT`, never on `action is not None`."""
    d = decide(
        make_event(cause="invalid_card", cause_confidence=0.40, ticket_inr=18000.0),
        policy, arm="treatment", history=NO_HISTORY,
    )
    assert d.terminal == Terminal.ROUTE_TO_HUMAN
    assert d.action is not None and d.terminal != Terminal.ACT, (
        "action is set on a ROUTE_TO_HUMAN decision (the proposed rung), so "
        "`action is not None` must NOT be read as 'spend authorised' -- "
        "execution gates on terminal == Terminal.ACT"
    )
    assert "action" in Decision.__doc__ and "Terminal.ACT" in Decision.__doc__


# ----------------------------------------------------- Fix C - integrity
def test_flat_fields_matches_dataclass_fields_exactly():
    field_names = [f.name for f in dataclasses.fields(Decision)]
    ff = list(Decision.FLAT_FIELDS)
    dups = [x for x in ff if ff.count(x) > 1]
    assert not dups, f"FLAT_FIELDS has duplicates: {sorted(set(dups))}"
    assert set(ff) == set(field_names), (
        "FLAT_FIELDS vs Decision dataclass fields:\n"
        f"  in FLAT_FIELDS only : {sorted(set(ff) - set(field_names))}\n"
        f"  in dataclass only   : {sorted(set(field_names) - set(ff))}"
    )


def test_store_does_not_enable_the_foreign_keys_pragma():
    src = (REPO / "app" / "decision" / "store.py").read_text(encoding="utf-8")
    assert "PRAGMA foreign_keys" not in src, (
        "store.py must not enable foreign_keys: a decision references an "
        "ingested payment that need not have a row in Slice 2's events table"
    )


# ----------------------------------------------------- Fix D - rationale arithmetic
_D_PCT = re.compile(r"(\d+(?:\.\d+)?)% estimated recovery lift")
_D_TICKET = re.compile(r"on a Rs ([\d,]+\.\d{2}) ticket is worth")
_D_EV = re.compile(r"is worth Rs (-?[\d,]+\.\d{2}) expected")
_D_COST = re.compile(r"against Rs ([\d,]+\.\d{2}) to send")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_ev_identity(rendered: str) -> tuple[float, float, float]:
    """Parse pct / ticket / cost / ev straight out of the RENDERED STRING and
    return (lhs = pct*ticket - cost, ev, tolerance). The tolerance is exactly
    the display-rounding band: pct shown to 0.1 percentage-point (5e-4 in
    probability), each rupee value to 0.005 - and no wider."""
    pct = float(_D_PCT.search(rendered).group(1)) / 100.0
    ticket = _num(_D_TICKET.search(rendered).group(1))
    ev = _num(_D_EV.search(rendered).group(1))
    cost = _num(_D_COST.search(rendered).group(1))
    # pct now shown to 2dp of a percent (0.01% => 1e-4 in probability, half 5e-5)
    tol = 0.00005 * ticket + pct * 0.005 + 0.005 + 0.005
    return pct * ticket - cost, ev, tol


def _fix_d_decisions(policy) -> list[Decision]:
    return [
        decide(make_event(cause="invalid_card", cause_confidence=0.90, ticket_inr=2500.0),
               policy, "treatment", {"prior_recoveries": [{"arm": "control"}]}),  # ACT agent_call, basis != p_eff
        decide(make_event(cause="invalid_card", cause_confidence=0.40, ticket_inr=900.0),
               policy, "treatment", NO_HISTORY),                                   # ACT agent_call
        decide(make_event(cause="invalid_card", cause_confidence=0.90, ticket_inr=100.0),
               policy, "treatment", NO_HISTORY),                                   # ACT whatsapp, basis == p_eff
        decide(make_event(cause="otp_timeout", cause_confidence=0.90, ticket_inr=30.0),
               policy, "treatment", NO_HISTORY),                                   # EV_BELOW_FLOOR sms
    ]


def test_fix_d_rendered_sentence_arithmetic_is_internally_consistent(policy):
    checked = 0
    for d in _fix_d_decisions(policy):
        if d.terminal == Terminal.ACT or d.skip_reason is SkipReason.EV_BELOW_FLOOR:
            lhs, ev, tol = _parse_ev_identity(d.rationale)
            gap = abs(lhs - ev)
            assert gap <= tol, (
                f"{d.terminal}/{d.skip_reason}: sentence says the lift is a % that "
                f"gives {lhs:.4f}, but states EV {ev:.4f} (band +/-{tol:.4f})\n{d.rationale}"
            )
            print(f"Fix D  {d.terminal}/{getattr(d.skip_reason, 'name', None)} "
                  f"ticket={d.ticket_inr:.0f} rung={d.action}: gap Rs {gap:.4f}  band +/-Rs {tol:.4f}")
            checked += 1
    assert checked >= 4  # 3 ACT + 1 EV_BELOW_FLOOR in _fix_d_decisions


def test_fix_d_check_has_teeth_p_effective_would_fail_it(policy):
    """If the sentence had rendered p_effective (the pre-fix bug) instead of
    p_action_basis, the reader's own arithmetic would land well outside the
    band for any rung whose effectiveness != 1.0."""
    proved = 0
    for d in _fix_d_decisions(policy):
        if d.terminal != Terminal.ACT and d.skip_reason is not SkipReason.EV_BELOW_FLOOR:
            continue
        if d.p_action_basis == pytest.approx(d.p_effective):
            continue  # effectiveness == 1.0 rung: no discrepancy to catch
        _, ev, tol = _parse_ev_identity(d.rationale)
        wrong = d.p_effective * d.ticket_inr - d.action_cost_inr
        assert abs(wrong - ev) > tol, (
            f"p_effective substitution should break the band but didn't: "
            f"{wrong:.4f} vs {ev:.4f} (band +/-{tol:.4f})"
        )
        proved += 1
    assert proved >= 2


# ----------------------------------------------------- Fix E - unknown coupling
def test_unknown_prior_is_coupled_to_population_incremental(policy):
    unk = policy["incremental_priors"]["unknown"]
    assert "population_incremental" in unk["basis"], (
        "unknown's basis must state the coupling it relies on"
    )
    assert unk["p_incremental"] == policy["population_incremental"], (
        "unknown's own basis: it 'falls back to population_incremental ... "
        "correct as long as it tracks population_incremental' -- enforce that: "
        f"{unk['p_incremental']} != {policy['population_incremental']}"
    )


# ----------------------------------------------------------------- persistence
def test_record_decision_is_append_only_and_writes_audit_in_one_txn(tmp_path):
    db = tmp_path / "decisions.db"
    store.init_decision_store(db)
    policy = load_policy()
    d = decide(make_event(payment_id="pay_persist", cause="invalid_card", ticket_inr=2500.0),
               policy, arm="treatment", history=NO_HISTORY)

    row_id = store.record_decision(d, event_id="evt_persist", db_path=db,
                                   now="2026-08-30T12:00:00+00:00")

    conn = sqlite3.connect(str(db))
    try:
        got = conn.execute(
            "SELECT payment_id, terminal, inputs_hash, history_multiplier, "
            "gate_basis, p_action_basis FROM decisions WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert got == ("pay_persist", d.terminal, d.inputs_hash, d.history_multiplier,
                       d.gate_basis, d.p_action_basis)
        audit = conn.execute(
            "SELECT payment_id, event_id, action FROM audit WHERE event_id = 'evt_persist'"
        ).fetchall()
        assert audit == [("pay_persist", "evt_persist", "decision")]

        with pytest.raises(sqlite3.IntegrityError, match="decisions is append-only: UPDATE is not allowed"):
            conn.execute("UPDATE decisions SET terminal = 'ACT' WHERE id = ?", (row_id,))
        with pytest.raises(sqlite3.IntegrityError, match="decisions is append-only: DELETE is not allowed"):
            conn.execute("DELETE FROM decisions WHERE id = ?", (row_id,))
    finally:
        conn.close()


# ---------------------------------------------------- Slice 1-6 isolation intact
def test_app_still_never_references_the_holdout_answer_key():
    """The Slice 3 wall, re-asserted here: nothing under app/ names the
    held-out answer key, the blind-label artifacts, or reaches into eval/ by
    any import route (not just the `eval.` substring)."""
    import re as _re

    substrings = ("ground_truth", "blind_sample", "_truth_manifest")
    eval_import = _re.compile(r"^[ \t]*(from[ \t]+eval\b|import[ \t]+eval\b)", _re.M)
    eval_attr = _re.compile(r"\beval\.")

    hits = []
    for py in (REPO / "app").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        rel = py.relative_to(REPO)
        hits += [f"{rel}: {tok}" for tok in substrings if tok in text]
        if eval_import.search(text):
            hits.append(f"{rel}: imports from eval/")
        if eval_attr.search(text):
            hits.append(f"{rel}: bare `eval.` reference")
    assert hits == [], hits
