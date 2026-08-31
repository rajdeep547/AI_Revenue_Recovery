"""LIVE metrics -- derived read-only from ``WEBHOOK_DB_PATH``.

Every SQLite handle here is opened as
``sqlite3.connect("file:<path>?mode=ro", uri=True)`` (see :func:`connect_ro`):
any INSERT / UPDATE / DELETE on it raises ``sqlite3.OperationalError``. No file
is written. Nothing in this module reads the synthetic truth file under
``data/`` and nothing imports from the ``eval`` package.

The numbers here come only from real webhook deliveries the decision path
already recorded:

* ``decisions``          -- one row per failed payment the engine decided on
* ``normalized_events``  -- the Slice 4 normalized row (customer_id, occurred_at)
* ``events``             -- Slice 2 payment state; ``status='recovered'`` is the
                            only in-DB recovery signal, set when a real
                            ``payment.captured`` webhook arrived
* ``spend_ledger``       -- one row per committed action; every row is
                            ``status='dry_run'`` at 0.00 today
                            (``_REAL_SPEND_ENABLED`` is False)

:func:`load_live` returns a dict with two faces: ``display`` (pre-formatted
strings, em-dash where a value is unknown) for the template, and ``raw``
(plain numbers / None) for :mod:`scripts.audit_metrics` to check.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.arms import assign_arm

DASH = "—"

_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "decision_policy.json"

# The five action-ladder rungs, in ladder order. Fixed list so the card always
# shows every rung even when a rung never fired.
RUNGS = ("retry_silent", "email", "sms", "whatsapp", "agent_call")

# Terminals the engine can persist. BLOCKED/<PRIMARY> variants are discovered
# from the data (guardrails can emit any of six primaries).
BASE_TERMINALS = ("ACT", "SKIP", "ROUTE_TO_HUMAN")

_SEVENTY_TWO_H = timedelta(hours=72)

# Uplift is only shown when BOTH arms have at least this many observations.
UPLIFT_MIN_ARM_N = 30


def connect_ro(db_path: str) -> sqlite3.Connection:
    """The ONLY way this package opens SQLite. Read-only URI handle: a write
    attempt raises ``sqlite3.OperationalError: attempt to write a readonly
    database``. Caller closes it."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def experiment_seed() -> int:
    """``experiment_seed`` straight from ``config/decision_policy.json`` -- the
    same value the pipeline hands to :func:`app.arms.assign_arm`."""
    with open(_POLICY_PATH, encoding="utf-8") as fh:
        return int(json.load(fh)["experiment_seed"])


# --------------------------------------------------------------------- helpers
def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_72h(occurred_at, transition_at) -> bool:
    """``transition_at - occurred_at`` within ``[0, 72h]``. A missing or
    unparseable timestamp on either side is treated as NOT within-72h (the
    signal is simply not usable), never as an error."""
    a = _parse_iso(occurred_at)
    b = _parse_iso(transition_at)
    if a is None or b is None:
        return False
    delta = b - a
    return timedelta(0) <= delta <= _SEVENTY_TWO_H


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _pct(rec: int, n: int) -> str:
    return f"{rec / n * 100:.1f}% ({rec}/{n})" if n else DASH


def _rupees(x: float) -> str:
    return f"Rs {x:,.2f}"


