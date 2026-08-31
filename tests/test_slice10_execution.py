"""Slice 10 - real execution. BREAK phase.

B1-B4, B7-B9 run against a mock (the scriptable FakeActionClient or a mocked
HTTP transport). B5/B6 spawn a real subprocess + a real local stub HTTP server
and SIGKILL the child mid-flight. No wall-clock dependence: the executor's
sleep is stubbed out everywhere.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.execution import executor as executor_mod
from app.execution.client import (
    ExecutionResult,
    ExecutionStatus,
    compute_idem_key,
)
from app.execution.executor import execute
from app.execution.fake_client import FakeActionClient
from app.execution.ledger import (
    append_outcome,
    init_execution_ledger,
    insert_intent,
)
from app.execution.razorpay_client import RazorpayClient
from app.execution.reconcile import reconcile

REPO = Path(__file__).resolve().parents[1]
NOSLEEP = {"sleep": lambda _s: None}  # kill the backoff wait in every executor call


# --------------------------------------------------------------------- helpers
def _rows(db, table):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        con.close()


def _retriable(http=503):
    return ExecutionResult(ExecutionStatus.FAILED_RETRIABLE, http_status=http, error="boom")


def _ok(ref="plink_x", http=200):
    return ExecutionResult(ExecutionStatus.SENT, provider_ref=ref, http_status=http)


# ============================================================= B1 - replay
def test_b1_replay_calls_provider_once(tmp_path):
    db = tmp_path / "x.db"
    client = FakeActionClient()
    kw = dict(db_path=str(db), event_id="evt_1", action_type="email", attempt_n=1,
              payload={"amount_paise": 5000}, **NOSLEEP)

    r1 = execute(client, **kw)
    r2 = execute(client, **kw)  # identical (event_id, action_type, attempt_n)

    assert client.send_count == 1
    assert r1.status is ExecutionStatus.SENT
    assert r2 == r1                                  # second returns the first outcome
    assert len(_rows(db, "execution_intents")) == 1
    assert len(_rows(db, "execution_outcomes")) == 1


# ============================================================= B2 - 500s then 200
def test_b2_transient_500s_then_success(tmp_path):
    db = tmp_path / "x.db"
    client = FakeActionClient(script=[_retriable(), _retriable(), _retriable(), _ok("plink_ok")])

    r = execute(client, db_path=str(db), event_id="evt_2", action_type="sms", attempt_n=1,
                payload={"amount_paise": 5000}, **NOSLEEP)

    assert r.status is ExecutionStatus.SENT and r.provider_ref == "plink_ok"
    assert client.send_count == 4                          # four HTTP attempts
    keys = {req.idem_key for req in client.calls}
    assert keys == {compute_idem_key("evt_2", "sms", 1)}   # identical idem_key on all four
    outcomes = _rows(db, "execution_outcomes")
    assert len(outcomes) == 1                              # exactly one (terminal) outcome row
    assert len(_rows(db, "execution_intents")) == 1        # one logical send


# ============================================================= B3 - 500s exhausted
def test_b3_retries_exhausted_is_non_terminal(tmp_path):
    db = tmp_path / "x.db"
    client = FakeActionClient(script=[_retriable(), _retriable(), _retriable(), _retriable()])

    r = execute(client, db_path=str(db), event_id="evt_3", action_type="email", attempt_n=1,
                payload={"amount_paise": 5000}, **NOSLEEP)

    assert r.status is ExecutionStatus.FAILED_RETRIABLE
    assert not r.is_terminal
    assert client.send_count == 4
    assert len(_rows(db, "execution_intents")) == 1        # no second intent row
    outcomes = _rows(db, "execution_outcomes")
    assert len(outcomes) == 1 and outcomes[0][2] == "FAILED_RETRIABLE"  # status column

    # a follow-up execute() with the same key still adds no intent row and does not re-send
    r2 = execute(client, db_path=str(db), event_id="evt_3", action_type="email", attempt_n=1,
                 payload={"amount_paise": 5000}, **NOSLEEP)
    assert client.send_count == 4
    assert len(_rows(db, "execution_intents")) == 1
    assert r2.status is ExecutionStatus.FAILED_RETRIABLE


# ============================================================= B4 - payload mismatch
def test_b4_same_key_mutated_payload_is_terminal_no_send(tmp_path):
    db = tmp_path / "x.db"
    # first attempt fails non-terminally so the intent persists without a terminal outcome
    client = FakeActionClient(script=[_retriable(), _retriable(), _retriable(), _retriable()])
    execute(client, db_path=str(db), event_id="evt_4", action_type="email", attempt_n=1,
            payload={"amount_paise": 5000, "note": "v1"}, **NOSLEEP)
    assert client.send_count == 4

    r = execute(client, db_path=str(db), event_id="evt_4", action_type="email", attempt_n=1,
                payload={"amount_paise": 5000, "note": "v2"}, **NOSLEEP)  # mutated

    assert r.status is ExecutionStatus.FAILED_TERMINAL
    assert r.error == "idem_key_payload_mismatch"
    assert client.send_count == 4                          # no new provider call
    assert len(_rows(db, "execution_intents")) == 1


# ============================================================= B7 - concurrency
def test_b7_two_threads_same_key_one_provider_call(tmp_path):
    db = tmp_path / "x.db"
    init_execution_ledger(str(db))
    barrier = threading.Barrier(2)

    def _slow_send(_req):
        time.sleep(0.15)  # keep the winner "in flight" while the loser checks

    client = FakeActionClient(on_send=_slow_send)
    results = {}

    def worker(tag):
        barrier.wait()
        results[tag] = execute(
            client, db_path=str(db), event_id="evt_7", action_type="whatsapp",
            attempt_n=1, payload={"amount_paise": 9000}, **NOSLEEP,
        )

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    assert client.send_count == 1                          # PRIMARY KEY is the lock
    assert len(_rows(db, "execution_intents")) == 1
    statuses = {r.status for r in results.values()}
    assert ExecutionStatus.SENT in statuses               # the winner sent
    assert len(_rows(db, "execution_outcomes")) >= 1


# ============================================================= B8 - append-only
def test_b8_ledger_tables_reject_update_and_delete(tmp_path):
    db = tmp_path / "x.db"
    init_execution_ledger(str(db))
    key = "k" * 32
    insert_intent(str(db), idem_key=key, event_id="e", action_type="email", attempt_n=1,
                  request_fingerprint="fp")
    append_outcome(str(db), idem_key=key, result=_ok("plink_b8"))

    con = sqlite3.connect(str(db))
    try:
        for tbl, col in (("execution_intents", "created_at"), ("execution_outcomes", "status")):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(f"UPDATE {tbl} SET {col} = 'x'")
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(f"DELETE FROM {tbl}")
    finally:
        con.close()


# ============================================================= B9 - gates
def test_b9_razorpay_mode_requires_live_flag():
    from app.execution.config import build_client

    with pytest.raises(RuntimeError):
        build_client(env_override={"EXECUTION_MODE": "razorpay_test",
                                   "LIVE_EXECUTION_ENABLED": "false"})


def test_b9_non_test_key_id_raises():
    with pytest.raises(ValueError):
        RazorpayClient("rzp_live_abcdef", "secret")
    with pytest.raises(ValueError):
        RazorpayClient("", "secret")


def test_b9_tail_act_path_never_reaches_executor():
    from app import llm_diagnosis as tail

    # the master switch is still off
    assert tail.TAIL_ACT_ENABLED is False

    # an LLM-sourced, otherwise money-eligible diagnosis routes to the human
    # queue and never invokes the spend callback (which is where an executor
    # call would live)
    d = tail.Diagnosis("invalid_card", tail.SRC_LLM, tail.ROUTE_ACT, "llm_confident", 1)
    assert d.is_money_eligible is True
    spent = []
    route = tail.apply(d, spend=lambda cause: spent.append(cause), enqueue=lambda _d: None)
    assert route == tail.ROUTE_HUMAN
    assert spent == []

    # structural: nothing in the diagnosis / decision / pipeline path imports
    # app.execution, and nothing in app.execution imports the LLM tail
    for rel in ("app/llm_diagnosis.py", "app/diagnosis.py", "app/decision/engine.py",
                "app/pipeline.py"):
        assert "app.execution" not in (REPO / rel).read_text(encoding="utf-8")
    for p in (REPO / "app" / "execution").glob("*.py"):
        assert "llm_diagnosis" not in p.read_text(encoding="utf-8")


# ============================================================= B5 / B6 - kill -9
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(cond, timeout=15.0, tick=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(tick)
    return False


def _child_env():
    return {**os.environ, "PYTHONPATH": str(REPO)}


def _start_stub(port, record, *, hang_post):
    args = [sys.executable, str(REPO / "tests" / "_slice10_stub.py"),
            "--port", str(port), "--record", str(record)]
    if hang_post:
        args.append("--hang-post")
    p = subprocess.Popen(args, cwd=str(REPO), env=_child_env())
    assert _wait(lambda: _port_open(port), timeout=10), "stub did not come up"
    return p


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def _terminal_count(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT COUNT(*) FROM execution_outcomes "
            "WHERE status IN ('SENT','DUPLICATE','FAILED_TERMINAL')"
        ).fetchone()[0]
    finally:
        con.close()


def test_b5_sigkill_between_send_and_record_then_reconcile(tmp_path):
    port = _free_port()
    rec = tmp_path / "rec.jsonl"
    rec.write_text("")
    db = tmp_path / "exec.db"
    marker = tmp_path / "marker"
    base = f"http://127.0.0.1:{port}"

    stub = _start_stub(port, rec, hang_post=True)
    child = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "_slice10_child.py"),
         "--db", str(db), "--base-url", base, "--mode", "b5", "--marker", str(marker)],
        cwd=str(REPO), env=_child_env(),
    )
    try:
        # wait until the provider has recorded exactly one POST, then kill mid-flight
        assert _wait(lambda: _line_count(rec) == 1), "stub never received the POST"
        assert _wait(lambda: marker.exists()), "child never committed the intent"
        child.kill()
        child.wait(timeout=10)

        # intent committed before the send; outcome never written
        assert len(_rows(db, "execution_intents")) == 1
        assert _terminal_count(db) == 0
        assert _line_count(rec) == 1

        # restart -> reconcile adopts the real state from the provider
        client = RazorpayClient("rzp_test_stub", "stub_secret", base_url=base)
        summary = reconcile(client, db_path=str(db), out=lambda *_a: None)
        assert summary["adopted"] == 1
        assert _line_count(rec) == 1            # reconcile did NOT re-POST
        assert _terminal_count(db) == 1         # ledger converged to one terminal outcome
    finally:
        child.kill()
        stub.kill()


def test_b6_sigkill_before_intent_commit_persists_nothing(tmp_path):
    port = _free_port()
    rec = tmp_path / "rec.jsonl"
    rec.write_text("")
    db = tmp_path / "exec.db"
    marker = tmp_path / "marker"
    base = f"http://127.0.0.1:{port}"

    stub = _start_stub(port, rec, hang_post=False)
    child = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "_slice10_child.py"),
         "--db", str(db), "--base-url", base, "--mode", "b6", "--marker", str(marker)],
        cwd=str(REPO), env=_child_env(),
    )
    try:
        assert _wait(lambda: marker.exists()), "child never reached the pre-commit hang"
        child.kill()
        child.wait(timeout=10)

        # SIGKILL landed before COMMIT: nothing persisted, nothing sent
        con = sqlite3.connect(str(db))
        try:
            n_intents = con.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0]
        finally:
            con.close()
        assert n_intents == 0
        assert _line_count(rec) == 0
    finally:
        child.kill()
        stub.kill()
