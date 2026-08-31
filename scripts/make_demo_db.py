"""Regenerate ``data/demo.db``: the 2,000-event corpus driven through the REAL
decision pipeline, every decision persisted.

NOT a parallel pipeline. It calls :func:`scripts.run_corpus.build_decision_db`
-- the exact ingest + :func:`app.pipeline.process_failure` + read-back path
that ``run_corpus.py final`` (STEP 1) already uses to persist decisions.
``run_corpus.py`` builds that table into a throwaway scratch DB; the only thing
added there is a ``--decisions-db`` keep-path flag, which this script points at
``data/demo.db``.

Guarantees:

* writes ``data/demo.db`` and nothing else;
* aborts if the target resolves to ``WEBHOOK_DB_PATH`` or is named
  ``webhook_events.db``;
* schema from the real init functions, decisions from the real engine, arms
  from ``assign_arm`` -- no hand-written rows;
* deterministic: same seed -> identical ``decisions.inputs_hash`` set every run
  (``build_decision_db`` uses each event's own payload ``created_at``, never a
  wall-clock);
* regenerated, never edited by hand; git-ignored.

    python scripts/make_demo_db.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.decision.engine import load_policy  # noqa: E402
from scripts.run_corpus import DEFAULT_EVENTS, build_decision_db  # noqa: E402

DEFAULT_TARGET = REPO / "data" / "demo.db"


def resolve_target(argv: list[str]) -> Path:
    """The path to (re)build. ``argv[1]`` overrides the default. Aborts (exit
    1, nothing written) if it resolves to the live webhook DB or is named
    ``webhook_events.db``."""
    target = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_TARGET.resolve()

    forbidden = {Path("webhook_events.db").resolve()}
    env = os.environ.get("WEBHOOK_DB_PATH")
    if env:
        forbidden.add(Path(env).resolve())

    if target in forbidden or target.name == "webhook_events.db":
        raise SystemExit(
            f"ABORT: refusing to write {target} -- it resolves to the live "
            f"webhook DB (WEBHOOK_DB_PATH={env!r}). This script only writes "
            f"data/demo.db."
        )
    return target


def build(target: Path) -> dict:
    """(Re)create ``target`` from the corpus and return ``{payment_id: row}``."""
    policy = load_policy()
    built = build_decision_db(DEFAULT_EVENTS, policy, target)
    return built["decision_by_pid"]


def terminal_distribution(decision_by_pid: dict) -> Counter:
    return Counter(d["terminal"] for d in decision_by_pid.values())


def main(argv: list[str]) -> int:
    target = resolve_target(argv)
    decisions = build(target)

    dist = terminal_distribution(decisions)
    hashes = {d["inputs_hash"] for d in decisions.values()}

    print(
        f"wrote {target}\n  {len(decisions)} decisions, "
        f"{len(hashes)} distinct inputs_hash\n"
    )
    print("terminal distribution:")
    for term, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {term:26s} {n:>5d}")

    route = dist.get("ROUTE_TO_HUMAN", 0)
    blocked = sum(n for t, n in dist.items() if str(t).startswith("BLOCKED/"))
    if route == 0 and blocked == 0:
        print(
            "\nFINDING: ROUTE_TO_HUMAN and BLOCKED/* are both zero on this "
            "corpus. Those paths never fire here -- the rules classifier emits "
            "only insufficient_funds / bank_downtime, phone coverage is ~0 so "
            "the ladder collapses to the email rung, and email EV clears the "
            "floor for every ticket in the corpus. This is a property of the "
            "corpus + policy, not a bug to patch."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
