"""Slice 10 PASS check - independent. Re-derives every invariant straight from
the ledger tables; trusts no in-process reporting.

  1. GROUP BY idem_key over execution_outcomes -> <= 1 TERMINAL outcome each
  2. GROUP BY (event_id, action_type, attempt_n) -> <= 1 distinct provider_ref
  3. every provider_ref appears exactly once across the whole outcomes table

Prints: intents, terminal outcomes, distinct provider_refs, duplicates found
(must be 0). Exit code 0 iff every invariant holds.

    .venv/Scripts/python audit/slice10_idempotency.py --db exec.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

_TERMINAL = {"SENT", "DUPLICATE", "FAILED_TERMINAL"}


def audit(db_path: str, out=print) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        intents = con.execute("SELECT * FROM execution_intents").fetchall()
        outcomes = con.execute("SELECT * FROM execution_outcomes ORDER BY id").fetchall()
    finally:
        con.close()

    problems: list[str] = []

    # 1. <= 1 terminal outcome per idem_key
    by_key: dict[str, list] = {}
    for o in outcomes:
        by_key.setdefault(o["idem_key"], []).append(o)
    for key, rows in by_key.items():
        n_term = sum(1 for r in rows if r["status"] in _TERMINAL)
        if n_term > 1:
            problems.append(
                f"idem_key {key}: {n_term} terminal outcomes "
                f"({[r['status'] for r in rows if r['status'] in _TERMINAL]})"
            )

    # 2. <= 1 distinct provider_ref per business key (event_id, action_type, attempt_n)
    business_of = {
        i["idem_key"]: (i["event_id"], i["action_type"], i["attempt_n"]) for i in intents
    }
    refs_by_business: dict[tuple, set] = {}
    for o in outcomes:
        if not o["provider_ref"]:
            continue
        bkey = business_of.get(o["idem_key"], ("<no-intent>", o["idem_key"], -1))
        refs_by_business.setdefault(bkey, set()).add(o["provider_ref"])
    for bkey, refs in refs_by_business.items():
        if len(refs) > 1:
            problems.append(f"business key {bkey}: {len(refs)} distinct provider_refs {sorted(refs)}")

    # 3. every provider_ref appears exactly once across the whole table
    ref_counts: dict[str, int] = {}
    for o in outcomes:
        if o["provider_ref"]:
            ref_counts[o["provider_ref"]] = ref_counts.get(o["provider_ref"], 0) + 1
    duplicates = {r: n for r, n in ref_counts.items() if n > 1}
    for r, n in duplicates.items():
        problems.append(f"provider_ref {r} appears {n} times across execution_outcomes")

    n_terminal = sum(1 for o in outcomes if o["status"] in _TERMINAL)
    out(f"intents                {len(intents)}")
    out(f"outcomes (all)         {len(outcomes)}")
    out(f"terminal outcomes      {n_terminal}")
    out(f"distinct provider_refs {len(ref_counts)}")
    out(f"duplicates found       {len(duplicates)}")

    if problems:
        out("\nFAIL:")
        for p in problems:
            out(f"  - {p}")
        return 1
    out("\nOK - idempotency invariants hold (<=1 terminal/key, <=1 ref/business key, each ref once)")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Slice 10 idempotency audit")
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    sys.exit(audit(args.db))


if __name__ == "__main__":
    main()
