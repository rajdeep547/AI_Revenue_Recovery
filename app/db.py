import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("WEBHOOK_DB_PATH", "webhook_events.db")


def _resolve(db_path: str | None) -> str:
    # Re-read the env var per call instead of trusting DB_PATH — that
    # constant is frozen at import time, which breaks when multiple test
    # modules set WEBHOOK_DB_PATH before importing app.db in one process.
    if db_path is not None:
        return db_path
    return os.environ.get("WEBHOOK_DB_PATH", "webhook_events.db")


def init_db(db_path: str | None = None) -> None:
    db_path = _resolve(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                headers TEXT NOT NULL,
                raw_body BLOB NOT NULL,
                received_at TEXT NOT NULL,
                parsed_ok INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                payment_id TEXT PRIMARY KEY,
                amount INTEGER,
                method TEXT,
                status TEXT NOT NULL,
                first_event_id TEXT,
                last_event_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT,
                event_id TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (payment_id) REFERENCES events(payment_id)
            )
            """
        )
        # Append-only enforcement lives in the DB, not app code: any UPDATE
        # or DELETE against audit is aborted by the trigger before it can
        # touch the row, regardless of who issues the statement.
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_no_update
            BEFORE UPDATE ON audit
            BEGIN
                SELECT RAISE(ABORT, 'audit is append-only: UPDATE is not allowed');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
            BEFORE DELETE ON audit
            BEGIN
                SELECT RAISE(ABORT, 'audit is append-only: DELETE is not allowed');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: str | None = None):
    conn = sqlite3.connect(_resolve(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def store_raw_event(
    event_id: str | None,
    headers: dict,
    raw_body: bytes,
    db_path: str | None = None,
) -> bool:
    """Persist the raw request before any parsing happens.

    Returns True if a new row was inserted, False if event_id already
    existed (duplicate delivery — treated as a no-op, not an error).
    """
    received_at = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
                (event_id, headers, raw_body, received_at, parsed_ok)
            VALUES (?, ?, ?, ?, 0)
            """,
            (event_id, json.dumps(headers), raw_body, received_at),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_parsed(event_id: str | None, db_path: str | None = None) -> None:
    if event_id is None:
        return
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE webhook_events SET parsed_ok = 1 WHERE event_id = ?",
            (event_id,),
        )
        conn.commit()


def create_event(
    payment_id: str,
    event_id: str,
    status: str,
    amount: int | None = None,
    method: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Create the events row for payment_id if it doesn't already exist.

    INSERT OR IGNORE so a race against a concurrent first-sighting is a
    no-op rather than an error. Returns True if this call actually created
    the row (i.e. this really is the first time we've seen the payment).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
                (payment_id, amount, method, status, first_event_id, last_event_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, amount, method, status, event_id, event_id, now, now),
        )
        conn.commit()
        return cur.rowcount > 0


def update_event(
    payment_id: str,
    event_id: str,
    status: str | None = None,
    amount: int | None = None,
    method: str | None = None,
    db_path: str | None = None,
) -> None:
    """Record that event_id was seen for payment_id.

    last_event_id and updated_at always move forward. status/amount/method
    are only touched when the caller has a real transition to apply — a
    duplicate/no-op delivery calls this with status=None to just bump
    last_event_id without disturbing the recorded state.
    """
    fields = ["last_event_id = ?", "updated_at = ?"]
    params: list = [event_id, datetime.now(timezone.utc).isoformat()]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if amount is not None:
        fields.append("amount = ?")
        params.append(amount)
    if method is not None:
        fields.append("method = ?")
        params.append(method)
    params.append(payment_id)
    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE events SET {', '.join(fields)} WHERE payment_id = ?",
            params,
        )
        conn.commit()


def get_event(payment_id: str, db_path: str | None = None) -> dict | None:
    with get_conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM events WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return dict(row) if row else None


def append_audit(
    payment_id: str | None,
    event_id: str,
    action: str,
    detail: dict | None = None,
    db_path: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO audit (payment_id, event_id, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payment_id, event_id, action, json.dumps(detail) if detail is not None else None, now),
        )
        conn.commit()


def get_audit_trace(payment_id: str, db_path: str | None = None) -> list[dict]:
    """Ordered audit history for payment_id.

    Ordered by created_at then by the autoincrement id as a tiebreaker,
    so two rows sharing a timestamp still come back in insertion order
    instead of whatever order SQLite feels like on a given run.
    """
    with get_conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, event_id, action, detail, created_at FROM audit
            WHERE payment_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (payment_id,),
        ).fetchall()
    trace = []
    for row in rows:
        entry = dict(row)
        entry["detail"] = json.loads(entry["detail"]) if entry["detail"] else None
        trace.append(entry)
    return trace
