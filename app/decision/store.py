"""Append-only persistence for :class:`~app.decision.engine.Decision`.

One flat row per decision in ``decisions``; the same append-only guarantee
as Slice 2's ``audit`` table -- ``BEFORE UPDATE`` / ``BEFORE DELETE``
triggers ``RAISE(ABORT, ...)`` -- so a recorded decision cannot be rewritten
by app code, a bug, or a raw SQL console. Every recorded decision also
writes one ``audit`` row (action ``decision``) in the SAME transaction: one
connection, one ``COMMIT``, so the flat record and its audit trail land
together or not at all.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db
from app.decision.engine import Decision

_CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id          TEXT NOT NULL,
    policy_version      TEXT NOT NULL,
    terminal            TEXT NOT NULL,
    action              TEXT,
    skip_reason         TEXT,
    cause               TEXT NOT NULL,
    cause_confidence    REAL NOT NULL,
    p_incremental_prior REAL,
    p_effective         REAL,
    p_action_basis      REAL,
    p_lower_bound       REAL,
    history_multiplier  REAL NOT NULL,
    ticket_inr          REAL NOT NULL,
    action_cost_inr     REAL,
    ev_inr              REAL,
    ev_lower_inr        REAL,
    shadow_action       TEXT,
    gate_basis          TEXT NOT NULL,
    route_ticket_floor_inr    REAL,
    route_confidence_ceiling  REAL,
    rationale           TEXT NOT NULL,
    inputs_hash         TEXT NOT NULL,
    created_at          TEXT NOT NULL
)
"""

# Same pattern as app/db.py's audit_no_update / audit_no_delete.
_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS decisions_no_update
    BEFORE UPDATE ON decisions
    BEGIN
        SELECT RAISE(ABORT, 'decisions is append-only: UPDATE is not allowed');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS decisions_no_delete
    BEFORE DELETE ON decisions
    BEGIN
        SELECT RAISE(ABORT, 'decisions is append-only: DELETE is not allowed');
    END
    """,
)


def init_decision_store(db_path: str | Path) -> None:
    """Idempotent. Also runs :func:`app.db.init_db` so the canonical
    ``audit`` table (and its append-only triggers) exists."""
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_CREATE_DECISIONS)
        for trig in _TRIGGERS:
            conn.execute(trig)
        conn.commit()
    finally:
        conn.close()


def record_decision(
    decision: Decision,
    *,
    event_id: str,
    db_path: str | Path,
    now: str | None = None,
) -> int:
    """Insert the flat decision row and its ``audit`` row in one transaction.
    Returns the new ``decisions.id``.

    ``event_id`` is the delivery/ingest id the decision was made against --
    recorded on the audit row exactly as Slice 2 does. ``now`` (ISO-8601) is
    injectable so callers/tests stay deterministic; it defaults to the wall
    clock here because persistence, unlike the engine, is not a pure step.
    """
    now = now or datetime.now(timezone.utc).isoformat()
    flat = decision.flat()
    columns = [*Decision.FLAT_FIELDS, "created_at"]
    placeholders = ", ".join(f":{c}" for c in columns)
    params = {**flat, "created_at": now}

    audit_detail = json.dumps({
        "terminal": decision.terminal,
        "action": decision.action,
        "skip_reason": flat["skip_reason"],
        "shadow_action": decision.shadow_action,
        "ev_inr": decision.ev_inr,
        "inputs_hash": decision.inputs_hash,
    })

    # Foreign-key enforcement is intentionally left OFF on this connection
    # (SQLite's default). A decision is keyed to an ingested payment, which
    # need not have a row in Slice 2's `events` table; the append-only
    # guarantee comes from the triggers, not from any FK.
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            f"INSERT INTO decisions ({', '.join(columns)}) VALUES ({placeholders})",
            params,
        )
        decision_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO audit (payment_id, event_id, action, detail, created_at)
            VALUES (?, ?, 'decision', ?, ?)
            """,
            (decision.payment_id, event_id, audit_detail, now),
        )
        conn.commit()
        return decision_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
