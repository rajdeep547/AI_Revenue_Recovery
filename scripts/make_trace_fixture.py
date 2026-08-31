"""Regenerate ``tests/fixtures/trace_demo.db`` from scratch.

This is the ONLY module in Slice 12 permitted to write SQLite. It writes
exactly one path -- ``tests/fixtures/trace_demo.db`` -- and:

* builds the schema through the real production init functions
  (``app.db.init_db``, ``app.decision.store.init_decision_store``,
  ``app.guardrails.init_guardrail_store``) so the fixture cannot drift from
  the production schema;
* derives every decision's arithmetic from the real engine
  (``app.decision.engine.decide_with_ladder``) and every guardrail row from
  the real guardrail walk (``app.pipeline._apply_guardrails``), so the
  fixture cannot drift from production behaviour either;
* refuses to run if the target ever resolves to the live webhook DB
  (``WEBHOOK_DB_PATH`` / ``webhook_events.db``).

Never edit the ``.db`` by hand -- rerun this. The file is git-ignored.

    python scripts/make_trace_fixture.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.arms import assign_arm  # noqa: E402
from app.db import init_db  # noqa: E402
from app.decision.engine import Decision, SkipReason, decide_with_ladder, load_policy  # noqa: E402
from app.decision.rationale import render as render_rationale  # noqa: E402
from app.decision.store import init_decision_store, record_decision  # noqa: E402
from app.guardrails import init_guardrail_store  # noqa: E402
from app.ingest import Ingestor  # noqa: E402  (creates normalized_events / rejected_events)
from app import pipeline  # noqa: E402  (real _apply_guardrails, anti-drift)

DEFAULT_TARGET = REPO / "tests" / "fixtures" / "trace_demo.db"


# --------------------------------------------------------------------- guard
def resolve_target(argv: list[str]) -> Path:
    """The path to (re)build. ``argv[1]`` overrides the default. Aborts (exit
    1, nothing written) if that path resolves to the live webhook DB or is
    named ``webhook_events.db``."""
    target = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_TARGET.resolve()

    forbidden = {Path("webhook_events.db").resolve()}
    env = os.environ.get("WEBHOOK_DB_PATH")
    if env:
        forbidden.add(Path(env).resolve())

    if target in forbidden or target.name == "webhook_events.db":
        raise SystemExit(
            f"ABORT: refusing to write {target} -- it resolves to the live "
            f"webhook DB (WEBHOOK_DB_PATH={env!r}). This script only ever "
            f"writes tests/fixtures/trace_demo.db."
        )
    return target


# --------------------------------------------------------------- id selection
def _pick(seed: int, tag: str, arm: str) -> str:
    """First ``fixture_<tag>_NNN`` id whose real ``assign_arm`` result is
    ``arm``. We never write a row that contradicts the hash -- we search for
    an id that already lands in the arm the shape needs."""
    for i in range(10_000):
        cid = f"fixture_{tag}_{i:03d}"
        if assign_arm(seed, cid) == arm:
            return cid
    raise RuntimeError(f"no {arm} id found for tag {tag!r}")


# ------------------------------------------------------------------ raw insert
def _norm(db: str, pid: str, cid: str, *, email, phone, amount_paise, at, method="card"):
    con = sqlite3.connect(db)
    with con:
        con.execute(
            "INSERT INTO normalized_events (event_id, source, customer_id, email, "
            "phone, amount_paise, currency, method, reason, occurred_at, reference, "
            "raw, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"card_failure:{pid}", "card_failure", cid, email, phone, amount_paise,
             "INR", method, "payment_failed", at, pid, "{}", at),
        )
    con.close()


def _event(db: str, pid: str, *, amount_paise, method, at):
    con = sqlite3.connect(db)
    with con:
        con.execute(
            "INSERT INTO events (payment_id, amount, method, status, first_event_id, "
            "last_event_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, amount_paise, method, "failed", f"evt:{pid}", f"evt:{pid}", at, at),
        )
    con.close()


def _audit_ingested(db: str, pid: str, *, at):
    con = sqlite3.connect(db)
    with con:
        con.execute(
            "INSERT INTO audit (payment_id, event_id, action, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (pid, f"card_failure:{pid}", "ingested",
             json.dumps({"event": "payment.failed"}), at),
        )
    con.close()


def _spend_debit(db: str, *, pid, cid, rung, amount_inr, ist_day, ts):
    con = sqlite3.connect(db)
    with con:
        con.execute(
            "INSERT INTO spend_ledger (event_id, customer_id, payment_id, rung, "
            "amount_inr, status, ist_day, ts) VALUES (?,?,?,?,?,?,?,?)",
            (f"card_failure:{pid}", cid, pid, rung, amount_inr, "debit", ist_day, ts),
        )
    con.close()


# --------------------------------------------------------------------- build
def build(target: Path) -> list[dict]:
    """(Re)create ``target`` and return a manifest, one dict per shape."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    db = str(target)

    # Production schema, verbatim -- every table is created by the same init
    # code the live app runs, so the fixture cannot drift.
    init_db(db)
    init_decision_store(db)
    init_guardrail_store(db)
    Ingestor(db).close()  # normalized_events + rejected_events

    policy = load_policy()
    seed = policy["experiment_seed"]
    hist = {"last_contact_at": None, "prior_recoveries": []}
    manifest: list[dict] = []

    def _persist_plain(decision, pid, at):
        record_decision(decision, event_id=f"card_failure:{pid}", db_path=db, now=at)

    def _row(shape, pid, cid, decision, at, *, note=""):
        manifest.append({
            "shape": shape, "payment_id": pid, "customer_id": cid,
            "arm": assign_arm(seed, cid), "terminal": decision.terminal,
            "skip_reason": decision.skip_reason.name if decision.skip_reason else None,
            "action": decision.action, "note": note,
        })

    # -- 1. SKIP / EV_BELOW_FLOOR, null action (mirror of the live row) --------
    tag = "evfloor"
    cid = _pick(seed, tag, "treatment")           # EV floor is checked only in
    pid = "pay_FIXTURE_ev_below_floor"            # the treatment path; control
    at = "2026-08-30T19:32:26+00:00"              # would short-circuit to
    ev = {"payment_id": pid, "cause": "insufficient_funds", "cause_confidence": 0.32,
          "ticket_inr": 10.0, "email": f"{cid}@fixture.test", "phone": "+910000000001",
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=ev["phone"], amount_paise=1000, at=at)
    _event(db, pid, amount_paise=1000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-08-30T19:31:00+00:00")
    dec, _ = decide_with_ladder(ev, policy, assign_arm(seed, cid), hist)
    _persist_plain(dec, pid, at)
    _row("SKIP/EV_BELOW_FLOOR", pid, cid, dec, at)

    # -- 2. SKIP / CONTROL_ARM, control customer, EV arithmetic populated -----
    tag = "control"
    cid = _pick(seed, tag, "control")
    pid = "pay_FIXTURE_control_arm"
    at = "2026-08-30T20:00:00+00:00"
    ev = {"payment_id": pid, "cause": "insufficient_funds", "cause_confidence": 0.32,
          "ticket_inr": 800.0, "email": f"{cid}@fixture.test", "phone": "+910000000002",
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=ev["phone"], amount_paise=80000, at=at)
    _event(db, pid, amount_paise=80000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-08-30T19:59:00+00:00")
    dec, _ = decide_with_ladder(ev, policy, "control", hist)
    _persist_plain(dec, pid, at)
    _row("SKIP/CONTROL_ARM", pid, cid, dec, at, note="shadow_action populated")

    # -- 3. ACT / email, treatment, all 7 guardrails passing -----------------
    #    Real-world ACT rows are email: the customer has an email but no phone,
    #    so sms/whatsapp/agent_call are channel-ineligible and email wins the
    #    two-rung ladder. now is well outside quiet hours and no prior spend.
    tag = "act"
    cid = _pick(seed, tag, "treatment")
    pid = "pay_FIXTURE_act_email"
    at = "2026-03-15T12:00:00+00:00"
    ev = {"payment_id": pid, "cause": "insufficient_funds", "cause_confidence": 0.32,
          "ticket_inr": 1630.0, "email": f"{cid}@fixture.test", "phone": None,
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=None, amount_paise=163000, at=at)
    _event(db, pid, amount_paise=163000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-03-15T11:59:00+00:00")
    dec, ranked = decide_with_ladder(ev, policy, assign_arm(seed, cid), hist)
    assert dec.terminal == "ACT" and dec.action == "email", (dec.terminal, dec.action)
    dec = pipeline._apply_guardrails(
        dec, ranked, ev, event_id=f"card_failure:{pid}", customer_id=cid,
        db_path=db, now_utc=at,
    )
    _persist_plain(dec, pid, at)
    _row("ACT/email", pid, cid, dec, at, note="7 guardrail_evaluations rows, all pass")

    # -- 4. ROUTE_TO_HUMAN, high ticket, low confidence, policy_override -----
    tag = "route"
    cid = _pick(seed, tag, "treatment")
    pid = "pay_FIXTURE_route_to_human"
    at = "2026-08-30T21:00:00+00:00"
    ev = {"payment_id": pid, "cause": "insufficient_funds", "cause_confidence": 0.32,
          "ticket_inr": 20000.0, "email": f"{cid}@fixture.test", "phone": "+910000000004",
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=ev["phone"], amount_paise=2000000, at=at)
    _event(db, pid, amount_paise=2000000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-08-30T20:59:00+00:00")
    dec, _ = decide_with_ladder(ev, policy, assign_arm(seed, cid), hist)
    assert dec.terminal == "ROUTE_TO_HUMAN" and dec.gate_basis == "policy_override", dec
    _persist_plain(dec, pid, at)
    _row("ROUTE_TO_HUMAN", pid, cid, dec, at, note="ticket>=floor AND confidence<ceiling")

    # -- 5. BLOCKED / QUIET_HOURS -------------------------------------------
    #    now is 05:30 IST (inside quiet hours) so sms/whatsapp/agent_call are
    #    blocked by quiet_hours; a same-day debit of Rs 499.96 leaves email and
    #    retry_silent blocked by spend_cap (order 5, after quiet_hours order 4),
    #    so the EV-best rung (whatsapp) blocks primarily on quiet_hours and the
    #    whole ladder falls through to BLOCKED/QUIET_HOURS.
    tag = "blocked"
    cid = _pick(seed, tag, "treatment")
    pid = "pay_FIXTURE_blocked_quiet"
    at = "2026-02-01T00:00:00+00:00"               # 2026-02-01 05:30 IST
    _spend_debit(db, pid="pay_FIXTURE_capfill", cid="fixture_capfill", rung="whatsapp",
                 amount_inr=499.96, ist_day="2026-02-01", ts=at)
    ev = {"payment_id": pid, "cause": "insufficient_funds", "cause_confidence": 0.32,
          "ticket_inr": 500.0, "email": f"{cid}@fixture.test", "phone": "+910000000005",
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=ev["phone"], amount_paise=50000, at=at)
    _event(db, pid, amount_paise=50000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-01-31T23:59:00+00:00")
    dec, ranked = decide_with_ladder(ev, policy, assign_arm(seed, cid), hist)
    assert dec.terminal == "ACT", dec.terminal
    dec = pipeline._apply_guardrails(
        dec, ranked, ev, event_id=f"card_failure:{pid}", customer_id=cid,
        db_path=db, now_utc=at,
    )
    assert dec.terminal == "BLOCKED/QUIET_HOURS", dec.terminal
    _persist_plain(dec, pid, at)
    _row("BLOCKED/QUIET_HOURS", pid, cid, dec, at, note="35 guardrail rows (5 rungs x 7)")

    # -- 6. SKIP / NO_CONTACT_CHANNEL ------------------------------------
    #    The live engine can never emit this today: retry_silent has
    #    requires_channel=null, so _any_channel_satisfiable is always true.
    #    It is still a real terminal the schema and the rationale renderer
    #    support, so we hand-build the pre-ladder-skip shape (every EV field
    #    NULL, gate_basis 'hard_gate') exactly as engine._pre_ladder_skip
    #    would, and render its canonical sentence.
    tag = "nochan"
    cid = _pick(seed, tag, "treatment")
    pid = "pay_FIXTURE_no_channel"
    at = "2026-08-30T22:00:00+00:00"
    _norm(db, pid, cid, email=None, phone=None, amount_paise=120000, at=at)
    _event(db, pid, amount_paise=120000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-08-30T21:59:00+00:00")
    dec = Decision(
        payment_id=pid, policy_version=policy["policy_version"], terminal="SKIP",
        action=None, skip_reason=SkipReason.NO_CONTACT_CHANNEL,
        cause="insufficient_funds", cause_confidence=0.32,
        p_incremental_prior=float(policy["incremental_priors"]["insufficient_funds"]["p_incremental"]),
        p_effective=None, p_action_basis=None, p_lower_bound=None,
        history_multiplier=1.0, ticket_inr=1200.0, action_cost_inr=None,
        ev_inr=None, ev_lower_inr=None, shadow_action=None, gate_basis="hard_gate",
        route_ticket_floor_inr=None, route_confidence_ceiling=None,
        rationale="", inputs_hash="fixture_no_contact_channel_hand_built",
    )
    dec = dataclasses.replace(dec, rationale=render_rationale(dec))
    _persist_plain(dec, pid, at)
    _row("SKIP/NO_CONTACT_CHANNEL", pid, cid, dec, at, note="hand-built pre-ladder skip")

    # -- 7. LLM-path decision, cause = expired_card ----------------------
    #    expired_card is emitted only by the LLM diagnoser (never the rules
    #    map), and LLM-sourced diagnoses cannot drive a paid action while
    #    TAIL_ACT_ENABLED is False -- they are recorded and routed to a human
    #    queue. We compute the arithmetic with the engine, then stamp the
    #    terminal/gate_basis the llm_diagnosis hard-gate would impose.
    tag = "llm"
    cid = _pick(seed, tag, "treatment")
    pid = "pay_FIXTURE_llm_expired_card"
    at = "2026-08-30T23:00:00+00:00"
    ev = {"payment_id": pid, "cause": "expired_card", "cause_confidence": 0.80,
          "ticket_inr": 1200.0, "email": f"{cid}@fixture.test", "phone": None,
          "now_utc": at}
    _norm(db, pid, cid, email=ev["email"], phone=None, amount_paise=120000, at=at)
    _event(db, pid, amount_paise=120000, method="card", at=at)
    _audit_ingested(db, pid, at="2026-08-30T22:59:00+00:00")
    dec, _ = decide_with_ladder(ev, policy, assign_arm(seed, cid), hist)
    dec = dataclasses.replace(
        dec,
        terminal="ROUTE_TO_HUMAN",
        action=dec.action or "email",
        skip_reason=None,
        gate_basis="llm_gated",
        rationale=(
            "LLM-classified failure cause (expired_card): routed to a human "
            "queue because an LLM-sourced diagnosis cannot trigger a paid "
            "action while TAIL_ACT_ENABLED is False."
        ),
    )
    _persist_plain(dec, pid, at)
    _row("ROUTE_TO_HUMAN (LLM path)", pid, cid, dec, at, note="cause=expired_card")

    return manifest


def main(argv: list[str]) -> int:
    target = resolve_target(argv)
    manifest = build(target)
    print(f"wrote {target}  ({len(manifest)} decisions)\n")
    for m in manifest:
        print(
            f"  {m['shape']:26s}  {m['payment_id']:30s}  {m['customer_id']:24s}  "
            f"{m['arm']:9s}  {m['terminal']}"
            + (f"  -- {m['note']}" if m["note"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
