"""Slice 10 B5/B6 helper - the process the parent test SIGKILLs mid-flight.

  --mode b5 : run execute() normally against the stub; the stub records the
              POST then hangs, so the child blocks in the HTTP call AFTER the
              intent has been committed and BEFORE the outcome is written.
  --mode b6 : hang inside insert_intent's pre-commit hook -- the intent row is
              INSERTed but never COMMITted -- so a SIGKILL here must leave zero
              intent rows and zero provider requests.

Writes --marker as soon as it reaches the hang point, so the parent knows when
to kill.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from app.execution.executor import execute
from app.execution.razorpay_client import RazorpayClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--mode", required=True, choices=["b5", "b6"])
    ap.add_argument("--marker", required=True)
    a = ap.parse_args()

    client = RazorpayClient("rzp_test_stub", "stub_secret", base_url=a.base_url)
    payload = {
        "amount_paise": 50000, "currency": "INR",
        "email": "buyer@example.test", "description": "recovery",
    }

    hooks = {}
    if a.mode == "b6":
        def _pre_commit():
            Path(a.marker).write_text("at_precommit")
            while True:
                time.sleep(3600)
        hooks["_pre_commit"] = _pre_commit
    else:  # b5: the stub itself hangs on the POST; mark that we're about to call
        def _post_commit():
            Path(a.marker).write_text("intent_committed")
        hooks["_post_commit"] = _post_commit

    execute(
        client, db_path=a.db, event_id="evt_kill", action_type="email",
        attempt_n=1, payload=payload, **hooks,
    )


if __name__ == "__main__":
    main()
