"""Slice 9c - verification only. Exactly one test: a treatment-arm event at
22:30 IST whose EV-best rung is whatsapp walks down past the quiet-hours block
to email, and the full per-rung guardrail log is written (never collapsed).
"""

from __future__ import annotations

import sqlite3

from app import pipeline
from app.arms import assign_arm
from app.decision.engine import Terminal, load_policy
from app.ingest import Ingestor

POLICY = load_policy()
SEED = POLICY["experiment_seed"]

# 22:30 IST, inside quiet hours (21:00-09:00 IST). Explicit offset -- no wall clock.
NOW_2230_IST = "2026-08-31T22:30:00+05:30"


def _treatment_customer() -> str:
    for i in range(1, 20000):
        cid = f"cust_{i:05d}"
        if assign_arm(SEED, cid) == "treatment":
            return cid
    raise AssertionError("no treatment customer found")  # pragma: no cover


def _card_failure(payment_id, customer_id, amount_paise, phone):
    return {
        "event": "payment.failed",
        "created_at": 1735689654,
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": amount_paise, "currency": "INR",
            "method": "card", "status": "failed",
            "error_code": "BAD_REQUEST_ERROR", "error_description": "diagnostic text",
            "error_reason": "insufficient_funds",
            "notes": {"customer_id": customer_id,
                      "email": f"{customer_id}@example.test", "contact": phone},
        }}},
    }


def test_walk_down_produces_lower_rung_and_full_log(tmp_path):
    db = tmp_path / "w.db"
    cust = _treatment_customer()
    # Rs 200 + a phone -> EV order whatsapp > sms > email > retry_silent > agent_call,
    # so the engine's EV-best rung is whatsapp and it terminates ACT.
    with Ingestor(str(db)) as ing:
        res = ing.ingest("card_failure", _card_failure("pay_wd", cust, 20000, "+919000000009"))
        assert res.reason_code is None, res

    d = pipeline.process_failure("pay_wd", db_path=str(db), policy=POLICY, now_utc=NOW_2230_IST)
    assert d.terminal == Terminal.ACT

    con = sqlite3.connect(str(db))
    try:
        # (1) the recorded decision walked down to email
        drows = con.execute(
            "SELECT action, rationale FROM decisions WHERE payment_id = 'pay_wd'"
        ).fetchall()
        assert len(drows) == 1
        action, rationale = drows[0]
        assert action == "email"

        # (2) three rungs tried, seven guardrails each, never collapsed -> 21 rows
        grows = con.execute(
            "SELECT rung, guardrail_name, blocked FROM guardrail_evaluations "
            "WHERE event_id = 'card_failure:pay_wd' ORDER BY id"
        ).fetchall()
        assert len(grows) == 21

        by_rung: dict[str, list[tuple[str, int]]] = {}
        for rung, name, blocked in grows:
            by_rung.setdefault(rung, []).append((name, blocked))
        assert set(by_rung) == {"whatsapp", "sms", "email"}
        assert all(len(group) == 7 for group in by_rung.values())  # own seven-row group each

        # (3) whatsapp AND sms each recorded blocked=1 (on quiet_hours), in their own group
        assert dict(by_rung["whatsapp"])["quiet_hours"] == 1
        assert dict(by_rung["sms"])["quiet_hours"] == 1
        assert sum(b for _, b in by_rung["whatsapp"]) >= 1
        assert sum(b for _, b in by_rung["sms"]) >= 1
        assert sum(b for _, b in by_rung["email"]) == 0        # email survived the walk

        # (4) ev_best_rung is recoverable from the rationale text alone, no column
        assert "EV-best rung whatsapp" in rationale
        assert "walked down to email" in rationale

        # (5) exactly one spend_ledger row: dry_run, for the walked-to rung (email), not whatsapp
        lrows = con.execute("SELECT rung, status, amount_inr FROM spend_ledger").fetchall()
        assert lrows == [("email", "dry_run", 0.0)]
    finally:
        con.close()
