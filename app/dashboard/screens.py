"""GET /not-chased and GET /queue -- Screens 3 and 4 of the Slice 12 dashboard.

Read-only: SQLite opened only through :func:`app.dashboard.live.connect_ro`,
no writes, no ``eval`` import, no synthetic-truth file. Banner via
:mod:`app.dashboard.served`. These screens read whatever ``served_db_path()``
points at (never ``tests/fixtures/trace_demo.db``); a path with no real rows
shows an honest empty state, never a hidden section.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dashboard.live import connect_ro
from app.dashboard.served import banner, served_db_path

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_CSS = (_TEMPLATES_DIR / "trace.css").read_text(encoding="utf-8")

router = APIRouter()
DASH = "—"

# The five hard-gate skip reasons, shown individually so every path is visible.
HARD_GATE_REASONS = (
    "NO_CONTACT_CHANNEL",
    "ALREADY_RECOVERED",
    "COOLDOWN",
    "PRIOR_ZERO",
    "RISK_BLOCKED",
)

_HARD_GATE_GLOSS = {
    "NO_CONTACT_CHANNEL": "no usable email address or phone number on file",
    "ALREADY_RECOVERED": "the payment had already succeeded on its own",
    "COOLDOWN": "the customer was contacted too recently to message again",
    "PRIOR_ZERO": "this cause has a zero incremental prior — a nudge cannot "
                  "cause the recovery",
    "RISK_BLOCKED": "the risk engine flagged the payment; no nudge is appropriate",
}


def _rs(x) -> str:
    return f"Rs {x:,.2f}" if x is not None else DASH


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else DASH


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


# ================================================================ /not-chased
def _subcat(conn, key: str, label: str, gloss: str, where: str, params: tuple) -> dict:
    rows = conn.execute(
        f"SELECT payment_id, terminal, ticket_inr, cause FROM decisions "
        f"WHERE {where} ORDER BY created_at ASC, id ASC",
        params,
    ).fetchall()
    withheld = sum((r[2] or 0.0) for r in rows)
    return {
        "key": key,
        "label": label,
        "gloss": gloss,
        "count": len(rows),
        "withheld": withheld,
        "withheld_disp": _rs(withheld),
        "rows": [
            {
                "payment_id": r[0],
                "terminal": r[1],
                "ticket_disp": _rs(r[2]),
                "cause": r[3] or DASH,
            }
            for r in rows
        ],
    }


def _empty_not_chased() -> dict:
    """Structure for a DB with no decisions: every path still listed, at zero."""
    def z(key, label, gloss):
        return {"key": key, "label": label, "gloss": gloss, "count": 0,
                "withheld": 0.0, "withheld_disp": _rs(0.0), "rows": []}

    cats = [
        {"heading": "Held back by experimental design",
         "gloss": "the control group — the slice of customers we never contact, "
                  "on purpose, so their unaided recovery rate is the baseline "
                  "everything else is measured against",
         "subs": [z("CONTROL_ARM", "control-arm hold-out",
                    "assigned to control by a coin-flip on the customer id")]},
        {"heading": "Economics below the floor",
         "gloss": "the expected value of contacting — the likely rupee gain once "
                  "you weight the payoff by how probable a recovery is — came out "
                  "under the floor, the minimum expected value we require before "
                  "spending anything",
         "subs": [z("EV_BELOW_FLOOR", "expected value under the floor",
                    "cheapest viable rung still did not clear min_ev_inr")]},
        {"heading": "Hard gates",
         "gloss": "a structural rule made a contact impossible or pointless "
                  "before any economics were considered",
         "subs": [z(r, r, _HARD_GATE_GLOSS[r]) for r in HARD_GATE_REASONS]},
        {"heading": "Vetoed by a guardrail",
         "gloss": "a guardrail — a safety check that can stop an action even when "
                  "the economics say go — blocked every rung of the action ladder",
         "subs": [z("BLOCKED/*", "guardrail veto",
                    "every rung tried was blocked; terminal is BLOCKED/<blocker>")]},
    ]
    for c in cats:
        c["count"] = 0
        c["withheld"] = 0.0
        c["withheld_disp"] = _rs(0.0)
    return {"cats": cats,
            "totals": {"count": 0, "withheld_disp": _rs(0.0), "act": 0,
                       "route": 0, "total": 0}}


def _load_not_chased(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return _empty_not_chased()
    conn = connect_ro(db_path)
    try:
        if "decisions" not in _tables(conn):
            return _empty_not_chased()

        act = route = total = 0
        for term, n in conn.execute(
            "SELECT terminal, COUNT(*) FROM decisions GROUP BY terminal"
        ):
            total += n
            if term == "ACT":
                act += n
            elif term == "ROUTE_TO_HUMAN":
                route += n

        control = _subcat(
            conn, "CONTROL_ARM", "control-arm hold-out",
            "assigned to control by a coin-flip on the customer id — not a "
            "judgement about this payment",
            "skip_reason = ?", ("CONTROL_ARM",),
        )
        evfloor = _subcat(
            conn, "EV_BELOW_FLOOR", "expected value under the floor",
            "the best rung's expected value did not clear the minimum we require "
            "before spending",
            "skip_reason = ?", ("EV_BELOW_FLOOR",),
        )
        hard = [
            _subcat(conn, r, r, _HARD_GATE_GLOSS[r], "skip_reason = ?", (r,))
            for r in HARD_GATE_REASONS
        ]
        blocked = _subcat(
            conn, "BLOCKED/*", "guardrail veto",
            "every rung of the ladder was blocked by a safety check; the "
            "terminal names the first blocker",
            "terminal LIKE 'BLOCKED/%'", (),
        )

        cats = [
            {"heading": "Held back by experimental design",
             "gloss": "the control group — the slice of customers we never "
                      "contact, on purpose, so their unaided recovery rate is the "
                      "baseline every claim of value is measured against",
             "subs": [control]},
            {"heading": "Economics below the floor",
             "gloss": "the expected value of contacting — the likely rupee gain "
                      "once you weight the payoff by how probable a recovery is — "
                      "came out under the floor, the minimum we require before "
                      "spending anything",
             "subs": [evfloor]},
            {"heading": "Hard gates",
             "gloss": "a structural rule made contact impossible or pointless "
                      "before any economics were considered",
             "subs": hard},
            {"heading": "Vetoed by a guardrail",
             "gloss": "a guardrail — a safety check that can stop an action even "
                      "when the economics say go — blocked every rung of the "
                      "action ladder",
             "subs": [blocked]},
        ]
        for c in cats:
            c["count"] = sum(s["count"] for s in c["subs"])
            c["withheld"] = sum(s["withheld"] for s in c["subs"])
            c["withheld_disp"] = _rs(c["withheld"])

        nc_count = sum(c["count"] for c in cats)
        nc_withheld = sum(c["withheld"] for c in cats)
    finally:
        conn.close()

    return {
        "cats": cats,
        "totals": {
            "count": nc_count,
            "withheld_disp": _rs(nc_withheld),
            "act": act,
            "route": route,
            "total": total,
        },
    }


@router.get("/not-chased", response_class=HTMLResponse)
def not_chased(request: Request) -> HTMLResponse:
    ctx = {"css": _CSS, "dash": DASH, "corpus_banner": banner(),
           **_load_not_chased(served_db_path())}
    return _templates.TemplateResponse(
        request=request, name="not_chased.html", context=ctx
    )


# ===================================================================== /queue
def _route_reason(gate_basis, cause, ticket, floor, ceiling, conf) -> str:
    if gate_basis == "llm_gated" or cause == "expired_card":
        return (
            "cause identified by the LLM classifier — LLM-classified failures are "
            "barred from taking a paid action until validated on real data, so "
            "the case is parked for a person"
        )
    return (
        f"ticket {_rs(ticket)} is at or above {_rs(floor)} and the diagnosis is "
        f"held with only {_pct(conf)} confidence (under the {_pct(ceiling)} "
        f"ceiling) — a standing policy rule, not a low expected-value decision"
    )


def _load_queue(db_path: str) -> dict:
    rows = []
    if os.path.exists(db_path):
        conn = connect_ro(db_path)
        try:
            if "decisions" in _tables(conn):
                for (pid, ticket, cause, conf, gate_basis, floor, ceiling,
                     created_at) in conn.execute(
                    "SELECT payment_id, ticket_inr, cause, cause_confidence, "
                    "gate_basis, route_ticket_floor_inr, route_confidence_ceiling, "
                    "created_at FROM decisions WHERE terminal = 'ROUTE_TO_HUMAN' "
                    "ORDER BY created_at ASC, id ASC"
                ):
                    rows.append({
                        "payment_id": pid,
                        "ticket_disp": _rs(ticket),
                        "cause": cause or DASH,
                        "confidence_disp": _pct(conf),
                        "created_at": created_at or DASH,
                        "why": _route_reason(gate_basis, cause, ticket, floor,
                                             ceiling, conf),
                    })
        finally:
            conn.close()
    return {"rows": rows}


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request) -> HTMLResponse:
    ctx = {"css": _CSS, "dash": DASH, "corpus_banner": banner(),
           **_load_queue(served_db_path())}
    return _templates.TemplateResponse(
        request=request, name="queue.html", context=ctx
    )
