"""Slice 9b - wiring the guardrails into the runtime decision path.

Covers: guardrails run only after an ACT and never override a pre-ladder skip;
the walk records every rung tried; a walk that lands on a lower rung records
both rungs; a fully blocked walk becomes a BLOCKED/<PRIMARY> terminal; caps
count commitments; and measurement derives a real treatment_blocked bucket
from the guardrail log. All clocks frozen explicitly.
"""

from __future__ import annotations

import sqlite3

import pytest

import datagen
from app import guardrails
from app.arms import assign_arm
from app.decision.engine import SkipReason, Terminal, load_policy
from app.guardrails import (
    GuardrailConfig,
    GuardrailState,
    add_suppression,
    init_guardrail_store,
    record_ladder_walk,
    walk_ladder,
)
from app.ingest import Ingestor
from app import pipeline
from eval.measurement import (
    blocked_customers_from_guardrail_log,
    recover_everything_policy,
    run_policy,
)

POLICY = load_policy()
SEED = POLICY["experiment_seed"]
LADDER = {r["name"]: r for r in POLICY["action_ladder"]}

NOON_IST = "2026-08-31T12:00:00+05:30"     # daytime IST: no time-based guardrail
NIGHT_IST = "2026-08-31T18:00:00+00:00"    # 23:30 IST: inside quiet hours


# --------------------------------------------------------------------- helpers
def _first_customer(arm: str) -> str:
    for i in range(1, 20000):
        cid = f"cust_{i:05d}"
        if assign_arm(SEED, cid) == arm:
            return cid
    raise AssertionError(f"no {arm} customer found")  # pragma: no cover


def _payload(payment_id, customer_id, amount_paise, *, phone=None,
             error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds"):
    notes = {"customer_id": customer_id, "email": f"{customer_id}@example.test"}
    if phone is not None:
        notes["contact"] = phone
    return {
        "event": "payment.failed",
        "created_at": 1735689654,
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": amount_paise, "currency": "INR",
            "method": "card", "status": "failed",
            "error_code": error_code, "error_description": "diagnostic text",
            "error_reason": error_reason, "notes": notes,
        }}},
    }


def _ingest(db, *payloads):
    with Ingestor(str(db)) as ing:
        for p in payloads:
            res = ing.ingest("card_failure", p)
            assert res.reason_code is None, res


def _eval_rows(db, event_id):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        try:
            return [
                dict(r) for r in con.execute(
                    "SELECT * FROM guardrail_evaluations WHERE event_id = ? ORDER BY id",
                    (event_id,),
                )
            ]
        except sqlite3.OperationalError:
            return []
    finally:
        con.close()


