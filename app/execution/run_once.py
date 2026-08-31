"""Slice 10 PASS helper - one real execution against the configured provider.

    EXECUTION_MODE=razorpay_test LIVE_EXECUTION_ENABLED=1 \\
    .venv/Scripts/python -m app.execution.run_once \\
        --db exec.db --event-id evt_demo --action email \\
        --amount-paise 50000 --email you@example.test

Prints one line:  idem_key <k>  provider_ref <r>  status <s>
Re-running with the same --event-id/--action/--attempt-n returns the recorded
outcome and makes ZERO provider calls.
"""

from __future__ import annotations

import argparse

from app.execution.client import compute_idem_key
from app.execution.config import build_client
from app.execution.executor import execute


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Slice 10 one real execution")
    ap.add_argument("--db", required=True)
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--action", default="email")
    ap.add_argument("--attempt-n", type=int, default=1)
    ap.add_argument("--amount-paise", type=int, required=True)
    ap.add_argument("--email", default=None)
    ap.add_argument("--phone", default=None)
    ap.add_argument("--description", default=None)
    args = ap.parse_args(argv)

    payload = {
        "amount_paise": args.amount_paise,
        "currency": "INR",
        "email": args.email,
        "phone": args.phone,
        "description": args.description,
    }
    client = build_client()  # EXECUTION_MODE / LIVE_EXECUTION_ENABLED / RAZORPAY_* from env
    result = execute(
        client, db_path=args.db, event_id=args.event_id, action_type=args.action,
        attempt_n=args.attempt_n, payload=payload,
    )
    idem_key = compute_idem_key(args.event_id, args.action, args.attempt_n)
    print(f"idem_key {idem_key}  provider_ref {result.provider_ref}  status {result.status}")


if __name__ == "__main__":
    main()
