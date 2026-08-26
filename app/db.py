import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("WEBHOOK_DB_PATH", "webhook_events.db")


def init_db(db_path: str = DB_PATH) -> None:
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
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def store_raw_event(
    event_id: str | None,
    headers: dict,
    raw_body: bytes,
    db_path: str = DB_PATH,
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


def mark_parsed(event_id: str | None, db_path: str = DB_PATH) -> None:
    if event_id is None:
        return
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE webhook_events SET parsed_ok = 1 WHERE event_id = ?",
            (event_id,),
        )
        conn.commit()
