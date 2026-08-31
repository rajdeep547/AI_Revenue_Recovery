"""Slice 10 - the append-only execution ledger.

Same discipline as Slice 2's ``audit``: ``BEFORE UPDATE`` / ``BEFORE DELETE``
triggers ``RAISE(ABORT, ...)`` on both tables, so a recorded intent or outcome
can never be rewritten. Current state is DERIVED from ``execution_outcomes``
(``latest_outcome`` / ``terminal_outcome``), never stored as a mutable column.

Ordering is the whole slice: ``insert_intent`` COMMITS before the executor is
allowed to touch the network. ``append_outcome`` is the only write after.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.execution.client import ExecutionResult, ExecutionStatus, TERMINAL_STATUSES

_CONNECT_TIMEOUT_S = 5.0  # so a concurrent writer waits for COMMIT, then hits the PK

_CREATE_INTENTS = """
CREATE TABLE IF NOT EXISTS execution_intents (
    idem_key            TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    attempt_n           INTEGER NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at          TEXT NOT NULL
)
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS execution_outcomes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key       TEXT NOT NULL,
    status         TEXT NOT NULL,
    provider_ref   TEXT,
    http_status    INTEGER,
    error_redacted TEXT,
    observed_at    TEXT NOT NULL
)
"""

_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS execution_intents_no_update
       BEFORE UPDATE ON execution_intents
       BEGIN SELECT RAISE(ABORT, 'execution_intents is append-only: UPDATE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_intents_no_delete
       BEFORE DELETE ON execution_intents
       BEGIN SELECT RAISE(ABORT, 'execution_intents is append-only: DELETE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_outcomes_no_update
       BEFORE UPDATE ON execution_outcomes
       BEGIN SELECT RAISE(ABORT, 'execution_outcomes is append-only: UPDATE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_outcomes_no_delete
       BEFORE DELETE ON execution_outcomes
       BEGIN SELECT RAISE(ABORT, 'execution_outcomes is append-only: DELETE not allowed'); END""",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), timeout=_CONNECT_TIMEOUT_S)


def init_execution_ledger(db_path: str | Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(_CREATE_INTENTS)
        conn.execute(_CREATE_OUTCOMES)
        for trig in _TRIGGERS:
            conn.execute(trig)
        conn.commit()
    finally:
        conn.close()


def insert_intent(
    db_path: str | Path,
    *,
    idem_key: str,
    event_id: str,
    action_type: str,
    attempt_n: int,
    request_fingerprint: str,
    now: str | None = None,
    _pre_commit=None,
) -> None:
    """INSERT the intent and COMMIT. Raises :class:`sqlite3.IntegrityError` if
    the idem_key already exists -- the PRIMARY KEY IS the concurrency lock (see
    ``executor.execute``).

    ``_pre_commit`` is a crash-test seam ONLY: a callable run after the INSERT
    but before COMMIT, so a test can SIGKILL the process holding an uncommitted
    intent and prove nothing persisted.
    """
    now = now or _utcnow_iso()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO execution_intents "
            "(idem_key, event_id, action_type, attempt_n, request_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (idem_key, event_id, action_type, attempt_n, request_fingerprint, now),
        )
        if _pre_commit is not None:
            _pre_commit()
        conn.commit()
    finally:
        conn.close()


def get_intent(db_path: str | Path, idem_key: str) -> dict | None:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT * FROM execution_intents WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _row_to_result(r: sqlite3.Row) -> ExecutionResult:
    return ExecutionResult(
        status=ExecutionStatus(r["status"]),
        provider_ref=r["provider_ref"],
        http_status=r["http_status"],
        error=r["error_redacted"],
    )


def latest_outcome(db_path: str | Path, idem_key: str) -> ExecutionResult | None:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT * FROM execution_outcomes WHERE idem_key = ? ORDER BY id DESC LIMIT 1",
            (idem_key,),
        ).fetchone()
        return _row_to_result(r) if r else None
    finally:
        conn.close()


def terminal_outcome(db_path: str | Path, idem_key: str) -> ExecutionResult | None:
    """The first terminal outcome recorded for this idem_key, or None. By
    construction there is at most one (``audit/slice10_idempotency.py`` proves
    it), but the scan does not assume that."""
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM execution_outcomes WHERE idem_key = ? ORDER BY id ASC",
            (idem_key,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        if ExecutionStatus(r["status"]) in TERMINAL_STATUSES:
            return _row_to_result(r)
    return None


def append_outcome(
    db_path: str | Path, *, idem_key: str, result: ExecutionResult, now: str | None = None
) -> None:
    now = now or _utcnow_iso()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO execution_outcomes "
            "(idem_key, status, provider_ref, http_status, error_redacted, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (idem_key, str(result.status), result.provider_ref, result.http_status,
             result.error, now),
        )
        conn.commit()
    finally:
        conn.close()


def all_intents(db_path: str | Path) -> list[dict]:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM execution_intents ORDER BY created_at, idem_key"
            )
        ]
    finally:
        conn.close()
