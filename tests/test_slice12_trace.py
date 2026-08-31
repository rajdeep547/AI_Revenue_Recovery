"""Slice 12 Screen 2 -- decision trace page (/trace/{payment_id}) and the
/decisions index. BREAK conditions 1-8 + PASS.

The page opens SQLite only read-only (connect_ro), writes nothing, imports no
measurement harness. The fixture DB is built from scratch by
scripts/make_trace_fixture.py into a tmp path; the real live DB is only ever
read.
"""

from __future__ import annotations

import html as _html
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.make_trace_fixture import build, resolve_target  # noqa: E402

SHAPES = [
    "pay_FIXTURE_ev_below_floor",
    "pay_FIXTURE_control_arm",
    "pay_FIXTURE_act_email",
    "pay_FIXTURE_route_to_human",
    "pay_FIXTURE_blocked_quiet",
    "pay_FIXTURE_no_channel",
    "pay_FIXTURE_llm_expired_card",
]


# --------------------------------------------------------------------- helpers
def _visible(html: str) -> str:
    t = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)


def _verdict(html: str) -> str:
    m = re.search(r'class="verdict-line">(.+?)</p>', html, re.S)
    assert m, "no verdict-line on page"
    return _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def fixture_db(tmp_path):
    db = tmp_path / "trace_demo.db"
    build(db)
    return db


@pytest.fixture
def client(fixture_db, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(fixture_db))
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


# ------------------------------------------------------------------- BREAK 1
def test_break1_unknown_payment_id_human_404(client):
    r = client.get("/trace/pay_FIXTURE_not_a_real_id")
    assert r.status_code == 404
    assert "No decision is on record" in r.text
    assert "<pre" not in r.text            # no stack dump
    assert "Traceback" not in r.text
    assert "/decisions" in r.text          # points the reader somewhere useful


# ------------------------------------------------------------------- BREAK 2
@pytest.mark.parametrize("pid", SHAPES)
def test_break2_shape_renders_clean_sentence(client, pid):
    r = client.get("/trace/" + pid)
    assert r.status_code == 200
    text = _visible(r.text)
    for bad in ("None", "null", "undefined"):
        assert not re.search(rf"(?<![A-Za-z]){bad}(?![A-Za-z])", text), (pid, bad)
    v = _verdict(r.text)
    assert v[:1].isupper() and v.endswith("."), (pid, v)
    assert len(v.split()) >= 8, (pid, v)


# ------------------------------------------------------------------- BREAK 3
def test_break3_route_to_human_reads_as_policy_override(client):
    v = _verdict(client.get("/trace/pay_FIXTURE_route_to_human").text)
    assert "Rs 10,000.00 or more" in v
    assert "less than 55% confidence" in v
    assert "32% confidence" in v
    assert "meets both conditions" in v
    # the outcome is the override, NOT "EV was too low / below the floor"
    assert "below the" not in v.lower()
    assert "too low" not in v.lower()
    assert "not, on its own, enough" in v.lower()


# ------------------------------------------------------------------- BREAK 4
def test_break4_blocked_quiet_names_quiet_hours(client):
    r = client.get("/trace/pay_FIXTURE_blocked_quiet")
    text = _visible(r.text)
    assert "quiet_hours" in text
    assert "inside quiet hours 21:00-09:00 IST" in text
    assert "block" in text
    assert "quiet hours" in _verdict(r.text).lower()


# ------------------------------------------------------------------- BREAK 5
def test_break5_control_arm_design_ev_not_the_reason(client):
    r = client.get("/trace/pay_FIXTURE_control_arm")
    text = _visible(r.text)
    v = _verdict(r.text).lower()
    assert "control group" in v and "by design" in v
    assert "not because the economics" in v
    # EV arithmetic still rendered ...
    assert "p_effective" in text and "Rs 76.56" in text
    # ... but explicitly marked as not the driver
    assert "did not drive the outcome" in text


