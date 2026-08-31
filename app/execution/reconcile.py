"""Slice 10 - startup reconciliation.

For every intent whose latest outcome is missing or ``FAILED_RETRIABLE``, ask
the provider (by ``reference_id == idem_key``) what really happened:

  found     -> append SENT / DUPLICATE with the real provider_ref (adopted)
  not found -> leave non-terminal; do NOT auto-resend in this slice

Run:  python -m app.execution.reconcile --db <path> [--mode fake|razorpay_test]
"""

from __future__ import annotations

import argparse

from app.execution.client import TERMINAL_STATUSES
from app.execution.ledger import (
    _utcnow_iso,
    all_intents,
    append_outcome,
    init_execution_ledger,
    latest_outcome,
)


def reconcile(client, *, db_path, out=print) -> dict:
    init_execution_ledger(db_path)
    intents = all_intents(db_path)
    non_terminal = adopted = still_open = 0
    open_keys: list[str] = []

    for it in intents:
        key = it["idem_key"]
        latest = latest_outcome(db_path, key)
        if latest is not None and latest.status in TERMINAL_STATUSES:
            continue
        non_terminal += 1
        found = client.lookup(key)
        if found is not None and found.provider_ref:
            append_outcome(db_path, idem_key=key, result=found, now=_utcnow_iso())
            adopted += 1
        else:
            still_open += 1
            open_keys.append(key)

    out("-- reconcile --------------------------------")
    out(f"intents scanned   {len(intents)}")
    out(f"non-terminal      {non_terminal}")
    out(f"adopted           {adopted}")
    out(f"still open        {still_open}")
    for k in open_keys:
        out(f"  open: {k}")
    return {
        "intents": len(intents),
        "non_terminal": non_terminal,
        "adopted": adopted,
        "still_open": still_open,
        "open_keys": open_keys,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Slice 10 execution reconcile")
    ap.add_argument("--db", required=True)
    ap.add_argument("--mode", default=None, choices=["fake", "razorpay_test"])
    args = ap.parse_args(argv)

    import os

    from app.execution.config import _load_dotenv_once, build_client

    _load_dotenv_once()
    env = dict(os.environ)
    if args.mode:
        env["EXECUTION_MODE"] = args.mode
    reconcile(build_client(env_override=env), db_path=args.db)


if __name__ == "__main__":
    main()
