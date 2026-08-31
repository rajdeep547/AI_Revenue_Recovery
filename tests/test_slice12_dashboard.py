"""Slice 12 -- read-only metrics dashboard (BREAK conditions 1-7 + PASS).

The dashboard opens SQLite only read-only and writes nothing; these tests pin
that, plus the LIVE-vs-audit agreement and the verbatim corpus rendering.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.arms import assign_arm  # noqa: E402
from app.db import init_db  # noqa: E402
from app.decision import store  # noqa: E402
from app import guardrails  # noqa: E402
from app.ingest import Ingestor  # noqa: E402
from app.dashboard import corpus as corpus_mod  # noqa: E402
from app.dashboard import live as live_mod  # noqa: E402
from scripts import audit_metrics  # noqa: E402

SEED = json.loads((REPO / "config" / "decision_policy.json").read_text())["experiment_seed"]
EMAIL_COST = next(
    r["cost_inr"]
    for r in json.loads((REPO / "config" / "decision_policy.json").read_text())["action_ladder"]
    if r["name"] == "email"
)

OCCURRED = "2026-01-01T00:00:00+00:00"
WITHIN_72H = "2026-01-02T00:00:00+00:00"        # +24h
OUTSIDE_72H = "2026-01-05T12:00:00+00:00"       # +108h


# --------------------------------------------------------------------- builders
def _fresh_db(path: str) -> None:
    init_db(path)
    store.init_decision_store(path)
    guardrails.init_guardrail_store(path)
    Ingestor(path).close()          # creates normalized_events / rejected_events


def _insert_norm(conn, pid, cid, ticket_inr):
    conn.execute(
        "INSERT INTO normalized_events (event_id, source, customer_id, email, phone, "
        "amount_paise, currency, method, reason, occurred_at, reference, raw, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"card_failure:{pid}", "card_failure", cid, f"{cid}@x.test", "+919000000000",
         int(round(ticket_inr * 100)), "INR", "card", "payment_failed", OCCURRED, pid,
         "{}", OCCURRED),
    )


def _insert_decision(conn, pid, terminal, ticket_inr, *, skip_reason=None,
                     action=None, action_cost_inr=None):
    conn.execute(
        "INSERT INTO decisions (payment_id, policy_version, terminal, action, skip_reason, "
        "cause, cause_confidence, history_multiplier, ticket_inr, action_cost_inr, "
        "gate_basis, rationale, inputs_hash, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "s7.1", terminal, action, skip_reason, "insufficient_funds", 0.32, 1.0,
         ticket_inr, action_cost_inr, "expected_value", "x", f"h_{pid}", OCCURRED),
    )


def _insert_recovery(conn, pid, ticket_inr, transition_at):
    conn.execute(
        "INSERT INTO events (payment_id, amount, method, status, first_event_id, "
        "last_event_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, int(round(ticket_inr * 100)), "card", "recovered", f"e1_{pid}",
         f"e2_{pid}", OCCURRED, transition_at),
    )
    conn.execute(
        "INSERT INTO audit (payment_id, event_id, action, detail, created_at) "
        "VALUES (?,?,?,?,?)",
        (pid, f"card_failure:{pid}", "outcome_observed",
         json.dumps({"event": "payment.captured", "from": "failed", "to": "recovered"}),
         transition_at),
    )


def _insert_spend(conn, pid, cid, rung):
    conn.execute(
        "INSERT INTO spend_ledger (event_id, customer_id, payment_id, rung, amount_inr, "
        "status, ist_day, ts) VALUES (?,?,?,?,?,?,?,?)",
        (f"card_failure:{pid}", cid, pid, rung, 0.0, "dry_run", "2026-01-01", OCCURRED),
    )


def _build_corpus_db(path: str) -> dict:
    """300 customers, one decision each. Both arms comfortably over n=30, with
    recoveries inside and (one) outside the 72h window, ~40 ACT/email sends, and
    several CONTROL_ARM skips for the audit invariant."""
    _fresh_db(path)
    conn = sqlite3.connect(path)
    treatment, control = [], []
    for i in range(300):
        cid = f"cust_{i:05d}"
        (treatment if assign_arm(SEED, cid) == "treatment" else control).append(cid)

    with conn:
        # treatment arm
        for j, cid in enumerate(treatment):
            pid = f"pay_t_{j:04d}"
            ticket = 1000.0 + (j % 5) * 250.0
            _insert_norm(conn, pid, cid, ticket)
            if j < 40:
                _insert_decision(conn, pid, "ACT", ticket, action="email",
                                 action_cost_inr=EMAIL_COST)
                _insert_spend(conn, pid, cid, "email")
            else:
                _insert_decision(conn, pid, "SKIP", ticket, skip_reason="EV_BELOW_FLOOR")
            if j < 55:                         # 55 treatment recoveries in-window
                _insert_recovery(conn, pid, ticket, WITHIN_72H)
            elif j == 55:                      # one recovery OUTSIDE the window
                _insert_recovery(conn, pid, ticket, OUTSIDE_72H)

        # control arm
        for j, cid in enumerate(control):
            pid = f"pay_c_{j:04d}"
            ticket = 1200.0 + (j % 4) * 300.0
            _insert_norm(conn, pid, cid, ticket)
            reason = "CONTROL_ARM" if j < 12 else "EV_BELOW_FLOOR"
            _insert_decision(conn, pid, "SKIP", ticket, skip_reason=reason)
            if j < 9:                          # 9 control recoveries in-window
                _insert_recovery(conn, pid, ticket, WITHIN_72H)
    conn.close()
    return {"n_treatment": len(treatment), "n_control": len(control)}


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app), monkeypatch


# ------------------------------------------------------------------- BREAK 1
def test_break1_empty_db_renders_dashes_not_zero_pct(tmp_path, client):
    c, mp = client
    missing = tmp_path / "does_not_exist.db"
    mp.setenv("WEBHOOK_DB_PATH", str(missing))

    # load_live on an absent file: no exception, no NaN, no ZeroDivisionError.
    raw = live_mod.load_live(str(missing))
    disp = raw["display"]
    assert raw["raw"]["rate_treatment"] is None
    assert disp["rate_treatment"] == "—"
    assert disp["rate_control"] == "—"
    assert disp["rate_pooled"] == "—"
    assert "0.0%" not in json.dumps(disp)
    assert disp["uplift"] == "insufficient sample (treatment n=0, control n=0)"

    r = c.get("/metrics")
    assert r.status_code == 200
    assert "0.0%" not in r.text

    # also an existing-but-empty schema (lifespan init_db ran) -> still all "—"
    empty = tmp_path / "empty_schema.db"
    _fresh_db(str(empty))
    disp2 = live_mod.load_live(str(empty))["display"]
    assert disp2["rate_treatment"] == disp2["rate_control"] == "—"
    assert disp2["events_total"] == "—"


# ------------------------------------------------------------------- BREAK 2
def test_break2_one_event_uplift_reads_insufficient_sample(tmp_path, client):
    c, mp = client
    db = tmp_path / "one.db"
    _fresh_db(str(db))
    conn = sqlite3.connect(str(db))
    cid = "cust_00000"
    with conn:
        _insert_norm(conn, "pay_x", cid, 1000.0)
        _insert_decision(conn, "pay_x", "SKIP", 1000.0, skip_reason="EV_BELOW_FLOOR")
    conn.close()
    arm = assign_arm(SEED, cid)
    t_n, c_n = (1, 0) if arm == "treatment" else (0, 1)

    mp.setenv("WEBHOOK_DB_PATH", str(db))
    r = c.get("/metrics")
    assert r.status_code == 200
    assert f"insufficient sample (treatment n={t_n}, control n={c_n})" in r.text

    disp = live_mod.load_live(str(db))["display"]
    assert disp["events_total"] == "1"
    assert disp["uplift"] == f"insufficient sample (treatment n={t_n}, control n={c_n})"


# ------------------------------------------------------------------- BREAK 3
def test_break3_missing_corpus_file_degrades_to_one_line(tmp_path, client):
    c, mp = client
    mp.setenv("CORPUS_RESULT_PATH", str(tmp_path / "no_such_final_run.json"))
    mp.setenv("WEBHOOK_DB_PATH", str(tmp_path / "empty.db"))

    assert corpus_mod.load_corpus() is None
    r = c.get("/metrics")
    assert r.status_code == 200
    assert "results/final_run.json</code> is not present" in r.text
    assert 'class="badge"' not in r.text          # no corpus cards rendered
    assert "CORPUS RUN" in r.text                  # header still there


# ------------------------------------------------------------------- BREAK 4
def test_break4_full_data_dashboard_equals_audit(tmp_path):
    db = str(tmp_path / "corpus.db")
    meta = _build_corpus_db(db)

    dash = live_mod.load_live(db)["raw"]
    aud = audit_metrics.audit_live(db)

    for key in audit_metrics._METRIC_KEYS:
        assert audit_metrics._eq(dash[key], aud[key]), (
            f"{key}: dashboard={dash[key]!r} audit={aud[key]!r}"
        )
    checked, disagree = aud["_control_arm_invariant"]
    assert checked == 12 and disagree == 0

    # the run() entry point returns 0 (all PASS) and exercises the printout
    assert audit_metrics.run(db) == 0

    # uplift is live (both arms >= 30) and internally consistent
    assert dash["uplift_suppressed"] is False
    assert dash["den_treatment_n"] >= 30 and dash["den_control_n"] >= 30
    assert dash["rec_treatment_n"] == 55        # the +108h recovery excluded
    assert dash["rec_control_n"] == 9
    expected_pp = (55 / meta["n_treatment"] - 9 / meta["n_control"]) * 100.0
    assert abs(dash["uplift_pp"] - expected_pp) < 1e-9


# ------------------------------------------------------------------- BREAK 5
def test_break5_write_through_dashboard_conn_raises_page_still_renders(tmp_path, client):
    c, mp = client
    db = tmp_path / "ro.db"
    _fresh_db(str(db))

    conn = live_mod.connect_ro(str(db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO decisions (payment_id, policy_version, terminal, cause, "
                "cause_confidence, history_multiplier, ticket_inr, gate_basis, "
                "rationale, inputs_hash, created_at) "
                "VALUES ('p','s7.1','SKIP','x',0.3,1.0,1.0,'g','r','h','t')"
            )
            conn.commit()
    finally:
        conn.close()

    mp.setenv("WEBHOOK_DB_PATH", str(db))
    assert c.get("/metrics").status_code == 200


# ------------------------------------------------------------------- BREAK 6
def test_break6_no_ground_truth_or_eval_import_in_dashboard_pkg():
    pkg = REPO / "app" / "dashboard"
    hits = []
    for p in pkg.rglob("*"):
        if p.suffix not in {".py", ".html", ".css"}:
            continue
        text = p.read_text(encoding="utf-8")
        if "ground_truth" in text:
            hits.append(f"{p}: ground_truth")
        if "from eval" in text or "import eval" in text:
            hits.append(f"{p}: eval import")
    assert not hits, hits


# ------------------------------------------------------------------- BREAK 7
def test_break7_corpus_numbers_are_verbatim_and_not_blended(tmp_path, client):
    c, mp = client
    final_run = REPO / "results" / "final_run.json"
    if not final_run.exists():
        pytest.skip("results/final_run.json not generated in this checkout")

    doc = json.load(open(final_run), parse_float=str, parse_int=str)
    cl = doc["aggregate"]["customer_level"]
    el = doc["aggregate"]["event_level"]

    mp.delenv("CORPUS_RESULT_PATH", raising=False)
    mp.setenv("WEBHOOK_DB_PATH", str(tmp_path / "empty.db"))   # LIVE all "—"
    r = c.get("/metrics")
    assert r.status_code == 200
    html = r.text

    for value in (
        cl["incremental_uplift_pp"],
        cl["recovery_rate_treatment"],
        cl["recovery_rate_control"],
        cl["net_incremental_ev_inr"],
        cl["net_incremental_ev_ci_95"]["low"],
        cl["total_recovered_value_treatment_inr"],
        cl["n_treatment_customers"],
        cl["n_control_customers"],
        el["distinct_actions_by_rung"]["email"],
    ):
        assert str(value) in html, f"corpus value {value!r} not rendered verbatim"

    # no blending: the LIVE panel still shows the em-dash placeholders
    assert "simulated outcomes · eval harness · not live traffic" in html
    assert "insufficient sample (treatment n=0, control n=0)" in html
