"""Slice 8 - integration. The pipeline wires diagnose -> assign arm ->
decide -> record; it executes NO action and adds no decision logic.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import datagen
from eval import measurement

import app.arms as app_arms
from app import pipeline
from app.decision.engine import SkipReason, Terminal, load_policy
from app.ingest import Ingestor

REPO = Path(__file__).resolve().parents[1]
POLICY = load_policy()
SEED = POLICY["experiment_seed"]
NOW = "2026-08-31T09:00:00+00:00"


# --------------------------------------------------------------------- helpers
def _card_failure(payment_id, customer_id, amount_paise, *,
                  error_code="GATEWAY_ERROR", error_reason="bank_downtime",
                  created_at=1735689654):
    return {
        "event": "payment.failed",
        "created_at": created_at,
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": amount_paise, "currency": "INR",
            "method": "card", "status": "failed",
            "error_code": error_code, "error_description": "diagnostic text",
            "error_reason": error_reason,
            "notes": {"customer_id": customer_id,
                      "email": f"{customer_id}@example.test"},
        }}},
    }


def _ingest(db_path, *payloads):
    with Ingestor(str(db_path)) as ing:
        for p in payloads:
            res = ing.ingest("card_failure", p)
            assert res.reason_code is None, res


def _first_customer(arm: str) -> str:
    for i in range(1, 5000):
        cid = f"cust_{i:05d}"
        if measurement.assign_arm(SEED, cid) == arm:
            return cid
    raise AssertionError(f"no {arm} customer found")  # pragma: no cover


def _decisions_rows(db_path, payment_id=None):
    con = sqlite3.connect(str(db_path))
    try:
        try:
            sql = "SELECT * FROM decisions"
            args: tuple = ()
            if payment_id is not None:
                sql += " WHERE payment_id = ?"
                args = (payment_id,)
            return con.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []  # table never created -> nothing was written
    finally:
        con.close()


# ==================================================================== BREAK 1
def test_no_ingested_row_returns_none_and_writes_nothing(tmp_path):
    db = tmp_path / "w.db"
    _ingest(db, _card_failure("pay_other", _first_customer("treatment"), 500000))

    d = pipeline.process_failure("pay_absent", db_path=str(db), policy=POLICY, now_utc=NOW)

    assert d is None
    assert _decisions_rows(db) == []


# ==================================================================== BREAK 2
def test_unresolvable_cause_becomes_unknown_and_uses_population_prior(tmp_path):
    db = tmp_path / "w.db"
    _ingest(db, _card_failure("pay_weird", _first_customer("treatment"), 800000,
                              error_code="NOT_A_REAL_CODE", error_reason="mystery"))

    d = pipeline.process_failure("pay_weird", db_path=str(db), policy=POLICY, now_utc=NOW)

    assert d is not None                      # a real Decision, no MissingPrior
    assert d.cause == "unknown"
    assert d.p_incremental_prior == POLICY["incremental_priors"]["unknown"]["p_incremental"]
    assert d.p_incremental_prior == 0.10
    assert d.p_effective == pytest.approx(POLICY["population_incremental"])
    assert d.terminal in (Terminal.ACT, Terminal.SKIP, Terminal.ROUTE_TO_HUMAN)
    assert len(_decisions_rows(db, "pay_weird")) == 1


# ==================================================================== BREAK 3
def test_control_customer_records_control_arm_skip_with_shadow_and_no_action(tmp_path):
    db = tmp_path / "w.db"
    cust = _first_customer("control")
    # ticket under human_review_ticket_inr so the treatment counterfactual is
    # ACT (not a policy-override route) -> shadow_action is populated.
    _ingest(db, _card_failure("pay_ctrl", cust, 600000,
                              error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds"))

    d = pipeline.process_failure("pay_ctrl", db_path=str(db), policy=POLICY, now_utc=NOW)

    assert d.skip_reason is SkipReason.CONTROL_ARM
    assert d.gate_basis == "experiment"
    assert d.shadow_action is not None        # it would have acted in treatment
    assert d.action is None                    # control never acts
    assert len(_decisions_rows(db, "pay_ctrl")) == 1

    # nothing in the pipeline can execute / send / dispatch an action
    offenders = [
        n for n in dir(pipeline)
        if not n.startswith("__")
        and any(k in n.lower() for k in ("send", "execute", "dispatch", "deliver", "nudge"))
    ]
    assert offenders == [], offenders


# ==================================================================== BREAK 4
def test_same_payment_twice_writes_one_row_and_returns_equal_decisions(tmp_path):
    db = tmp_path / "w.db"
    _ingest(db, _card_failure("pay_dup", _first_customer("treatment"), 1200000))

    d1 = pipeline.process_failure("pay_dup", db_path=str(db), policy=POLICY, now_utc=NOW)
    # a different now_utc on the second call must be ignored - the recorded
    # decision is returned as-is.
    d2 = pipeline.process_failure("pay_dup", db_path=str(db), policy=POLICY,
                                  now_utc="2027-03-03T03:03:03+00:00")

    assert len(_decisions_rows(db, "pay_dup")) == 1
    assert d1 == d2
    assert d1.inputs_hash == d2.inputs_hash


# ==================================================================== BREAK 5
def test_pipeline_exception_does_not_break_webhook_ack(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.main as main

    db = tmp_path / "wh.db"
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(db))
    monkeypatch.setattr(main, "verify_signature", lambda *_a, **_k: True)

    def _boom(*_a, **_k):
        raise RuntimeError("decision pipeline blew up")

    monkeypatch.setattr(main.pipeline, "process_failure", _boom)

    body = json.dumps({
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_boom", "amount": 700000, "method": "card"}}},
    }).encode()

    with TestClient(main.app) as client:
        resp = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": "bypassed-by-monkeypatch",
                "x-razorpay-event-id": str(uuid.uuid4()),
                "content-type": "application/json",
            },
        )

    assert resp.status_code == 200                       # ack despite the pipeline raising
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0] == 1
        assert con.execute(
            "SELECT status FROM events WHERE payment_id = 'pay_boom'"
        ).fetchone() == ("failed",)                      # the event row is still stored
    finally:
        con.close()


# ==================================================================== BREAK 6
_DET_SCRIPT = r"""
import json, sys
from app.ingest import Ingestor
from app import pipeline
from app.decision.engine import load_policy

