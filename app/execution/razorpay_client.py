"""Slice 10 - Razorpay TEST-mode client. Target object: a Payment Link.

A recovery nudge = create a Payment Link for the failed amount and let the
customer complete payment from it.

PROVIDER-SIDE DEDUP. The link's ``reference_id`` carries our idem_key. Razorpay
Payment Links require a unique ``reference_id`` (<= 40 chars; the 32-hex key
fits) and REJECT a duplicate with HTTP 400 rather than returning the original
object. So:

  * fresh reference_id     -> 200, provider_ref = payment_link id ("plink_...") -> SENT
  * duplicate reference 400 -> the link already exists (a prior, possibly
                               crashed, send landed). GET
                               /v1/payment_links?reference_id=<key> recovers the
                               id -> DUPLICATE
  * other 4xx              -> FAILED_TERMINAL
  * 5xx / timeout / conn   -> FAILED_RETRIABLE (executor retries, SAME key)

Razorpay's ``X-Razorpay-Idempotency-Key`` header is NOT accepted on Payment
Links / core Payments (it is a RazorpayX Payouts / idempotent-Refunds feature),
so ``reference_id`` uniqueness is the ONLY provider-side mechanism here.
Verified against the public docs (Aug 2026); NOT yet confirmed by a live
test-mode run -- see DECISIONS.md Slice 10.

The key secret never reaches a log line or a ledger row: every error string is
run through ``redact(text, key_secret)``, and this module writes no logs at all.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode

from app.execution.client import ExecutionRequest, ExecutionResult, ExecutionStatus, redact

_DEFAULT_BASE = "https://api.razorpay.com"
_HTTP_TIMEOUT_S = 10.0


class RazorpayClient:
    def __init__(self, key_id: str, key_secret: str, *, base_url: str = _DEFAULT_BASE,
                 transport=None):
        # Hard gate at construction: test-mode keys only, never a live key.
        if not isinstance(key_id, str) or not key_id.startswith("rzp_test_"):
            raise ValueError(
                "RAZORPAY_KEY_ID must be a Razorpay TEST-mode key id "
                "(prefix 'rzp_test_'); refusing to construct"
            )
        if not key_secret:
            raise ValueError("RAZORPAY_KEY_SECRET is required")
        self._key_id = key_id
        self._key_secret = key_secret
        self._base = base_url.rstrip("/")
        self._auth = "Basic " + base64.b64encode(
            f"{key_id}:{key_secret}".encode("utf-8")
        ).decode("ascii")
        self._transport = transport or self._urllib_transport

    # ---- HTTP -----------------------------------------------------------
    def _urllib_transport(self, method, url, headers, body):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"error": {"description": raw[:200]}}
            return e.code, parsed
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            return None, {"__transport_error__": type(e).__name__}

    def _call(self, method, path, *, body=None, params=None):
        url = self._base + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": self._auth, "Content-Type": "application/json"}
        return self._transport(method, url, headers, body)

    # ---- ActionClient -------------------------------------------------
    def send(self, req: ExecutionRequest) -> ExecutionResult:
        status, data = self._call("POST", "/v1/payment_links", body=self._link_body(req))

        if status is None:  # timeout / connection error
            return ExecutionResult(
                ExecutionStatus.FAILED_RETRIABLE, http_status=None,
                error=redact(f"transport:{data.get('__transport_error__')}", self._key_secret),
            )
        if 200 <= status < 300:
            return ExecutionResult(
                ExecutionStatus.SENT, provider_ref=data.get("id"), http_status=status
            )

        desc = _err_desc(data)
        if status == 400 and _looks_like_dup_reference(desc):
            found = self.lookup(req.idem_key)
            return ExecutionResult(
                ExecutionStatus.DUPLICATE,
                provider_ref=found.provider_ref if found else None,
                http_status=status,
                error=redact("duplicate reference_id", self._key_secret),
            )
        if 400 <= status < 500:
            return ExecutionResult(
                ExecutionStatus.FAILED_TERMINAL, http_status=status,
                error=redact(desc, self._key_secret),
            )
        return ExecutionResult(  # 5xx
            ExecutionStatus.FAILED_RETRIABLE, http_status=status,
            error=redact(desc, self._key_secret),
        )

    def lookup(self, idem_key: str) -> ExecutionResult | None:
        status, data = self._call(
            "GET", "/v1/payment_links", params={"reference_id": idem_key}
        )
        if status is None or not (200 <= status < 300):
            return None
        links = data.get("payment_links") or data.get("items") or []
        if not links:
            return None
        return ExecutionResult(
            ExecutionStatus.SENT, provider_ref=links[0].get("id"), http_status=status
        )

    # ---- helpers ----------------------------------------------------
    def _link_body(self, req: ExecutionRequest) -> dict:
        p = req.payload
        amount = int(p.get("amount_paise") or p.get("amount") or 0)
        customer: dict = {}
        if p.get("email"):
            customer["email"] = p["email"]
        if p.get("phone"):
            customer["contact"] = p["phone"]
        if p.get("customer_name"):
            customer["name"] = p["customer_name"]
        return {
            "amount": amount,
            "currency": p.get("currency", "INR"),
            "accept_partial": False,
            "reference_id": req.idem_key,  # <-- the provider-side dedup key
            "description": (p.get("description") or f"Payment recovery ({req.action_type})")[:255],
            "customer": customer,
            "notify": {"sms": bool(customer.get("contact")), "email": bool(customer.get("email"))},
            "reminder_enable": False,
        }


def _err_desc(data) -> str:
    if not isinstance(data, dict):
        return str(data)[:200]
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("description") or err.get("code") or err)[:200]
    return str(err if err is not None else data)[:200]


def _looks_like_dup_reference(desc: str) -> bool:
    d = (desc or "").lower()
    return "reference" in d and ("already" in d or "exist" in d or "duplicate" in d)
