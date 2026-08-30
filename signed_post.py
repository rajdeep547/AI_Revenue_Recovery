import hmac, hashlib, json, os, urllib.request

secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
assert secret, "set RAZORPAY_WEBHOOK_SECRET first"

body = json.dumps({
    "event": "payment.failed",
    "payload": {"payment": {"entity": {
        "id": "pay_localtest3", "status": "failed", "amount": 50000,
        "method": "upi", "error_code": "BAD_REQUEST_ERROR",
        "error_description": "payment failed due to insufficient funds",
        "notes": {"customer_id": "cust_local", "email": "cust_local@example.test"},
    }}},
}, separators=(",", ":")).encode()

sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
req = urllib.request.Request(
    "http://localhost:8000/webhooks/razorpay", data=body,
    headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_localtest3"},
)
try:
    with urllib.request.urlopen(req) as r:
        print(r.status, r.read().decode()[:400])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:400])
