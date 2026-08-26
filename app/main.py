import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import init_db, mark_parsed, store_raw_event
from app.security import verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("razorpay_webhook")

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


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
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # 3. Only now attempt to parse the body.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("stored but unparseable payload (event_id=%s)", event_id)
        return JSONResponse(status_code=400, content={"error": "malformed json"})

    mark_parsed(event_id)
    logger.info("stored webhook event=%s id=%s", payload.get("event"), event_id)
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "alive"}