def _ledger_rows(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        try:
            return [dict(r) for r in con.execute("SELECT * FROM spend_ledger ORDER BY id")]
        except sqlite3.OperationalError:
            return []
    finally:
        con.close()


def _rungs_in(rows):
    seen = []
    for r in rows:
        if r["rung"] not in seen:
            seen.append(r["rung"])
    return seen


# ============================================ clean ACT on the EV-best rung
def test_clean_act_keeps_decision_but_records_the_walk(tmp_path):
    db = tmp_path / "w.db"
    cust = _first_customer("treatment")
    _ingest(db, _payload("pay_clean", cust, 20000))  # Rs 200, email-only

    d = pipeline.process_failure("pay_clean", db_path=str(db), policy=POLICY, now_utc=NOON_IST)

    assert d.terminal == Terminal.ACT
    assert d.action == "email"                         # the EV-best rung
    assert d.gate_basis == "expected_value"
    assert "GUARDRAILS" not in d.rationale             # nothing amended

    rows = _eval_rows(db, "card_failure:pay_clean")
    assert len(rows) == 7                              # one rung tried, seven guardrails
    assert _rungs_in(rows) == ["email"]

    ledger = _ledger_rows(db)
    assert len(ledger) == 1
    assert ledger[0]["rung"] == "email"
    assert ledger[0]["status"] == "dry_run"            # no transport yet -> commitment only
    assert ledger[0]["amount_inr"] == 0.0


# ============================================ walk down to a lower rung
def test_walk_down_to_lower_rung_records_both_and_why(tmp_path):
    db = tmp_path / "w.db"
    cust = _first_customer("treatment")
    # Rs 200 with a phone -> EV order whatsapp > sms > email > retry_silent;
    # at 23:30 IST quiet_hours blocks whatsapp + sms, email is the walked-to rung.
    _ingest(db, _payload("pay_walk", cust, 20000, phone="+919000000001"))

    d = pipeline.process_failure("pay_walk", db_path=str(db), policy=POLICY, now_utc=NIGHT_IST)

    assert d.terminal == Terminal.ACT
    assert d.action == "email"                         # walked down from whatsapp
    assert "whatsapp" in d.rationale                   # the EV-best rung is named
    assert "quiet_hours" in d.rationale                # ...and why it fell
    assert "walked down to email" in d.rationale

    rows = _eval_rows(db, "card_failure:pay_walk")
    assert _rungs_in(rows) == ["whatsapp", "sms", "email"]   # walk order, not collapsed
    assert len(rows) == 21                                    # 3 rungs x 7 guardrails
    # the higher rungs are recorded as blocked, the walked-to rung is not
    by_rung_blocked = {}
    for r in rows:
        by_rung_blocked.setdefault(r["rung"], 0)
        by_rung_blocked[r["rung"]] += r["blocked"]
    assert by_rung_blocked["whatsapp"] >= 1 and by_rung_blocked["sms"] >= 1
    assert by_rung_blocked["email"] == 0

    ledger = _ledger_rows(db)
    assert [ (x["rung"], x["status"]) for x in ledger ] == [("email", "dry_run")]

    # idempotent: a second call adds no rows and returns an equal Decision
    d2 = pipeline.process_failure("pay_walk", db_path=str(db), policy=POLICY,
                                  now_utc="2027-01-01T00:00:00+00:00")
    assert d2 == d
    assert len(_eval_rows(db, "card_failure:pay_walk")) == 21
    assert len(_ledger_rows(db)) == 1


# ============================================ every rung blocked -> BLOCKED/<PRIMARY>
def test_all_rungs_blocked_becomes_blocked_terminal_no_action(tmp_path):
    db = tmp_path / "w.db"
    cust = _first_customer("treatment")
    _ingest(db, _payload("pay_blk", cust, 5000))       # Rs 50, email-only -> ranked [email, retry_silent]

    # seed guardrail state: customer suppressed AND the day's spend already at cap
    init_guardrail_store(str(db))
    add_suppression(str(db), cust, now=NOON_IST, reason="test")
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spend_ledger "
        "(event_id, customer_id, payment_id, rung, amount_inr, status, ist_day, ts) "
        "VALUES ('seed', ?, 'pay_seed', 'agent_call', 500.0, 'debit', ?, ?)",
        (cust, guardrails.ist_day(NOON_IST), NOON_IST),
    )
    con.commit()
    con.close()

    d = pipeline.process_failure("pay_blk", db_path=str(db), policy=POLICY, now_utc=NOON_IST)

    # email blocked by opt_out (+ spend_cap); retry_silent blocked by spend_cap.
    # PRIMARY of the highest rung tried (email) is opt_out.
    assert d.terminal == "BLOCKED/OPT_OUT"
    assert d.action is None
    assert d.gate_basis == "guardrail"
    assert "every rung blocked" in d.rationale

    rows = _eval_rows(db, "card_failure:pay_blk")
    assert _rungs_in(rows) == ["email", "retry_silent"]
    assert len(rows) == 14                              # 2 rungs x 7, still recorded in full
    assert sum(r["blocked"] for r in rows if r["rung"] == "email") == 2   # opt_out + spend_cap

    # no commitment row written on a full block (only the seeded cap row remains)
    assert [x["event_id"] for x in _ledger_rows(db)] == ["seed"]

    d2 = pipeline.process_failure("pay_blk", db_path=str(db), policy=POLICY, now_utc=NIGHT_IST)
    assert d2 == d
    assert len(_eval_rows(db, "card_failure:pay_blk")) == 14


