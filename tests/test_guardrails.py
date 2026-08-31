"""Slice 9 - guardrails. Evaluate-all-then-decide.

The point of the slice is the evaluation contract: ``evaluate_all`` runs ALL
SEVEN guardrails with no short-circuit, the full report is persisted (seven
rows, always), and a treatment-arm event that gets fully blocked becomes a
third outcome class, ``treatment_blocked``, without disturbing arm assignment.

Every clock is frozen explicitly -- no test reads the wall clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import datagen
from app import guardrails as g
from app.arms import assign_arm
from app.decision.engine import load_policy
from app.guardrails import (
    GUARDRAIL_ORDER,
    GuardrailConfig,
    GuardrailState,
    evaluate_all,
    init_guardrail_store,
    load_state,
    record_evaluation,
    record_spend,
    spent_today_inr,
    walk_ladder,
)
from eval.measurement import recover_everything_policy, run_policy

LADDER = {r["name"]: r for r in load_policy()["action_ladder"]}
IST = timezone(timedelta(hours=5, minutes=30))
CONTACT_RUNGS = ("email", "sms", "whatsapp", "agent_call")


def _ist(y, mo, d, h, mi=0) -> str:
    """A frozen ISO timestamp at an explicit IST wall-clock time."""
    return datetime(y, mo, d, h, mi, tzinfo=IST).isoformat()


# a daytime IST moment where nothing time-based fires
NOON = _ist(2026, 8, 31, 12, 0)
NIGHT = _ist(2026, 8, 31, 22, 30)  # inside 21:00-09:00 IST


def _event(customer_id="cust_x", payment_id="pay_x", **extra) -> dict:
    e = {"customer_id": customer_id, "payment_id": payment_id}
    e.update(extra)
    return e


def _ledger_rows(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM spend_ledger ORDER BY id")]
    finally:
        con.close()


def _eval_rows(db_path, event_id):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM guardrail_evaluations WHERE event_id = ? ORDER BY id",
                (event_id,),
            )
        ]
    finally:
        con.close()


# ============================================================ 1. kill_switch
def test_kill_switch_blocks_everything():
    state = GuardrailState(config=GuardrailConfig(kill_switch=True))
    for name, rung in LADDER.items():
        report = evaluate_all(_event(), rung, state, NOON)
        assert report.blocked, name
        assert "kill_switch" in report.blocked_by, name
        assert report.terminal == "BLOCKED/KILL_SWITCH", name
    # the silent rung is not exempt
    silent = evaluate_all(_event(), LADDER["retry_silent"], state, NOON)
    assert silent.result("kill_switch").blocked

    # switch off -> kill_switch never blocks
    off = GuardrailState(config=GuardrailConfig(kill_switch=False))
    assert not evaluate_all(_event(), LADDER["retry_silent"], off, NOON).blocked


# ============================================================ 2. opt_out
def test_opt_out_blocks_all_contact_rungs():
    state = GuardrailState(config=GuardrailConfig(), opted_out=True)
    for name in CONTACT_RUNGS:
        report = evaluate_all(_event(), LADDER[name], state, NOON)
        assert "opt_out" in report.blocked_by, name
        assert report.terminal == "BLOCKED/OPT_OUT", name

    # retry_silent reaches no one, so opt_out lets it through
    silent = evaluate_all(_event(), LADDER["retry_silent"], state, NOON)
    assert "opt_out" not in silent.blocked_by
    assert not silent.blocked

    # not opted out -> contact rungs are fine
    not_out = GuardrailState(config=GuardrailConfig(), opted_out=False)
    assert not evaluate_all(_event(), LADDER["email"], not_out, NOON).blocked


# ============================================================ 3. attempt_cap
def test_attempt_cap_blocks_after_max(tmp_path):
    cfg = GuardrailConfig()  # default cap 3

    # --- pure-predicate view (unchanged) ---
    below = GuardrailState(config=cfg, payment_action_count=2)
    at = GuardrailState(config=cfg, payment_action_count=3)
    over = GuardrailState(config=cfg, payment_action_count=9)
    assert "attempt_cap" not in evaluate_all(_event(), LADDER["email"], below, NOON).blocked_by
    r = evaluate_all(_event(), LADDER["email"], at, NOON)
    assert "attempt_cap" in r.blocked_by
    assert r.terminal == "BLOCKED/ATTEMPT_CAP"
    # it is per payment_id across any rung, silent included
    assert "attempt_cap" in evaluate_all(_event(), LADDER["retry_silent"], over, NOON).blocked_by

    # --- Slice 9b: load_state counts COMMITMENTS (debit + dry_run), not debits ---
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)
    pay = "pay_ac"
    # two real debits + one dry-run commitment = three committed actions
    record_spend(event_id="e1", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db, now=NOON, dispatched=True, dry_run=False)
    record_spend(event_id="e2", customer_id="c", payment_id=pay, rung=LADDER["retry_silent"],
                 db_path=db, now=NOON, dispatched=True, dry_run=False)
    record_spend(event_id="e3", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db, now=NOON, dispatched=False, dry_run=True)
    # a blocked action consumed no attempt -> must NOT count toward the cap
    record_spend(event_id="e4", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db, now=NOON, dispatched=False, dry_run=False)

    st = load_state(db, customer_id="c", payment_id=pay, config=cfg, now=NOON)
    assert st.payment_action_count == 3  # 2 debit + 1 dry_run; the 'blocked' row excluded
    assert "attempt_cap" in evaluate_all(_event(payment_id=pay), LADDER["email"], st, NOON).blocked_by

    # drop the dry-run commitment: only 2 committed + 1 blocked -> under the cap
    db2 = str(tmp_path / "g2.db")
    init_guardrail_store(db2)
    record_spend(event_id="d1", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db2, now=NOON, dispatched=True, dry_run=False)
    record_spend(event_id="d2", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db2, now=NOON, dispatched=False, dry_run=True)
    record_spend(event_id="b1", customer_id="c", payment_id=pay, rung=LADDER["email"],
                 db_path=db2, now=NOON, dispatched=False, dry_run=False)  # blocked, uncounted
    st2 = load_state(db2, customer_id="c", payment_id=pay, config=cfg, now=NOON)
    assert st2.payment_action_count == 2
    assert "attempt_cap" not in evaluate_all(_event(payment_id=pay), LADDER["email"], st2, NOON).blocked_by


# ============================================================ 4. contact_limit
def test_contact_limit_blocks_within_rolling_window(tmp_path):
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)
    cfg = GuardrailConfig()  # default 2 contact actions / 24h

    cust, pay = "cust_cl", "pay_cl"
    # two contact dispatches: one 2h before NOON (in window), one 30h before (rolled out)
    record_spend(event_id="e_recent", customer_id=cust, payment_id=pay,
                 rung=LADDER["sms"], db_path=db, now=_ist(2026, 8, 31, 10, 0),
                 dispatched=True, dry_run=False)
    record_spend(event_id="e_old", customer_id=cust, payment_id="pay_old",
                 rung=LADDER["email"], db_path=db, now=_ist(2026, 8, 30, 6, 0),
                 dispatched=True, dry_run=False)

    state = load_state(db, customer_id=cust, payment_id=pay, config=cfg, now=NOON)
    assert state.contact_actions_in_window == 1  # the 30h-old one has rolled out
    assert "contact_limit" not in evaluate_all(_event(cust, pay), LADDER["sms"], state, NOON).blocked_by

    # a second recent contact dispatch tips it to the cap
    record_spend(event_id="e_recent2", customer_id=cust, payment_id=pay,
                 rung=LADDER["whatsapp"], db_path=db, now=_ist(2026, 8, 31, 11, 0),
                 dispatched=True, dry_run=False)
    state2 = load_state(db, customer_id=cust, payment_id=pay, config=cfg, now=NOON)
    assert state2.contact_actions_in_window == 2

    blocked = evaluate_all(_event(cust, pay), LADDER["sms"], state2, NOON)
    assert "contact_limit" in blocked.blocked_by
    # counts across channels, not per channel: email is blocked at the same count
    assert "contact_limit" in evaluate_all(_event(cust, pay), LADDER["email"], state2, NOON).blocked_by
    # retry_silent is not a contact rung -> unaffected
    assert "contact_limit" not in evaluate_all(_event(cust, pay), LADDER["retry_silent"], state2, NOON).blocked_by

    # --- Slice 9b: a dry-run contact COMMITMENT counts toward the limit ---
    record_spend(event_id="e_dry", customer_id=cust, payment_id=pay,
                 rung=LADDER["email"], db_path=db, now=_ist(2026, 8, 31, 11, 30),
                 dispatched=False, dry_run=True)
    state3 = load_state(db, customer_id=cust, payment_id=pay, config=cfg, now=NOON)
    assert state3.contact_actions_in_window == 3  # 2 debit + 1 dry_run

    # --- ...but a 'blocked' contact row does NOT ---
    record_spend(event_id="e_blk", customer_id=cust, payment_id=pay,
                 rung=LADDER["sms"], db_path=db, now=_ist(2026, 8, 31, 11, 45),
                 dispatched=False, dry_run=False)
    state4 = load_state(db, customer_id=cust, payment_id=pay, config=cfg, now=NOON)
    assert state4.contact_actions_in_window == 3  # unchanged: 'blocked' excluded


# ============================================================ 5. quiet_hours
def test_quiet_hours_blocks_sms():
    state = GuardrailState(config=GuardrailConfig())

    r_sms = evaluate_all(_event(), LADDER["sms"], state, NIGHT)
    assert "quiet_hours" in r_sms.blocked_by
    assert r_sms.terminal == "BLOCKED/QUIET_HOURS"

    # whatsapp / agent_call also held at night
    assert "quiet_hours" in evaluate_all(_event(), LADDER["whatsapp"], state, NIGHT).blocked_by
    assert "quiet_hours" in evaluate_all(_event(), LADDER["agent_call"], state, NIGHT).blocked_by

    # email and retry_silent pass during quiet hours
    for name in ("email", "retry_silent"):
        rep = evaluate_all(_event(), LADDER[name], state, NIGHT)
        assert "quiet_hours" not in rep.blocked_by
        assert not rep.blocked

    # sms is fine in daylight, and exactly at the 09:00 IST re-open boundary
    assert "quiet_hours" not in evaluate_all(_event(), LADDER["sms"], state, NOON).blocked_by
    assert "quiet_hours" not in evaluate_all(
        _event(), LADDER["sms"], state, _ist(2026, 8, 31, 9, 0)
    ).blocked_by
    # ...and blocked again exactly at 21:00 IST
    assert "quiet_hours" in evaluate_all(
        _event(), LADDER["sms"], state, _ist(2026, 8, 31, 21, 0)
    ).blocked_by

    # a UTC-offset timestamp is converted to IST first: 18:00Z == 23:30 IST
    assert "quiet_hours" in evaluate_all(
        _event(), LADDER["sms"], state, "2026-08-31T18:00:00+00:00"
    ).blocked_by


# ============================================================ 6. spend_cap
def test_spend_cap_blocks_when_exhausted(tmp_path):
    cfg = GuardrailConfig(spend_cap_inr=500.0)

    # pure-predicate view
    at_cap = GuardrailState(config=cfg, spent_today_inr=500.0)
    r = evaluate_all(_event(), LADDER["email"], at_cap, NOON)  # 500.00 + 0.10 > 500
    assert "spend_cap" in r.blocked_by
    assert r.terminal == "BLOCKED/SPEND_CAP"

    room = GuardrailState(config=cfg, spent_today_inr=400.0)
    assert "spend_cap" not in evaluate_all(_event(), LADDER["email"], room, NOON).blocked_by

    # DB-backed: real debits accumulate; a debit on another IST day does not count
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)
    record_spend(event_id="e1", customer_id="c", payment_id="p", rung=LADDER["agent_call"],
                 db_path=db, now=NOON, dispatched=True, dry_run=False)          # +42.00 today
    record_spend(event_id="e_dry", customer_id="c", payment_id="p", rung=LADDER["agent_call"],
                 db_path=db, now=NOON, dispatched=False, dry_run=True)          # +0.00 (dry-run)
    record_spend(event_id="e_yday", customer_id="c", payment_id="p", rung=LADDER["agent_call"],
                 db_path=db, now=_ist(2026, 8, 30, 12, 0), dispatched=True, dry_run=False)  # other day
    assert spent_today_inr(db, now=NOON) == 42.0

    st = load_state(db, customer_id="c", payment_id="p2", config=cfg, now=NOON)
    assert "spend_cap" not in evaluate_all(_event(), LADDER["agent_call"], st, NOON).blocked_by

    # push today's real debits over the cap
    for i in range(11):  # 11 * 42 = 462, running total 504 > 500
        record_spend(event_id=f"e_bulk_{i}", customer_id="c", payment_id="p", rung=LADDER["agent_call"],
                     db_path=db, now=NOON, dispatched=True, dry_run=False)
    assert spent_today_inr(db, now=NOON) == 504.0
    st2 = load_state(db, customer_id="c", payment_id="p3", config=cfg, now=NOON)
    assert "spend_cap" in evaluate_all(_event(), LADDER["retry_silent"], st2, NOON).blocked_by


# ============================================================ 7. dry_run
def test_dry_run_does_not_debit_spend(tmp_path):
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)
    cfg = GuardrailConfig(dry_run=True)
    state = GuardrailState(config=cfg)

    report = evaluate_all(_event(), LADDER["email"], state, NOON)
    assert not report.blocked                    # dry_run NEVER blocks
    assert "dry_run" not in report.blocked_by
    assert report.dispatched is False            # ...but nothing is dispatched

    amount = record_spend(
        event_id="e1", customer_id="cust_x", payment_id="pay_x", rung=LADDER["email"],
        db_path=db, now=NOON, dispatched=report.dispatched, dry_run=cfg.dry_run,
    )
    assert amount == 0.00
    assert spent_today_inr(db, now=NOON) == 0.0  # no real debit

    (row,) = _ledger_rows(db)
    assert row["status"] == "dry_run"
    assert row["amount_inr"] == 0.0

    # contrast: a real dispatch of the same rung debits its cost
    record_spend(event_id="e2", customer_id="cust_x", payment_id="pay_x", rung=LADDER["email"],
                 db_path=db, now=NOON, dispatched=True, dry_run=False)
    assert spent_today_inr(db, now=NOON) == pytest.approx(LADDER["email"]["cost_inr"])


# ============================================ 8. no short-circuit: both recorded
def test_quiet_hours_and_spend_cap_both_recorded(tmp_path):
    """The canary against a future short-circuit. An event at 22:30 IST with
    the spend ledger already at the daily cap: both quiet_hours and spend_cap
    must block, the terminal is BLOCKED/QUIET_HOURS by precedence, and the
    persisted evaluation still has all seven rows with BOTH blockers at 1."""
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)
    cfg = GuardrailConfig(spend_cap_inr=500.0)

    # spend ledger already AT the cap for this IST day (one explicit debit row)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO spend_ledger "
        "(event_id, customer_id, payment_id, rung, amount_inr, status, ist_day, ts) "
        "VALUES ('seed', 'cust_seed', 'pay_seed', 'agent_call', 500.0, 'debit', ?, ?)",
        (g.ist_day(NIGHT), NIGHT),
    )
    con.commit()
    con.close()
    assert spent_today_inr(db, now=NIGHT) == 500.0

    state = load_state(db, customer_id="cust_v", payment_id="pay_v", config=cfg, now=NIGHT)
    report = evaluate_all(_event("cust_v", "pay_v"), LADDER["sms"], state, NIGHT)

    # precedence: quiet_hours (index 4) beats spend_cap (index 5)
    assert report.terminal == "BLOCKED/QUIET_HOURS"
    assert len(report.blocked_by) == 2
    assert set(report.blocked_by) == {"quiet_hours", "spend_cap"}
    assert report.blocked_by == ["quiet_hours", "spend_cap"]  # fixed order

    record_evaluation(report, event_id="evt_v", customer_id="cust_v", db_path=db, ts=NIGHT)
    rows = _eval_rows(db, "evt_v")
    assert len(rows) == 7                                     # seven rows, always
    by_name = {r["guardrail_name"]: r for r in rows}
    assert set(by_name) == set(GUARDRAIL_ORDER)
    assert by_name["quiet_hours"]["blocked"] == 1
    assert by_name["spend_cap"]["blocked"] == 1
    assert sum(r["blocked"] for r in rows) == 2               # exactly those two
    assert by_name["kill_switch"]["blocked"] == 0             # non-firing guardrails recorded too
    assert by_name["quiet_hours"]["reason"] and by_name["spend_cap"]["reason"]
    assert json.loads(by_name["spend_cap"]["detail_json"])["cap_inr"] == 500.0

    # the ladder walk records EVERY rung it tries, never collapsed
    outcome = walk_ladder(
        _event("cust_v", "pay_v"),
        [LADDER[n] for n in ("agent_call", "whatsapp", "sms", "email", "retry_silent")],
        state, NIGHT,
    )
    # email escapes quiet_hours but not the exhausted spend cap; retry_silent
    # (cost 0.05) also tips the cap -> every rung blocked
    assert outcome.blocked
    assert outcome.terminal == "BLOCKED/QUIET_HOURS"          # PRIMARY of the highest rung tried
    assert len(outcome.attempts) == 5
    assert [a.rung for a in outcome.attempts] == ["agent_call", "whatsapp", "sms", "email", "retry_silent"]


# ================================ 9. arm integrity: treatment_blocked excluded
def _mini_ground_truth(cids):
    return {
        "meta": {"seed": datagen.DEFAULT_SEED},
        "customers": {
            c: {"customer_id": c, "p_would_pay_anyway": 0.2,
                "p_pay_if_nudged": 0.6, "lift": 0.4}
            for c in cids
        },
    }


def test_treatment_blocked_excluded_from_uplift_denominator():
    seed = datagen.DEFAULT_SEED
    cids = [f"cust_{i:05d}" for i in range(1, 41)]
    rows = [{"customer_id": c, "reason": "expired_card"} for c in cids]
    gt = _mini_ground_truth(cids)

    baseline = run_policy(rows, gt, recover_everything_policy, run_seed=seed)
    treat_ids = [c for c in cids if assign_arm(seed, c) == "treatment"]
    assert len(treat_ids) >= 3 and baseline.control.n >= 1
    assert baseline.treatment_blocked.n == 0  # no blocked_fn -> empty third bucket

    blocked_ids = set(treat_ids[:3])
    guarded = run_policy(
        rows, gt, recover_everything_policy, run_seed=seed,
        blocked_fn=lambda r: r["customer_id"] in blocked_ids,
    )

    # arm assignment is untouched: the control bucket is byte-identical, and the
    # three blocked customers came out of treatment, never reclassified as control
    assert guarded.control.n == baseline.control.n
    assert guarded.control.n_recovered == baseline.control.n_recovered
    assert guarded.treatment_blocked.n == 3
    assert guarded.treatment.n == baseline.treatment.n - 3
    assert guarded.treatment.n + guarded.treatment_blocked.n + guarded.control.n == len(rows)

    # excluded from the uplift denominator == identical to physically dropping them
    trimmed = run_policy(
        [r for r in rows if r["customer_id"] not in blocked_ids],
        gt, recover_everything_policy, run_seed=seed,
    )
    assert guarded.uplift == trimmed.uplift
    assert guarded.treatment.n == trimmed.treatment.n
    assert (guarded.ci_low, guarded.ci_high) == (trimmed.ci_low, trimmed.ci_high)

    # treatment_blocked is still reported (resolved under the untouched baseline)
    assert 0 <= guarded.treatment_blocked.n_recovered <= 3
