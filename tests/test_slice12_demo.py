"""Slice 12 -- demo database (data/demo.db) + selectable served DB + the
non-dismissible corpus banner. BREAK 1-7.

data/demo.db is the 2,000-event corpus driven through the REAL pipeline
(scripts/make_demo_db.py -> scripts.run_corpus.build_decision_db). The live
webhook DB is only ever read.
"""

from __future__ import annotations

import html as _html
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.dashboard.served import CORPUS_BANNER  # noqa: E402
from scripts import make_demo_db  # noqa: E402
from scripts.run_corpus import DEFAULT_EVENTS, build_decision_db  # noqa: E402
from app.decision.engine import load_policy  # noqa: E402

REPO_DEMO_DB = REPO / "data" / "demo.db"


def _count_decisions(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _visible(html: str) -> str:
    t = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    return _html.unescape(re.sub(r"<[^>]+>", " ", t))


# --------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def demo_db(tmp_path_factory) -> Path:
    """The full demo DB. Reuses data/demo.db if a developer already built it;
    otherwise builds once into a tmp path (the real pipeline, ~1 min)."""
    if REPO_DEMO_DB.exists() and _count_decisions(REPO_DEMO_DB) >= 1000:
        return REPO_DEMO_DB
    target = tmp_path_factory.mktemp("demo") / "demo.db"
    make_demo_db.build(target)
    return target


@pytest.fixture
def demo_client(demo_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(demo_db))
    monkeypatch.delenv("WEBHOOK_DB_PATH", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


# ------------------------------------------------------------------- BREAK 1
def test_break1_demo_decisions_and_trace_all_200(demo_db, demo_client):
    assert _count_decisions(demo_db) >= 1000

    r = demo_client.get("/decisions")
    assert r.status_code == 200
    links = re.findall(r'href="/trace/([^"]+)"', r.text)
    assert len(links) >= 1000                    # thousands of rows

    # one sampled payment per terminal actually present
    con = sqlite3.connect(str(demo_db))
    con.row_factory = sqlite3.Row
    terminals = [row[0] for row in con.execute(
        "SELECT DISTINCT terminal FROM decisions"
    )]
    sampled = {
        t: con.execute(
            "SELECT payment_id FROM decisions WHERE terminal = ? "
            "ORDER BY payment_id LIMIT 1", (t,)
        ).fetchone()[0]
        for t in terminals
    }
    con.close()

    for terminal, pid in sampled.items():
        resp = demo_client.get("/trace/" + pid)
        assert resp.status_code == 200, (terminal, pid)
        assert CORPUS_BANNER in resp.text


# ------------------------------------------------------------------- BREAK 2
def test_break2_banner_on_demo_absent_on_live(demo_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    pages = ("/metrics", "/decisions", "/trace/nope")

    # serving the demo DB -> banner on every page
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(demo_db))
    monkeypatch.delenv("WEBHOOK_DB_PATH", raising=False)
    c_demo = TestClient(app)
    for p in pages:
        assert CORPUS_BANNER in _visible(c_demo.get(p).text), ("expected banner", p)

    # serving the live DB -> no banner anywhere
    live = tmp_path / "webhook_live.db"
    live.write_bytes(b"")
    monkeypatch.delenv("DASHBOARD_DB_PATH", raising=False)
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(live))
    c_live = TestClient(app)
    for p in pages:
        assert CORPUS_BANNER not in _visible(c_live.get(p).text), ("no banner", p)


# ------------------------------------------------------------------- BREAK 3
def test_break3_live_db_byte_identical_through_demo_build_and_requests(
    demo_db, monkeypatch
):
    live = REPO / "webhook_events.db"
    if not live.exists():
        pytest.skip("no live webhook_events.db in this checkout")
    before = live.read_bytes()

    # a full demo build already happened (demo_db fixture); now a round of
    # page requests against the demo DB
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(demo_db))
    monkeypatch.delenv("WEBHOOK_DB_PATH", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    c.get("/metrics")
    c.get("/decisions")
    con = sqlite3.connect(str(demo_db))
    pid = con.execute("SELECT payment_id FROM decisions LIMIT 1").fetchone()[0]
    con.close()
    c.get("/trace/" + pid)

    assert live.read_bytes() == before


# ------------------------------------------------------------------- BREAK 4
def test_break4_two_builds_identical_inputs_hash_sets(tmp_path):
    policy = load_policy()
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    # capped identical builds -- determinism is a property of build_decision_db
    # (each event's own payload created_at, committed seed, no wall-clock)
    ha = {
        d["inputs_hash"]
        for d in build_decision_db(DEFAULT_EVENTS, policy, a, max_events=300)[
            "decision_by_pid"
        ].values()
    }
    hb = {
        d["inputs_hash"]
        for d in build_decision_db(DEFAULT_EVENTS, policy, b, max_events=300)[
            "decision_by_pid"
        ].values()
    }
    assert ha and ha == hb


def test_break4_full_demo_db_hashes_all_distinct(demo_db):
    con = sqlite3.connect(str(demo_db))
    con.row_factory = sqlite3.Row
    hashes = [r["inputs_hash"] for r in con.execute(
        "SELECT inputs_hash FROM decisions"
    )]
    con.close()
    assert len(hashes) >= 1000
    assert len(set(hashes)) == len(hashes)      # every decision fully distinct


# ------------------------------------------------------------------- BREAK 5
def test_break5_make_demo_db_aborts_on_webhook_db_path(tmp_path):
    target = tmp_path / "demo.db"
    env = {**os.environ, "WEBHOOK_DB_PATH": str(target)}
    p = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_demo_db.py"), str(target)],
        env=env, capture_output=True, text=True,
    )
    assert p.returncode != 0
    assert "ABORT" in (p.stdout + p.stderr)
    assert not target.exists()

    with pytest.raises(SystemExit):
        make_demo_db.resolve_target(["x", str(tmp_path / "webhook_events.db")])


# ------------------------------------------------------------------- BREAK 6
def test_break6_no_ground_truth_or_eval_import_in_dashboard_pkg():
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


# ---------------------------------------------------------------------- finding
def test_demo_terminal_distribution_route_and_blocked_are_zero(demo_db):
    con = sqlite3.connect(str(demo_db))
    dist = dict(con.execute("SELECT terminal, COUNT(*) FROM decisions GROUP BY terminal"))
    con.close()
    route = dist.get("ROUTE_TO_HUMAN", 0)
    blocked = sum(n for t, n in dist.items() if str(t).startswith("BLOCKED/"))
    # This is the documented FINDING, not a bug: those paths never fire on this
    # corpus (email-only ladder, EV clears the floor for every ticket).
    assert route == 0 and blocked == 0
    assert set(dist) == {"ACT", "SKIP"}