db = sys.argv[1]
payload = {"event": "payment.failed", "created_at": 1735689654, "payload": {"payment": {"entity": {
    "id": "pay_det", "amount": 1477000, "currency": "INR", "method": "card", "status": "failed",
    "error_code": "BAD_REQUEST_ERROR", "error_description": "x", "error_reason": "insufficient_funds",
    "notes": {"customer_id": "cust_00042", "email": "cust_00042@example.test"}}}}}
with Ingestor(db) as ing:
    ing.ingest("card_failure", payload)
d = pipeline.process_failure("pay_det", db_path=db, policy=load_policy(),
                             now_utc="2026-08-31T09:00:00+00:00")
print(json.dumps({"hash": d.inputs_hash, "terminal": d.terminal, "ev_inr": d.ev_inr,
                  "action": d.action, "cause": d.cause, "arm_skip": getattr(d.skip_reason, "name", None),
                  "rationale": d.rationale}, sort_keys=True))
"""


def test_end_to_end_determinism_across_fresh_processes(tmp_path):
    outs = []
    for i in range(2):
        run_dir = tmp_path / f"run{i}"
        run_dir.mkdir()
        r = subprocess.run(
            [sys.executable, "-c", _DET_SCRIPT, str(run_dir / "w.db")],
            cwd=str(REPO), capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.strip())
    assert outs[0] == outs[1], f"\nP1: {outs[0]}\nP2: {outs[1]}"
    parsed = json.loads(outs[0])
    print("BREAK 6 determinism payload:", json.dumps(parsed, indent=2))


# ==================================================== Fix 1 - seed provenance
def test_experiment_seed_is_datagen_default_seed_not_a_drifting_literal():
    """policy["experiment_seed"] must equal datagen.DEFAULT_SEED (imported,
    not re-typed here). If they drift, the pipeline assigns arms on one seed
    while measurement assigns on another and uplift is computed across
    mismatched arms.

    NOTE: tools/label_harness.py uses SEED = 20260829. That is a DIFFERENT
    seed for a DIFFERENT purpose (blind-sample draw order, deliberately
    decorrelated from generation order) and is correct as-is - it must NOT
    be reconciled with this one.
    """
    assert POLICY["experiment_seed"] == datagen.DEFAULT_SEED
    assert "datagen.DEFAULT_SEED" in POLICY["experiment_seed_basis"]
    assert datagen.DEFAULT_SEED != 20260829  # the label-harness seed is separate


def test_pipeline_and_measurement_share_one_arm_assignment_implementation():
    """Change 1: assign_arm lives in app.arms; eval.measurement re-exports the
    same object. The pipeline imports it from app/, never from eval/."""
    assert measurement.assign_arm is app_arms.assign_arm
    assert measurement.assign_arms is app_arms.assign_arms
    src = (REPO / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert "from app.arms import" in src
    assert "import eval" not in src and "from eval" not in src


def test_pipeline_arm_matches_measurement_arm_for_a_sample_of_customers(tmp_path):
    """For >= 50 customers, the arm the PIPELINE lands them in (inferred from
    the recorded Decision: CONTROL_ARM skip <=> control, anything else <=>
    treatment) equals the arm the MEASUREMENT layer assigns for the datagen
    seed. A seed drift would flip ~30% of these."""
    db = tmp_path / "w.db"
    cids = [f"cust_{i:05d}" for i in range(1, 61)]  # 60 >= 50
    _ingest(db, *[
        _card_failure(f"pay_arm_{i:05d}", cid, 900000 + i,
                      error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds")
        for i, cid in enumerate(cids, start=1)
    ])

    mismatches = []
    seen = {"control": 0, "treatment": 0}
    for i, cid in enumerate(cids, start=1):
        d = pipeline.process_failure(f"pay_arm_{i:05d}", db_path=str(db),
                                     policy=POLICY, now_utc=NOW)
        pipeline_arm = "control" if d.skip_reason is SkipReason.CONTROL_ARM else "treatment"
        measurement_arm = measurement.assign_arm(datagen.DEFAULT_SEED, cid)
        seen[pipeline_arm] += 1
        if pipeline_arm != measurement_arm:
            mismatches.append((cid, pipeline_arm, measurement_arm))

    assert not mismatches, mismatches
    assert seen["control"] > 0 and seen["treatment"] > 0, seen  # sample hit both arms
    assert seen["control"] + seen["treatment"] >= 50


# ============================================= Slice 8 live-ingest bridge
import re as _re


def _live_payload(payment_id, *, email=None, phone=None, amount_paise=1234500,
                  created_at=1735689654, error_code="GATEWAY_ERROR",
                  error_reason="bank_downtime", drop_amount=False):
    """A verified live Razorpay-shaped payment.failed webhook body: NO
    notes.customer_id (live test payments carry none)."""
    entity = {
        "id": payment_id, "currency": "INR", "method": "card", "status": "failed",
        "error_code": error_code, "error_description": "diagnostic text",
        "error_reason": error_reason, "notes": {},
    }
    if not drop_amount:
        entity["amount"] = amount_paise
    if email is not None:
        entity["notes"]["email"] = email
    if phone is not None:
        entity["notes"]["contact"] = phone
    return {"entity": "event", "event": "payment.failed", "contains": ["payment"],
            "created_at": created_at, "payload": {"payment": {"entity": entity}}}


def _normalized_rows(db_path, reference=None):
    with Ingestor(str(db_path)) as ing:
        return [r for r in ing.rows() if reference is None or r["reference"] == reference]


def _post_webhook(client, body_dict, event_id):
    return client.post(
        "/webhooks/razorpay",
        content=json.dumps(body_dict).encode(),
        headers={"x-razorpay-signature": "bypassed", "x-razorpay-event-id": event_id,
                 "content-type": "application/json"},
    )


@pytest.fixture()
def webhook(monkeypatch, tmp_path):
    """A TestClient whose signature check is bypassed and whose DB is a fresh
    temp file shared by webhook storage + ingest + decisions."""
    from fastapi.testclient import TestClient

    import app.main as _main  # deferred: a module-level import freezes
    #   app.main.RAZORPAY_WEBHOOK_SECRET before test_webhook.py sets its env

    db = tmp_path / "wh.db"
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(db))
    monkeypatch.setattr(_main, "verify_signature", lambda *_a, **_k: True)
    with TestClient(_main.app) as client:
        yield client, db


# ---- BREAK 1: realistic live payload (no notes, email present) --------------
def test_break1_live_payload_bridges_to_a_recorded_decision(tmp_path):
    db = tmp_path / "w.db"
    pid = "pay_TW5qVOcX3157VO"
    reject = pipeline.ingest_live_failure(
        _live_payload(pid, email="buyer@live.test", amount_paise=1850000),
        db_path=str(db),
    )
    assert reject is None  # adapter accepted it

    (row,) = _normalized_rows(db, pid)
    assert row["source"] == "card_failure" and row["reference"] == pid
    assert row["customer_id"] == f"live_{pid}"
    assert row["email"] == "buyer@live.test" and row["phone"] is None
    assert row["raw"]["payload"]["payment"]["entity"]["notes"]["customer_id_source"] == "derived"
    assert row["amount_paise"] == 1850000  # paise straight from the live entity

    d = pipeline.process_failure(pid, db_path=str(db), policy=POLICY, now_utc=NOW)
    assert d is not None
    assert d.terminal in (Terminal.ACT, Terminal.SKIP, Terminal.ROUTE_TO_HUMAN)
    assert len(_decisions_rows(db, pid)) == 1


# ---- BREAK 2: same payload delivered twice ---------------------------------
def test_break2_redelivery_still_one_events_row_and_one_decisions_row(webhook):
    client, db = webhook
    body = _live_payload("pay_live_dup", email="dup@live.test", amount_paise=990000)

    r1 = _post_webhook(client, body, event_id=str(uuid.uuid4()))
    r2 = _post_webhook(client, body, event_id=str(uuid.uuid4()))  # fresh envelope id
    assert r1.status_code == 200 and r2.status_code == 200

    con = sqlite3.connect(str(db))
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM events WHERE payment_id = 'pay_live_dup'"
        ).fetchone()[0] == 1
    finally:
        con.close()
    assert len(_normalized_rows(db, "pay_live_dup")) == 1
    assert len(_decisions_rows(db, "pay_live_dup")) == 1


# ---- BREAK 3: neither email nor phone -> recorded, not an exception --------
def test_break3_contactless_live_payload_records_a_decision(tmp_path):
    db = tmp_path / "w.db"
    # pick a payment_id whose derived customer lands in treatment, so the
    # outcome is a clean ACT/retry_silent rather than a control skip.
    pid = next(
        f"pay_nc_{i:04d}" for i in range(10000)
        if app_arms.assign_arm(datagen.DEFAULT_SEED, f"live_pay_nc_{i:04d}") == "treatment"
    )

    reject = pipeline.ingest_live_failure(
        _live_payload(pid, email=None, phone=None, amount_paise=500000),
        db_path=str(db),
    )
    assert reject is None  # allow_missing_contact kept the row

    (row,) = _normalized_rows(db, pid)
    assert row["email"] is None and row["phone"] is None
    assert row["customer_id"] == f"live_{pid}"

    d = pipeline.process_failure(pid, db_path=str(db), policy=POLICY, now_utc=NOW)
    assert d is not None                                   # no exception
    assert d.skip_reason is not SkipReason.NO_CONTACT_CHANNEL
    assert d.terminal == Terminal.ACT and d.action == "retry_silent"
    assert len(_decisions_rows(db, pid)) == 1


# ---- BREAK 4: adapter rejects -> 200, logged, no decisions row ------------
def test_break4_adapter_reject_still_acks_200_and_writes_no_decision(webhook, caplog):
    client, db = webhook
    caplog.set_level("WARNING", logger="razorpay_webhook")

    resp = _post_webhook(
        client,
        _live_payload("pay_live_bad", email="x@live.test", drop_amount=True),
        event_id=str(uuid.uuid4()),
    )
    assert resp.status_code == 200
    assert any("rejected by card_failure adapter" in m for m in caplog.messages)

    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0] == 1
        assert con.execute(
            "SELECT status FROM events WHERE payment_id = 'pay_live_bad'"
        ).fetchone() == ("failed",)
    finally:
        con.close()
    assert _decisions_rows(db, "pay_live_bad") == []


# ---- BREAK 5: derived customer_id cannot collide with a datagen one -------
def test_break5_derived_live_customer_id_never_collides_with_datagen():
    gt = json.loads((REPO / "data" / "ground_truth.json").read_text(encoding="utf-8"))
    datagen_ids = set(gt["customers"])
    assert datagen_ids  # sanity: the corpus is present

    for pid in ("pay_TW5qVOcX3157VO", "pay_00001", "cust_00001", "pay_nc_0007", ""):
        derived = pipeline._derive_live_customer_id(pid)
        assert derived.startswith("live_")
        assert not _re.fullmatch(r"cust_\d+", derived)   # datagen's id shape
        assert derived not in datagen_ids
