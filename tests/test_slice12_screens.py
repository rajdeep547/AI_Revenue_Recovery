"""Slice 12 -- Screens 3 (/not-chased) and 4 (/queue). BREAK 1-6 + nav.

Read-only, connect_ro only, banner via app.dashboard.served. The running
server never serves tests/fixtures/trace_demo.db; tests build their own DB
copies and point DASHBOARD_DB_PATH at them.
"""

from __future__ import annotations

import html as _html
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.dashboard.served import CORPUS_BANNER  # noqa: E402
from scripts import make_demo_db, make_trace_fixture  # noqa: E402

REPO_DEMO_DB = REPO / "data" / "demo.db"
NAV_LINKS = ('href="/metrics"', 'href="/decisions"', 'href="/not-chased"',
            'href="/queue"')


def _visible(html: str) -> str:
    t = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def _count_decisions(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


# --------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def demo_db(tmp_path_factory) -> Path:
    if REPO_DEMO_DB.exists() and _count_decisions(REPO_DEMO_DB) >= 1000:
        return REPO_DEMO_DB
    target = tmp_path_factory.mktemp("demo") / "demo.db"
    make_demo_db.build(target)
    return target


@pytest.fixture(scope="session")
def rich_db(tmp_path_factory) -> Path:
    """The 7-shape fixture DB (built to a tmp path, never the repo fixtures
    dir) -- gives real ROUTE_TO_HUMAN, BLOCKED/*, NO_CONTACT_CHANNEL rows."""
    target = tmp_path_factory.mktemp("rich") / "rich.db"
    make_trace_fixture.build(target)
    return target


def _client(db: Path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db))
    monkeypatch.delenv("WEBHOOK_DB_PATH", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


# ------------------------------------------------------------------- BREAK 1
def test_break1_both_screens_200_live_and_demo_banner_correct(demo_db, tmp_path,
                                                              monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    # demo DB -> 200 + banner
    cd = _client(demo_db, monkeypatch)
    for p in ("/not-chased", "/queue"):
        r = cd.get(p)
        assert r.status_code == 200
        assert CORPUS_BANNER in r.text

    # live DB -> 200 + no banner
    live = tmp_path / "webhook_live.db"
    live.write_bytes(b"")
    monkeypatch.delenv("DASHBOARD_DB_PATH", raising=False)
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(live))
    cl = TestClient(app)
    for p in ("/not-chased", "/queue"):
        r = cl.get(p)
        assert r.status_code == 200
        assert CORPUS_BANNER not in r.text


# ------------------------------------------------------------------- BREAK 2
def test_break2_not_chased_demo_control_rows_and_empty_oneliners(demo_db, monkeypatch):
    c = _client(demo_db, monkeypatch)
    r = c.get("/not-chased")
    assert r.status_code == 200
    text = _visible(r.text)

    con = sqlite3.connect(str(demo_db))
    n_control, withheld = con.execute(
        "SELECT COUNT(*), ROUND(SUM(ticket_inr), 2) FROM decisions "
        "WHERE skip_reason = 'CONTROL_ARM'"
    ).fetchone()
    con.close()

    assert r.text.count('href="/trace/') == n_control == 485
    assert f"{withheld:,.2f}" in text            # withheld total rendered
    assert "485 decision(s) not chased" in text

    # groups with zero rows on this data render as explicit one-liners
    for reason in ("EV_BELOW_FLOOR", "NO_CONTACT_CHANNEL", "ALREADY_RECOVERED",
                   "COOLDOWN", "PRIOR_ZERO", "RISK_BLOCKED", "BLOCKED/*"):
        assert re.search(
            re.escape(reason) + r".{0,140}empty on this data", text, re.S
        ), reason
    assert "did not fire" in text
    assert "Vetoed by a guardrail" in text        # the group header is visible too


# ------------------------------------------------------------------- BREAK 3
def test_break3_queue_empty_state_two_path_explanation(demo_db, monkeypatch):
    c = _client(demo_db, monkeypatch)
    text = _visible(c.get("/queue").text)
    assert "Nothing is queued" in text
    assert "High-ticket, low-confidence" in text
    assert "less than 55% confidence" in text
    assert "Every LLM-classified failure" in text
    assert "barred from taking" in text or "not yet trusted to move money" in text
    assert "TAIL_ACT_ENABLED" in text
    assert "neither fired on this" in text
    # read-only claim
    assert "writes nothing" in text
    assert "no claim, resolve, or action" in text


# ------------------------------------------------------------------- BREAK 4
def test_break4_not_chased_plus_act_equals_total(demo_db, monkeypatch):
    con = sqlite3.connect(str(demo_db))
    dist = dict(con.execute(
        "SELECT terminal, COUNT(*) FROM decisions GROUP BY terminal"
    ))
    con.close()
    total = sum(dist.values())
    act = dist.get("ACT", 0)
    route = dist.get("ROUTE_TO_HUMAN", 0)
    not_chased = total - act - route

    c = _client(demo_db, monkeypatch)
    text = _visible(c.get("/not-chased").text)
    m = re.search(r"(\d+) decision\(s\) not chased", text)
    rendered_not_chased = int(m.group(1))

    assert rendered_not_chased == not_chased
    assert route == 0                            # documents why the next line holds
    assert rendered_not_chased + act == total


def test_break4_holds_on_rich_db_with_route_and_blocked(rich_db, monkeypatch):
    con = sqlite3.connect(str(rich_db))
    dist = dict(con.execute(
        "SELECT terminal, COUNT(*) FROM decisions GROUP BY terminal"
    ))
    con.close()
    total = sum(dist.values())
    act = sum(n for t, n in dist.items() if t == "ACT")
    route = sum(n for t, n in dist.items() if t == "ROUTE_TO_HUMAN")
    blocked = sum(n for t, n in dist.items() if str(t).startswith("BLOCKED/"))
    skips = total - act - route - blocked

    c = _client(rich_db, monkeypatch)
    text = _visible(c.get("/not-chased").text)
    rendered = int(re.search(r"(\d+) decision\(s\) not chased", text).group(1))

    # not-chased == every SKIP + every BLOCKED/*
    assert rendered == skips + blocked
    assert rendered + act + route == total

    # queue now has rows, both routing reasons explained, still no write controls
    qtext = _visible(c.get("/queue").text)
    assert "policy rule" in qtext                       # policy-override reason
    assert "LLM classifier" in qtext                    # llm-gated reason
    assert "Nothing is queued" not in qtext
    assert "writes nothing" not in qtext or "read-only" in qtext.lower()


# ------------------------------------------------------------------- BREAK 5
def test_break5_no_ground_truth_or_eval_import_in_dashboard_pkg():
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


# ------------------------------------------------------------------- nav
def test_nav_links_on_every_screen(demo_db, monkeypatch):
    c = _client(demo_db, monkeypatch)
    con = sqlite3.connect(str(demo_db))
    pid = con.execute("SELECT payment_id FROM decisions LIMIT 1").fetchone()[0]
    con.close()
    for path in ("/metrics", "/decisions", "/not-chased", "/queue",
                 "/trace/" + pid, "/trace/does-not-exist"):
        html = c.get(path).text
        for link in NAV_LINKS:
            assert link in html, (path, link)


# ------------------------------------------------------------------- empty DB
def test_empty_db_shows_every_path_as_zero_not_hidden(tmp_path, monkeypatch):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    c = _client(empty, monkeypatch)
    text = _visible(c.get("/not-chased").text)
    assert "0 decision(s) not chased" in text
    # every named path still visible
    for reason in ("CONTROL_ARM", "EV_BELOW_FLOOR", "NO_CONTACT_CHANNEL",
                   "ALREADY_RECOVERED", "COOLDOWN", "PRIOR_ZERO", "RISK_BLOCKED"):
        assert reason in text, reason
    assert c.get("/queue").status_code == 200
