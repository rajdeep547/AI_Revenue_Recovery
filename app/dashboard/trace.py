"""GET /trace/{payment_id} + GET /decisions -- Screen 2 of the Slice 12
read-only dashboard.

Same hard constraints as /metrics: SQLite is opened only through
:func:`app.dashboard.live.connect_ro` (a ``mode=ro`` handle), nothing here
writes, nothing imports the offline measurement harness, nothing reads a
synthetic truth file, and there is no CDN / build step -- one inlined CSS
file, server-rendered Jinja.

Why the existing JSON route is not reused
----------------------------------------
``GET /events/{payment_id}/trace`` (app/main.py) returns only the Slice 2
``events`` row plus the ``audit`` list. The trace page is built almost
entirely from ``decisions`` (verdict, cause, EV arithmetic, ladder inputs,
provenance), ``normalized_events`` (customer_id -> arm, contact channels)
and ``guardrail_evaluations`` -- none of which that route exposes, and its
one overlap (the audit list) is fetched there through a read-write handle.
So this module runs its own read-only query and the JSON route is untouched.

Which DB the page reads is ``WEBHOOK_DB_PATH`` (default ``webhook_events.db``)
-- exactly as /metrics. The running server never opens
``tests/fixtures/trace_demo.db``; only the tests point the env var there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.arms import assign_arm
from app.dashboard.live import connect_ro
from app.dashboard.served import banner, served_db_path
from app.decision.engine import Decision, load_policy
from app.pipeline import RULES_CONFIDENCE  # flat rules-classifier confidence (0.32)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_CSS = (_TEMPLATES_DIR / "trace.css").read_text(encoding="utf-8")

router = APIRouter()

DASH = "—"

# The seven guardrails, evaluation == precedence order (app/guardrails.py).
GUARDRAILS = (
    "kill_switch", "opt_out", "attempt_cap", "contact_limit",
    "quiet_hours", "spend_cap", "dry_run",
)

# Verdict-side plain-English gloss for each guardrail, used only when one is
# the PRIMARY blocker on a BLOCKED/<X> decision.
_BLOCKER_GLOSS = {
    "kill_switch": "the global kill switch is engaged, halting every recovery action",
    "opt_out": "the customer is on the suppression list",
    "attempt_cap": "this payment has already used its lifetime cap of recovery attempts",
    "contact_limit": "this customer has already had the maximum number of contacts in the rolling window",
    "quiet_hours": "the message would land inside quiet hours (21:00–09:00 IST), "
                   "when SMS, WhatsApp and calls are held until morning",
    "spend_cap": "the daily spend cap for recovery actions is already exhausted",
    "dry_run": "the system is in dry-run mode",
}

# /decisions grouping.
_GROUP_ORDER = ("ACT", "ROUTE_TO_HUMAN", "SKIP", "BLOCKED")


def _db_path() -> str:
    """The file the dashboard reads: DASHBOARD_DB_PATH if set, else the live
    WEBHOOK_DB_PATH."""
    return served_db_path()


# ------------------------------------------------------------------ formatting
def _rs(x) -> str:
    return f"Rs {x:,.2f}" if x is not None else DASH


def _prob(x) -> str:
    return f"{x:.4f}" if x is not None else DASH


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else DASH


def _txt(x) -> str:
    return str(x) if x is not None and x != "" else DASH


def _audit_detail(raw) -> str:
    """Render an ``audit.detail`` blob as readable, NULL-free text. The stored
    value is JSON (or None); keys whose value is null are dropped so the page
    never surfaces a bare ``null``."""
    if raw is None or raw == "":
        return DASH
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(obj, dict):
        parts = [f"{k}: {v}" for k, v in obj.items() if v is not None]
        return ", ".join(parts) if parts else DASH
    return str(obj)


# ------------------------------------------------------------------- trace load
def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _decision_row(conn, payment_id: str) -> dict | None:
    cols = ", ".join([*Decision.FLAT_FIELDS, "created_at"])
    r = conn.execute(
        f"SELECT {cols} FROM decisions WHERE payment_id = ? ORDER BY id LIMIT 1",
        (payment_id,),
    ).fetchone()
    if r is None:
        return None
    return dict(zip([*Decision.FLAT_FIELDS, "created_at"], r))


def _verdict(row: dict, *, is_llm: bool, ev_best_rung: str | None,
             ev_best_ev, primary_blocker: str | None) -> str:
    """One complete plain-English sentence. Never contains a bare None."""
    t = row["terminal"]
    sr = row["skip_reason"]
    ticket = _rs(row["ticket_inr"])
    floor = _rs(load_policy()["min_ev_inr"])
    ev = _rs(row["ev_inr"])

    if t == "ACT":
        return (
            f"We contacted this customer via {row['action']}, because the expected "
            f"value of doing so ({ev}) cleared the {floor} floor and no guardrail "
            f"objected."
        )

    if t == "ROUTE_TO_HUMAN":
        if is_llm:
            return (
                "We did not act automatically on this customer. The failure cause "
                "here was identified by the LLM classifier, and LLM-classified "
                "events are never allowed to trigger a paid action (TAIL_ACT_ENABLED "
                "is False); the case was sent to a human queue for a person to decide."
            )
        return (
            f"We did not act automatically on this customer. Policy routes any ticket "
            f"of {_rs(row['route_ticket_floor_inr'])} or more that is diagnosed with "
            f"less than {_pct(row['route_confidence_ceiling'])} confidence to a person, "
            f"and this {ticket} ticket at {_pct(row['cause_confidence'])} confidence "
            f"meets both conditions. The {ev} expected value was not, on its own, "
            f"enough to authorise the spend."
        )

    if t and t.startswith("BLOCKED/"):
        gloss = _BLOCKER_GLOSS.get(primary_blocker, "a safety guardrail vetoed it")
        best = ev_best_rung or "a paid nudge"
        return (
            f"We did not contact this customer. The economics favoured reaching out "
            f"via {best} ({_rs(ev_best_ev)} expected value), but every step of the "
            f"action ladder was vetoed by a safety guardrail: {gloss}."
        )

    # SKIP, by reason
    if sr == "EV_BELOW_FLOOR":
        return (
            f"We did not contact this customer, because the expected value of "
            f"contacting them ({ev}) was below the {floor} floor."
        )
    if sr == "CONTROL_ARM":
        return (
            "We did not contact this customer, because they are in the control group "
            "of a live experiment and are held back from contact by design — not "
            "because the economics were unfavourable."
        )
    if sr == "NO_CONTACT_CHANNEL":
        return (
            f"We did not contact this customer, because we hold no usable email "
            f"address or phone number for them, so no step of the action ladder could "
            f"run on this {ticket} failure."
        )
    if sr == "RISK_BLOCKED":
        return (
            f"We did not contact this customer, because the risk engine blocked this "
            f"{ticket} payment, so no recovery nudge was appropriate."
        )
    if sr == "ALREADY_RECOVERED":
        return (
            f"We did not contact this customer, because this {ticket} payment had "
            f"already recovered on its own."
        )
    if sr == "COOLDOWN":
        return (
            f"We did not contact this customer, because they were contacted within the "
            f"cooldown window, so this {ticket} failure waits rather than stacking "
            f"another message."
        )
    if sr == "PRIOR_ZERO":
        return (
            "We did not contact this customer, because this failure cause carries a "
            "zero incremental prior — a nudge cannot cause this recovery."
        )
    return f"We recorded a {_txt(t)} decision for this customer."


def _load_trace(payment_id: str, db_path: str) -> dict | None:
    """Read-only view-model for one decision, or ``None`` if there is no
    decision for ``payment_id`` (-> 404)."""
    if not os.path.exists(db_path):
        return None
    conn = connect_ro(db_path)
    try:
        tables = _tables(conn)
        if "decisions" not in tables:
            return None
        conn.row_factory = None
        row = _decision_row(conn, payment_id)
        if row is None:
            return None

        # -- normalized row: contact channels, method, customer_id --
        norm = None
        if "normalized_events" in tables:
            nr = conn.execute(
                "SELECT customer_id, email, phone, method FROM normalized_events "
                "WHERE reference = ? ORDER BY id LIMIT 1",
                (payment_id,),
            ).fetchone()
            if nr is not None:
                norm = {"customer_id": nr[0], "email": nr[1], "phone": nr[2],
                        "method": nr[3]}
        method = (norm or {}).get("method")
        if method is None and "events" in tables:
            er = conn.execute(
                "SELECT method FROM events WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            method = er[0] if er else None
        customer_id = (norm or {}).get("customer_id")
        has_email = bool((norm or {}).get("email"))
        has_phone = bool((norm or {}).get("phone"))

        policy = load_policy()
        seed = policy["experiment_seed"]
        floor = policy["min_ev_inr"]
        pop = policy["population_incremental"]
        arm = assign_arm(seed, customer_id) if customer_id else None

        # -- classifier path (reconstructed; source is not a stored column) --
        cc = row["cause_confidence"]
        is_llm = (row["cause"] == "expired_card") or (
            cc is not None and abs(cc - RULES_CONFIDENCE) > 1e-9
        )

        # -- ladder recompute: all five rungs on this decision's p_effective --
        p_eff = row["p_effective"]
        ticket = row["ticket_inr"]
        ladder, best_name, best_ev, best_eff = [], None, None, None
        for rung in policy["action_ladder"]:
            rc = rung["requires_channel"]
            eligible = rc is None or (rc == "email" and has_email) or (
                rc == "phone" and has_phone
            )
            ev = viable = None
            if eligible and p_eff is not None and ticket is not None:
                p_rung = min(p_eff * rung["effectiveness"], 0.95)
                ev = p_rung * ticket - rung["cost_inr"]
                viable = ev >= floor
                if best_ev is None or ev > best_ev:
                    best_ev, best_name, best_eff = ev, rung["name"], rung["effectiveness"]
            ladder.append({
                "name": rung["name"], "cost": rung["cost_inr"],
                "effectiveness": rung["effectiveness"], "requires_channel": rc,
                "eligible": eligible, "ev": ev, "viable": viable,
            })

        chosen = row["action"] or best_name

        for L in ladder:
            L["chosen"] = (L["name"] == chosen)
            L["ev_disp"] = _rs(L["ev"])
            L["cost_disp"] = _rs(L["cost"])
            if L["chosen"]:
                L["lost"] = DASH
            elif not L["eligible"]:
                L["lost"] = f"channel — no {L['requires_channel']} on file for this customer"
            elif L["ev"] is None:
                L["lost"] = "not scored — no ladder ran on this decision"
            elif L["viable"] is False:
                L["lost"] = f"viability — its own EV {_rs(L['ev'])} is under the {_rs(floor)} floor"
            else:
                L["lost"] = f"cost — viable, but {_rs(L['ev'])} EV is beaten by {chosen}"

        # -- event_id (for guardrail rows + audit) --
        event_id = None
        if "audit" in tables:
            ar = conn.execute(
                "SELECT event_id FROM audit WHERE payment_id = ? AND action = 'decision' "
                "ORDER BY id LIMIT 1",
                (payment_id,),
            ).fetchone()
            if ar is not None:
                event_id = ar[0]

        # -- guardrail rows for the chosen (or EV-best) rung --
        gr_rows, primary_blocker = [], None
        if "guardrail_evaluations" in tables and event_id and chosen:
            q = conn.execute(
                "SELECT guardrail_name, blocked, reason FROM guardrail_evaluations "
                "WHERE event_id = ? AND rung = ? ORDER BY id",
                (event_id, chosen),
            ).fetchall()
            for name, blocked, reason in q:
                blk = bool(blocked)
                gr_rows.append({
                    "name": name, "blocked": blk,
                    "verdict": "BLOCK" if blk else "pass",
                    "reason": reason or DASH,
                })
                if blk and primary_blocker is None:
                    primary_blocker = name
        guardrails_ran = len(gr_rows) > 0

        # -- audit rows, time order --
        audit_rows = []
        if "audit" in tables:
            for cat, act, eid, det in conn.execute(
                "SELECT created_at, action, event_id, detail FROM audit "
                "WHERE payment_id = ? ORDER BY created_at ASC, id ASC",
                (payment_id,),
            ):
                audit_rows.append({
                    "created_at": _txt(cat), "action": _txt(act),
                    "event_id": _txt(eid), "detail": _audit_detail(det),
                })
    finally:
        conn.close()

    is_control = (row["terminal"] == "SKIP" and row["skip_reason"] == "CONTROL_ARM")
    verdict = _verdict(
        row, is_llm=is_llm, ev_best_rung=best_name, ev_best_ev=best_ev,
        primary_blocker=primary_blocker,
    )

    return {
        "payment_id": payment_id,
        # 1 verdict
        "verdict": verdict,
        "terminal": _txt(row["terminal"]),
        "skip_reason": _txt(row["skip_reason"]),
        "gate_basis": _txt(row["gate_basis"]),
        # 2 what failed
        "cause": _txt(row["cause"]),
        "cause_confidence": _pct(cc) + (f" ({_prob(cc)})" if cc is not None else ""),
        "ticket_inr": _rs(row["ticket_inr"]),
        "method": _txt(method),
        "is_llm": is_llm,
        "classifier_path": (
            "LLM classifier (app/llm_diagnosis.py) — routed to a human queue"
            if is_llm else "rules engine (app/diagnosis.py)"
        ),
        # 3 arithmetic
        "is_control": is_control,
        "cc_raw": _prob(cc) if cc is not None else DASH,
        "prior": _prob(row["p_incremental_prior"]),
        "population_rate": _prob(pop),
        "history_multiplier": _txt(row["history_multiplier"]),
        "p_effective": _prob(row["p_effective"]),
        "action_effectiveness": _txt(best_eff) if best_eff is not None else DASH,
        "p_action_basis": _prob(row["p_action_basis"]),
        "action_cost_inr": _rs(row["action_cost_inr"]),
        "ev_inr": _rs(row["ev_inr"]),
        "min_ev_inr": _rs(floor),
        "ev_vs_floor": (
            "n/a — no EV was computed on this decision"
            if row["ev_inr"] is None else
            (f"{_rs(row['ev_inr'])} ≥ {_rs(floor)} — clears the floor"
             if row["ev_inr"] >= floor else
             f"{_rs(row['ev_inr'])} < {_rs(floor)} — under the floor")
        ),
        "shadow_action": _txt(row["shadow_action"]),
        # 4 ladder
        "ladder": ladder,
        "chosen_rung": _txt(chosen),
        # 5 arm
        "customer_id": _txt(customer_id),
        "experiment_seed": _txt(seed),
        "arm": _txt(arm),
        # 6 guardrails
        "guardrails_ran": guardrails_ran,
        "guardrail_rows": gr_rows,
        "guardrail_rung": _txt(chosen),
        # 7 provenance
        "inputs_hash": _txt(row["inputs_hash"]),
        "policy_version": _txt(row["policy_version"]),
        "created_at": _txt(row["created_at"]),
        "audit_rows": audit_rows,
    }


@router.get("/trace/{payment_id}", response_class=HTMLResponse)
def trace(request: Request, payment_id: str) -> HTMLResponse:
    d = _load_trace(payment_id, _db_path())
    if d is None:
        return _templates.TemplateResponse(
            request=request, name="trace_404.html",
            context={"css": _CSS, "payment_id": payment_id,
                     "corpus_banner": banner()},
            status_code=404,
        )
    return _templates.TemplateResponse(
        request=request, name="trace.html",
        context={"css": _CSS, "dash": DASH, "d": d, "corpus_banner": banner()},
    )


# ------------------------------------------------------------------- /decisions
def _load_decisions(db_path: str) -> dict:
    """Every ``decisions`` row, grouped by terminal, each linking to its trace.
    Missing file / table -> empty groups (HTTP 200)."""
    groups: dict[str, list[dict]] = {}
    total = 0
    if os.path.exists(db_path):
        conn = connect_ro(db_path)
        try:
            tables = _tables(conn)
            if "decisions" in tables:
                have_norm = "normalized_events" in tables
                join = ("LEFT JOIN normalized_events n ON n.reference = d.payment_id"
                        if have_norm else "")
                cid_sel = "n.customer_id" if have_norm else "NULL"
                seed = load_policy()["experiment_seed"]
                rows = conn.execute(
                    f"""
                    SELECT d.payment_id, d.terminal, d.skip_reason, d.cause,
                           d.ticket_inr, d.action, {cid_sel} AS customer_id
                    FROM decisions d
                    {join}
                    ORDER BY d.created_at ASC, d.id ASC
                    """
                ).fetchall()
            else:
                rows = []
        finally:
            conn.close()

        for pid, terminal, skip_reason, cause, ticket, action, cid in rows:
            total += 1
            grp = "BLOCKED" if (terminal or "").startswith("BLOCKED/") else terminal
            groups.setdefault(grp, []).append({
                "payment_id": pid,
                "terminal": terminal,
                "skip_reason": skip_reason or DASH,
                "cause": cause or DASH,
                "ticket_inr": _rs(ticket),
                "chosen_rung": action or DASH,
                "arm": assign_arm(seed, cid) if cid else DASH,
            })

    ordered = [
        {"name": g, "rows": groups[g], "count": len(groups[g])}
        for g in _GROUP_ORDER if g in groups
    ]
    ordered += [
        {"name": g, "rows": groups[g], "count": len(groups[g])}
        for g in sorted(groups) if g not in _GROUP_ORDER
    ]
    return {"groups": ordered, "total": total}


@router.get("/decisions", response_class=HTMLResponse)
def decisions_index(request: Request) -> HTMLResponse:
    db_path = _db_path()
    ctx = {"css": _CSS, "dash": DASH, "db_path": db_path,
           "corpus_banner": banner(), **_load_decisions(db_path)}
    return _templates.TemplateResponse(
        request=request, name="decisions.html", context=ctx
    )
