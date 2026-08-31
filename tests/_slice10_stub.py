"""Slice 10 B5/B6 helper - a local stand-in for the Razorpay Payment Links API.

    python tests/_slice10_stub.py --port <p> --record <file> [--hang-post]

POST /v1/payment_links : append the raw request body (one JSON line) to
                          --record, then either hang forever (--hang-post,
                          emulating a crash between provider-accept and our
                          outcome write) or return 200 with a synthetic id.
GET  /v1/payment_links?reference_id=<k> : always return one "already created"
                          link for that reference_id (used by reconcile).
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_LINK_ID = "plink_stub_0000000001"


def _make_handler(record_path: str, hang_post: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            with open(record_path, "a", encoding="utf-8") as fh:
                fh.write(raw + "\n")
                fh.flush()
            if hang_post:
                while True:  # never respond
                    time.sleep(3600)
            ref = json.loads(raw).get("reference_id") if raw else None
            self._json(200, {"id": _LINK_ID, "reference_id": ref, "status": "created"})

        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            ref = (q.get("reference_id") or [""])[0]
            self._json(200, {"payment_links": [
                {"id": _LINK_ID, "reference_id": ref, "status": "created"}
            ]})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--hang-post", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), _make_handler(a.record, a.hang_post))
    srv.serve_forever()


if __name__ == "__main__":
    main()
