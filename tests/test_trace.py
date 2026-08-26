import hashlib
import hmac
import json
import os
import sqlite3
import uuid

import pytest

TEST_SECRET = "test_webhook_secret_123"
TEST_DB = "test_trace_events.db"

os.environ["RAZORPAY_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["WEBHOOK_DB_PATH"] = TEST_DB

from fastapi.testclient import TestClient  # noqa: E402

from app.db import append_audit, create_event, get_audit_trace, init_db  # noqa: E402
from app.main import app  # noqa: E402


def sign(body: bytes) -> str:
    return hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def clean_db():
    # pytest imports every test module during collection before running
    # any test, so a sibling module's own os.environ["WEBHOOK_DB_PATH"] =
    # ... (set at its import time) can overwrite this file's value for the
    # whole process. Re-set it here, at the start of each test, so this
    # file's TestClient calls always resolve to TEST_DB regardless of
    # module collection order.
    os.environ["WEBHOOK_DB_PATH"] = TEST_DB
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def make_body(event_type: str, payment_id: str, amount: int = 50000, method: str = "card") -> bytes:
    return json.dumps(
        {
            "event": event_type,
            "payload": {
                "payment": {
                    "entity": {"id": payment_id, "amount": amount, "method": method}
                }
            },
        }
    ).encode()


def post_payment_event(client: TestClient, event_id: str, event_type: str, payment_id: str):
    body = make_body(event_type, payment_id)
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sign(body),
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )


def test_unknown_payment_id_returns_404_not_500():
    with TestClient(app) as client:
        resp = client.get("/events/pay_does_not_exist/trace")
    assert resp.status_code == 404


def test_trace_returns_readable_ordered_history():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    with TestClient(app) as client:
        first = post_payment_event(client, str(uuid.uuid4()), "payment.failed", payment_id)
        second = post_payment_event(client, str(uuid.uuid4()), "payment.captured", payment_id)
        resp = client.get(f"/events/{payment_id}/trace")

    assert first.status_code == 200
    assert second.status_code == 200
    assert resp.status_code == 200
    body = resp.json()
    assert body["payment_id"] == payment_id
    assert body["status"] == "recovered"
    assert [row["action"] for row in body["trace"]] == ["ingested", "outcome_observed"]


def test_failed_then_captured_produces_one_row_recovered_two_audit_rows():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    failed_event_id = str(uuid.uuid4())
    captured_event_id = str(uuid.uuid4())

    with TestClient(app) as client:
        post_payment_event(client, failed_event_id, "payment.failed", payment_id)
        post_payment_event(client, captured_event_id, "payment.captured", payment_id)
        resp = client.get(f"/events/{payment_id}/trace")

    body = resp.json()
    assert body["status"] == "recovered"
    assert body["first_event_id"] == failed_event_id
    assert body["last_event_id"] == captured_event_id

    conn = sqlite3.connect(TEST_DB)
    try:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE payment_id = ?", (payment_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert row_count == 1

    trace = body["trace"]
    assert [row["action"] for row in trace] == ["ingested", "outcome_observed"]
    assert [row["event_id"] for row in trace] == [failed_event_id, captured_event_id]


def test_redelivered_captured_adds_duplicate_but_only_one_outcome_observed():
    payment_id = "pay_" + uuid.uuid4().hex[:14]

    with TestClient(app) as client:
        post_payment_event(client, str(uuid.uuid4()), "payment.failed", payment_id)
        post_payment_event(client, str(uuid.uuid4()), "payment.captured", payment_id)
        redelivery = post_payment_event(client, str(uuid.uuid4()), "payment.captured", payment_id)
        resp = client.get(f"/events/{payment_id}/trace")

    assert redelivery.status_code == 200
    body = resp.json()
    assert body["status"] == "recovered"
    actions = [row["action"] for row in body["trace"]]
    assert actions == ["ingested", "outcome_observed", "duplicate_delivery"]
    assert actions.count("outcome_observed") == 1


def test_captured_with_no_prior_row_is_ingested_not_recovered():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    with TestClient(app) as client:
        post_payment_event(client, str(uuid.uuid4()), "payment.captured", payment_id)
        resp = client.get(f"/events/{payment_id}/trace")

    body = resp.json()
    assert body["status"] == "captured"
    assert [row["action"] for row in body["trace"]] == ["ingested"]


def test_unhandled_event_type_returns_200_and_does_not_create_events_row():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    body = json.dumps(
        {
            "event": "payment.authorized",
            "payload": {"payment": {"entity": {"id": payment_id, "amount": 100, "method": "card"}}},
        }
    ).encode()
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body),
                "x-razorpay-event-id": str(uuid.uuid4()),
                "content-type": "application/json",
            },
        )
        trace_resp = client.get(f"/events/{payment_id}/trace")

    assert resp.status_code == 200
    assert trace_resp.status_code == 404


def test_ordering_is_stable_when_timestamps_collide():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    create_event(payment_id, str(uuid.uuid4()), status="failed", db_path=TEST_DB)

    # Force two audit rows to share the exact same timestamp, bypassing
    # append_audit's own clock so the collision is guaranteed.
    same_ts = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            "INSERT INTO audit (payment_id, event_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (payment_id, str(uuid.uuid4()), "first", None, same_ts),
        )
        conn.execute(
            "INSERT INTO audit (payment_id, event_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (payment_id, str(uuid.uuid4()), "second", None, same_ts),
        )
        conn.commit()
    finally:
        conn.close()

    trace = get_audit_trace(payment_id, db_path=TEST_DB)
    assert [row["action"] for row in trace] == ["first", "second"]

    # Ordering must be stable across repeated reads, not incidental.
    trace_again = get_audit_trace(payment_id, db_path=TEST_DB)
    assert [row["action"] for row in trace_again] == ["first", "second"]


def test_audit_update_fails_at_db_level():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    event_id = str(uuid.uuid4())
    create_event(payment_id, event_id, status="failed", db_path=TEST_DB)
    append_audit(payment_id, event_id, "ingested", db_path=TEST_DB)

    conn = sqlite3.connect(TEST_DB)
    try:
        # Match on the trigger's actual RAISE(ABORT, ...) text, not just the
        # exception class — a NOT NULL/UNIQUE violation is also an
        # IntegrityError, and would make this pass for the wrong reason.
        with pytest.raises(sqlite3.IntegrityError, match="audit is append-only: UPDATE is not allowed"):
            conn.execute("UPDATE audit SET action = 'tampered' WHERE payment_id = ?", (payment_id,))
        conn.rollback()
    finally:
        conn.close()

    trace = get_audit_trace(payment_id, db_path=TEST_DB)
    assert trace[0]["action"] == "ingested"


def test_audit_delete_fails_at_db_level():
    payment_id = "pay_" + uuid.uuid4().hex[:14]
    event_id = str(uuid.uuid4())
    create_event(payment_id, event_id, status="failed", db_path=TEST_DB)
    append_audit(payment_id, event_id, "ingested", db_path=TEST_DB)

    conn = sqlite3.connect(TEST_DB)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="audit is append-only: DELETE is not allowed"):
            conn.execute("DELETE FROM audit WHERE payment_id = ?", (payment_id,))
        conn.rollback()
    finally:
        conn.close()

    trace = get_audit_trace(payment_id, db_path=TEST_DB)
    assert len(trace) == 1
