import hashlib
import hmac
import json
import os
import sqlite3
import uuid

import pytest

TEST_SECRET = "test_webhook_secret_123"
TEST_DB = "test_webhook_events.db"

os.environ["RAZORPAY_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["WEBHOOK_DB_PATH"] = TEST_DB

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def sign(body: bytes) -> str:
    return hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def row_count() -> int:
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]
    finally:
        conn.close()


def test_valid_webhook_returns_200():
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    event_id = str(uuid.uuid4())
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": sign(body),
                "x-razorpay-event-id": event_id,
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert row_count() == 1


def test_bad_signature_returns_401_and_does_not_store():
    body = json.dumps({"event": "payment.captured"}).encode()
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "x-razorpay-signature": "0" * 64,
                "x-razorpay-event-id": str(uuid.uuid4()),
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 401
    assert row_count() == 0


def test_duplicate_event_id_returns_200_but_one_row():
    body = json.dumps({"event": "payment.captured"}).encode()
    event_id = str(uuid.uuid4())
    headers = {
        "x-razorpay-signature": sign(body),
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }
    with TestClient(app) as client:
        first = client.post("/webhooks/razorpay", content=body, headers=headers)
        second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert row_count() == 1


def test_malformed_json_returns_400_and_process_stays_alive():
    body = b'{"broken":'
    headers = {
        "x-razorpay-signature": sign(body),
        "x-razorpay-event-id": str(uuid.uuid4()),
        "content-type": "application/json",
    }
    with TestClient(app) as client:
        resp = client.post("/webhooks/razorpay", content=body, headers=headers)
        # process must still be alive: a follow-up request should work fine
        health = client.get("/healthz")

    assert resp.status_code == 400
    assert health.status_code == 200
    assert health.json() == {"status": "alive"}