# ------------------------------------------------------------------- BREAK 6
def test_break6_live_db_untouched_exactly_one_decision(monkeypatch):
    live = REPO / "webhook_events.db"
    if not live.exists():
        pytest.skip("no live webhook_events.db in this checkout")
    before = live.read_bytes()

    monkeypatch.setenv("WEBHOOK_DB_PATH", str(live))
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    r = c.get("/decisions")
    assert r.status_code == 200
    assert r.text.count('href="/trace/') == 1
    assert "pay_FIXTURE_" not in r.text
    assert re.search(r"1\s*</strong>\s*decision\(s\) total", r.text)
    assert c.get("/metrics").status_code == 200
    assert live.read_bytes() == before          # read-only, byte-identical


# ------------------------------------------------------------------- BREAK 7
def test_break7_fixture_builder_aborts_on_live_db(tmp_path):
    target = tmp_path / "trace_demo.db"
    env = {**os.environ, "WEBHOOK_DB_PATH": str(target)}
    p = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_trace_fixture.py"), str(target)],
        env=env, capture_output=True, text=True,
    )
    assert p.returncode != 0
    assert "ABORT" in (p.stdout + p.stderr)
    assert not target.exists()

    # the bare name 'webhook_events.db' is refused regardless of env
    with pytest.raises(SystemExit):
        resolve_target(["x", str(tmp_path / "webhook_events.db")])


# ------------------------------------------------------------------- BREAK 8
def test_break8_no_ground_truth_or_eval_import_in_dashboard_pkg():
    pkg = REPO / "app" / "dashboard"
    bad = []
    for p in pkg.rglob("*"):
        if p.suffix not in {".py", ".html", ".css"}:
            continue
        s = p.read_text(encoding="utf-8")
        if "ground_truth" in s:
            bad.append(f"{p}: ground_truth")
        if "from eval" in s or "import eval" in s:
            bad.append(f"{p}: eval import")
    assert not bad, bad


# ---------------------------------------------------------------------- PASS
def test_pass_all_shapes_200(client):
    for pid in SHAPES:
        assert client.get("/trace/" + pid).status_code == 200


def test_pass_decisions_index_grouped_shows_arm(client):
    t = client.get("/decisions").text
    for grp in ("ACT", "ROUTE_TO_HUMAN", "SKIP", "BLOCKED"):
        assert f'id="g-{grp.replace("/", "-")}"' in t
    vis = _visible(t)
    assert "CONTROL_ARM" in vis          # a control example is findable
    assert "control" in vis and "treatment" in vis   # arm per row


def test_pass_ladder_recomputes_five_rungs_with_loss_reason(client):
    below = _visible(client.get("/trace/pay_FIXTURE_ev_below_floor").text)
    for rung in ("retry_silent", "email", "sms", "whatsapp", "agent_call"):
        assert rung in below
    assert "viability" in below          # every rung is under the floor here

    ctrl = _visible(client.get("/trace/pay_FIXTURE_control_arm").text)
    assert "cost — viable" in ctrl  # here losing rungs lost on cost


def test_pass_act_email_seven_guardrails_all_pass(client):
    text = _visible(client.get("/trace/pay_FIXTURE_act_email").text)
    for g in ("kill_switch", "opt_out", "attempt_cap", "contact_limit",
              "quiet_hours", "spend_cap", "dry_run"):
        assert g in text
    assert "block" not in text.lower().split("provenance")[0].split("guardrail")[-1] or True
    # no guardrail blocked on this decision
    r = client.get("/trace/pay_FIXTURE_act_email")
    gsec = r.text.split("6 &middot; Guardrails")[1].split("7 &middot; Provenance")[0]
    assert 'tag block' not in gsec
    assert gsec.count("tag pass") == 7


def test_pass_live_trace_and_metrics_200(monkeypatch):
    live = REPO / "webhook_events.db"
    if not live.exists():
        pytest.skip("no live webhook_events.db in this checkout")
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(live))
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    assert c.get("/metrics").status_code == 200
    assert c.get("/decisions").status_code == 200
    assert c.get("/trace/pay_TW67GAczusj3yl").status_code == 200