# ------------------------------------------------------------------ derivation
def _derive(conn: sqlite3.Connection | None, tables: set[str]) -> dict:
    seed = experiment_seed()

    # ---- card 1: decisions by terminal; SKIP by skip_reason ----------------
    terminal_counts: dict[str, int] = {}
    skip_counts: dict[str, int] = {}
    if conn is not None and "decisions" in tables:
        for terminal, c in conn.execute(
            "SELECT terminal, COUNT(*) FROM decisions GROUP BY terminal"
        ):
            terminal_counts[terminal] = c
        for sr, c in conn.execute(
            "SELECT skip_reason, COUNT(*) FROM decisions "
            "WHERE terminal = 'SKIP' GROUP BY skip_reason"
        ):
            skip_counts[sr if sr is not None else "(none)"] = c
    decisions_total = sum(terminal_counts.values())
    have = decisions_total > 0

    ordered_terminals = list(BASE_TERMINALS) + sorted(
        t for t in terminal_counts if t not in BASE_TERMINALS
    )
    card1_rows = [
        (t, str(terminal_counts.get(t, 0)) if have else DASH)
        for t in ordered_terminals
    ]
    card1_skip_rows = [
        (sr, str(n)) for sr, n in sorted(skip_counts.items())
    ]

    # ---- card 2 + arm map: payment_id -> arm ------------------------------
    # decisions.payment_id -> normalized_events.reference -> customer_id,
    # then app.arms.assign_arm(experiment_seed, customer_id).
    arm_by_pid: dict[str, str] = {}
    arm_unknown = 0
    if conn is not None and {"decisions", "normalized_events"} <= tables:
        for pid, cid in conn.execute(
            "SELECT d.payment_id, n.customer_id "
            "FROM decisions d "
            "LEFT JOIN normalized_events n ON n.reference = d.payment_id"
        ):
            if cid is None:
                arm_unknown += 1
                continue
            arm_by_pid[pid] = assign_arm(seed, cid)
    arm_t = sum(1 for a in arm_by_pid.values() if a == "treatment")
    arm_c = sum(1 for a in arm_by_pid.values() if a == "control")
    arm_n = arm_t + arm_c
    control_share = (arm_c / arm_n) if arm_n else None

    # ---- card 3: recovery within 72h, by arm ----------------------------
    den_t = den_c = rec_t = rec_c = 0
    rec_value_t = rec_value_c = 0.0
    if conn is not None and {"decisions", "normalized_events", "events"} <= tables:
        for cid, status, updated_at, occurred_at, ticket_inr in conn.execute(
            "SELECT n.customer_id, e.status, e.updated_at, n.occurred_at, "
            "       d.ticket_inr "
            "FROM decisions d "
            "JOIN normalized_events n ON n.reference = d.payment_id "
            "LEFT JOIN events e ON e.payment_id = d.payment_id"
        ):
            if cid is None:
                continue
            arm = assign_arm(seed, cid)
            if arm == "control":
                den_c += 1
            else:
                den_t += 1
            if status == "recovered" and _within_72h(occurred_at, updated_at):
                if arm == "control":
                    rec_c += 1
                    rec_value_c += ticket_inr or 0.0
                else:
                    rec_t += 1
                    rec_value_t += ticket_inr or 0.0
    rate_t = (rec_t / den_t) if den_t else None
    rate_c = (rec_c / den_c) if den_c else None
    rate_pooled = (
        (rec_t + rec_c) / (den_t + den_c) if (den_t + den_c) else None
    )

    # ---- card 4: incremental uplift (suppressed unless both arms >= 30) ---
    suppressed = not (den_t >= UPLIFT_MIN_ARM_N and den_c >= UPLIFT_MIN_ARM_N)
    if suppressed or rate_t is None or rate_c is None:
        uplift_pp = None
        attributable = None
        uplift_display = (
            f"insufficient sample (treatment n={den_t}, control n={den_c})"
        )
    else:
        uplift_frac = rate_t - rate_c
        uplift_pp = uplift_frac * 100.0
        attributable = uplift_frac * den_t
        uplift_display = f"{uplift_pp:+.2f} pp"

    # ---- card 5: money (dry-run only; no rupee figure implies money moved) -
    would_be_cost = 0.0
    if conn is not None and "decisions" in tables:
        row = conn.execute(
            "SELECT COALESCE(SUM(action_cost_inr), 0.0) FROM decisions "
            "WHERE terminal = 'ACT'"
        ).fetchone()
        would_be_cost = float(row[0] or 0.0)
    dry_run_sends = 0
    real_debit = 0.0
    if conn is not None and "spend_ledger" in tables:
        dry_run_sends = conn.execute(
            "SELECT COUNT(*) FROM spend_ledger WHERE status = 'dry_run'"
        ).fetchone()[0]
        real_debit = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_inr), 0.0) FROM spend_ledger "
                "WHERE status = 'debit'"
            ).fetchone()[0]
            or 0.0
        )
    if suppressed or rate_c is None:
        net_realised_ev = None
        net_ev_display = (
            f"insufficient sample (treatment n={den_t}, control n={den_c})"
        )
    else:
        counterfactual_t = (rec_value_c / den_c) * den_t
        incr_value = rec_value_t - counterfactual_t
        net_realised_ev = incr_value - real_debit
        net_ev_display = _rupees(net_realised_ev)

    # ---- card 6: chosen rung distribution over ACT decisions -----------
    rung_counts = {r: 0 for r in RUNGS}
    if conn is not None and "decisions" in tables:
        for action, c in conn.execute(
            "SELECT action, COUNT(*) FROM decisions "
            "WHERE terminal = 'ACT' AND action IS NOT NULL GROUP BY action"
        ):
            rung_counts[action] = rung_counts.get(action, 0) + c

    display = {
        "have_data": have,
        "events_total": str(decisions_total) if have else DASH,
        "card1_rows": card1_rows,
        "card1_skip_rows": card1_skip_rows,
        "arm_treatment_n": str(arm_t) if arm_n else DASH,
        "arm_control_n": str(arm_c) if arm_n else DASH,
        "arm_unknown_n": arm_unknown,
        "control_share": f"{control_share:.3f}" if control_share is not None else DASH,
        "control_share_target": "0.30",
        "rate_treatment": _pct(rec_t, den_t),
        "rate_control": _pct(rec_c, den_c),
        "rate_pooled": _pct(rec_t + rec_c, den_t + den_c),
        "uplift": uplift_display,
        "uplift_attributable": (
            f"{attributable:+.1f}" if attributable is not None else DASH
        ),
        "uplift_suppressed": suppressed,
        "recovered_value_treatment": _rupees(rec_value_t) if den_t else DASH,
        "recovered_value_control": _rupees(rec_value_c) if den_c else DASH,
        "action_spend": (
            f"{dry_run_sends} dry-run send(s); {_rupees(real_debit)} debited "
            f"(real spend disabled: _REAL_SPEND_ENABLED is False)"
        ),
        "would_be_action_cost": (
            f"{_rupees(would_be_cost)} (never charged; dry-run)" if have else DASH
        ),
        "net_realised_ev": net_ev_display,
        "rung_rows": [(r, str(rung_counts[r]) if have else DASH) for r in RUNGS],
    }

    raw = {
        "decisions_total": decisions_total,
        "terminal_counts": terminal_counts,
        "skip_counts": skip_counts,
        "arm_treatment_n": arm_t,
        "arm_control_n": arm_c,
        "arm_unknown_n": arm_unknown,
        "control_share": control_share,
        "den_treatment_n": den_t,
        "den_control_n": den_c,
        "rec_treatment_n": rec_t,
        "rec_control_n": rec_c,
        "rate_treatment": rate_t,
        "rate_control": rate_c,
        "rate_pooled": rate_pooled,
        "uplift_suppressed": suppressed,
        "uplift_pp": uplift_pp,
        "uplift_attributable": attributable,
        "would_be_action_cost_inr": would_be_cost,
        "dry_run_send_count": dry_run_sends,
        "real_debit_inr": real_debit,
        "recovered_value_treatment_inr": rec_value_t,
        "recovered_value_control_inr": rec_value_c,
        "net_realised_ev_inr": net_realised_ev,
        "rung_counts": rung_counts,
    }
    return {"display": display, "raw": raw}


def load_live(db_path: str) -> dict:
    """Read-only derivation of every LIVE card from ``db_path``.

    A missing file, or a file with none of the expected tables, yields a
    well-formed all-em-dash result (HTTP 200, no exception) -- absence of data
    is not an error.
    """
    exists = os.path.exists(db_path)
    conn = None
    try:
        if exists:
            conn = connect_ro(db_path)
            tables = _tables(conn)
        else:
            tables = set()
        result = _derive(conn, tables)
    finally:
        if conn is not None:
            conn.close()
    result["db_path"] = db_path
    result["db_exists"] = exists
    return result
