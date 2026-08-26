import hmac, hashlib, json, os, uuid, sqlite3, sys, requests

BASE = "http://localhost:8000"
URL = f"{BASE}/webhooks/razorpay"
DB = "webhook_events.db"
SECRET = "slice1_test_secret_abc123".encode()

def sign(b): return hmac.new(SECRET, b, hashlib.sha256).hexdigest()
def post(b, s, e):
    return requests.post(URL, data=b, headers={
        "Content-Type": "application/json",
        "X-Razorpay-Signature": s,
        "x-razorpay-event-id": e,
    }).status_code

def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    return ok

body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
eid = "evt_localtest_" + uuid.uuid4().hex[:8]
bad = b'{"broken":'
r = []

r.append(check("valid delivery",      post(body, sign(body), eid), 200))
r.append(check("duplicate event id",  post(body, sign(body), eid), 200))
r.append(check("bad signature",       post(body, "deadbeef", "evt_" + uuid.uuid4().hex[:8]), 401))
r.append(check("malformed JSON",      post(bad, sign(bad), "evt_" + uuid.uuid4().hex[:8]), 400))
r.append(check("process still alive", requests.get(f"{BASE}/healthz").status_code, 200))

c = sqlite3.connect(DB)
r.append(check("duplicate stored once",
    c.execute("select count(*) from webhook_events where event_id=?", (eid,)).fetchone()[0], 1))

rows = c.execute("select event_id from webhook_events "
                 "where event_id not like 'evt_localtest%'").fetchall()
real = [x[0] for x in rows]
print(f"{'PASS' if real else 'FAIL'}  real Razorpay delivery: {len(real)} stored")
for x in real[:5]: print(f"      {x}")
r.append(bool(real))

print("\nSLICE 1 COMPLETE" if all(r) else "\nNOT COMPLETE — see FAIL lines")