# ============================================ Part 4: pre-ladder skip is untouched
def test_ev_below_floor_skips_before_the_ladder_and_never_reaches_guardrails(tmp_path):
    """Regression pin: insufficient_funds, Rs 10.00, best rung sms, and the EV
    (Rs ~0.46) is below the Rs 2.00 floor. The engine skips at stage (h),
    BEFORE the guardrail walk -- guardrails must not turn that skip into an
    act, and must not touch it at all."""
    db = tmp_path / "w.db"
    cust = _first_customer("treatment")
    _ingest(db, _payload("pay_floor", cust, 1000, phone="+919000000002"))  # Rs 10, phone -> sms best

    d = pipeline.process_failure("pay_floor", db_path=str(db), policy=POLICY, now_utc=NIGHT_IST)

    assert d.terminal == Terminal.SKIP
    assert d.skip_reason is SkipReason.EV_BELOW_FLOOR
    assert d.action is None
    assert d.gate_basis == "expected_value"
    assert "GUARDRAILS" not in d.rationale

    assert _eval_rows(db, "card_failure:pay_floor") == []   # walk never ran
    assert _ledger_rows(db) == []


def test_control_arm_skip_never_reaches_guardrails(tmp_path):
    db = tmp_path / "w.db"
    cust = _first_customer("control")
    _ingest(db, _payload("pay_ctl", cust, 20000))

    d = pipeline.process_failure("pay_ctl", db_path=str(db), policy=POLICY, now_utc=NIGHT_IST)

    assert d.terminal == Terminal.SKIP
    assert d.skip_reason is SkipReason.CONTROL_ARM
    assert _eval_rows(db, "card_failure:pay_ctl") == []
    assert _ledger_rows(db) == []


# ============================================ Part 3: blocked_fn from the log
def _mini_gt(cids):
    return {
        "meta": {"seed": SEED},
        "customers": {
            c: {"customer_id": c, "p_would_pay_anyway": 0.2,
                "p_pay_if_nudged": 0.6, "lift": 0.4}
            for c in cids
        },
    }


def test_blocked_fn_derived_from_guardrail_log_feeds_treatment_blocked(tmp_path):
    db = str(tmp_path / "g.db")
    init_guardrail_store(db)

    cids = [f"cust_{i:05d}" for i in range(1, 41)]
    treat = [c for c in cids if assign_arm(SEED, c) == "treatment"]
    blocked_cust, ok_cust = treat[0], treat[1]

    ranked = [LADDER[n] for n in ("email", "retry_silent")]
    ks_state = GuardrailState(config=GuardrailConfig(kill_switch=True))
    open_state = GuardrailState(config=GuardrailConfig())

    out_blocked = walk_ladder({"customer_id": blocked_cust, "payment_id": "p1"},
                              ranked, ks_state, NOON_IST)
    assert out_blocked.blocked
    record_ladder_walk(out_blocked, event_id="evt_b", customer_id=blocked_cust,
                       db_path=db, ts=NOON_IST)

    out_ok = walk_ladder({"customer_id": ok_cust, "payment_id": "p2"},
                         ranked, open_state, NOON_IST)
    assert not out_ok.blocked
    record_ladder_walk(out_ok, event_id="evt_ok", customer_id=ok_cust,
                       db_path=db, ts=NOON_IST)

    found = blocked_customers_from_guardrail_log(db)
    assert found == {blocked_cust}

    # missing table -> empty set, so the caller degrades to blocked_fn=None
    assert blocked_customers_from_guardrail_log(str(tmp_path / "nope.db")) == set()

    rows = [{"customer_id": c, "reason": "expired_card"} for c in cids]
    gt = _mini_gt(cids)
    baseline = run_policy(rows, gt, recover_everything_policy, run_seed=SEED)
    guarded = run_policy(rows, gt, recover_everything_policy, run_seed=SEED,
                         blocked_fn=lambda r: r["customer_id"] in found)

    assert guarded.treatment_blocked.n == 1
    assert guarded.treatment.n == baseline.treatment.n - 1
    assert guarded.control.n == baseline.control.n            # arm assignment untouched
    assert guarded.uplift != baseline.uplift or baseline.treatment_blocked.n == 0
