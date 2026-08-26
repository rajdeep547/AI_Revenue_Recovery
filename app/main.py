import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import (
    append_audit,
    create_event,
    get_audit_trace,
    get_event,
    init_db,
    mark_parsed,
    store_raw_event,
    update_event,
)
from app.security import verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("razorpay_webhook")

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# Only these two event types have a defined state transition in this slice.
# Anything else — even with a payment_id attached — is unhandled_event_type.
HANDLED_EVENT_TYPES = {"payment.failed", "payment.captured"}


def _extract_payment_entity(payload: dict) -> dict | None:
    """payload["payload"]["payment"]["entity"], or None if the shape doesn't match."""
    try:
        entity = payload["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return None
    return entity if isinstance(entity, dict) else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Razorpay Webhook Receiver", lifespan=lifespan)


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    headers = dict(request.headers)
    signature = headers.get("x-razorpay-signature")
    event_id = headers.get("x-razorpay-event-id")

    # 1. Verify signature over the raw bytes before touching anything else.
    if not verify_signature(raw_body, signature, RAZORPAY_WEBHOOK_SECRET):
        logger.warning("rejected webhook: bad signature (event_id=%s)", event_id)
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    # 2. Persist raw body + headers before parsing anything.
    is_new = store_raw_event(event_id, headers, raw_body)
    if not is_new:
        logger.info("duplicate webhook ignored (event_id=%s)", event_id)
        if event_id is not None:
            append_audit(None, event_id, "duplicate_delivery")
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # 3. Only now attempt to parse the body.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("stored but unparseable payload (event_id=%s)", event_id)
        if event_id is not None:
            append_audit(None, event_id, "parse_failed", {"error": "malformed json"})
        return JSONResponse(status_code=400, content={"error": "malformed json"})

    mark_parsed(event_id)
    event_type = payload.get("event")

    entity = _extract_payment_entity(payload)
    payment_id = entity.get("id") if entity else None

    # 4. No payment_id, or an event type this slice has no transition for
    # (payment.authorized, refund.*, etc.) — log it against the delivery
    # only and stop. Slices 5-8 add more transitions; this isn't one.
    if payment_id is None or event_type not in HANDLED_EVENT_TYPES:
        if event_id is not None:
            append_audit(None, event_id, "unhandled_event_type", {"event": event_type})
        logger.info("unhandled event type=%s id=%s", event_type, event_id)
        return JSONResponse(status_code=200, content={"status": "ok"})

    amount = entity.get("amount")
    method = entity.get("method")
    existing = get_event(payment_id)

    if event_type == "payment.failed":
        if existing is None:
            create_event(payment_id, event_id, status="failed", amount=amount, method=method)
            append_audit(payment_id, event_id, "ingested", {"event": event_type})
        else:
            update_event(payment_id, event_id)
            append_audit(payment_id, event_id, "duplicate_delivery", {"event": event_type})

    else:  # payment.captured
        if existing is None:
            create_event(payment_id, event_id, status="captured", amount=amount, method=method)
            append_audit(payment_id, event_id, "ingested", {"event": event_type})
        elif existing["status"] == "failed":
            update_event(payment_id, event_id, status="recovered", amount=amount, method=method)
            append_audit(
                payment_id,
                event_id,
                "outcome_observed",
                {"event": event_type, "from": "failed", "to": "recovered"},
            )
        else:
            update_event(payment_id, event_id)
            append_audit(payment_id, event_id, "duplicate_delivery", {"event": event_type})

    logger.info("processed event=%s payment_id=%s id=%s", event_type, payment_id, event_id)
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/events/{payment_id}/trace")
def event_trace(payment_id: str) -> dict:
    event = get_event(payment_id)
    if event is None:
        raise HTTPException(status_code=404, detail="payment not found")
    return {
        "payment_id": event["payment_id"],
        "amount": event["amount"],
        "method": event["method"],
        "status": event["status"],
        "first_event_id": event["first_event_id"],
        "last_event_id": event["last_event_id"],
        "created_at": event["created_at"],
        "updated_at": event["updated_at"],
        "trace": get_audit_trace(payment_id),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "alive"}
