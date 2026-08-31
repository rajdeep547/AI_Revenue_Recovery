"""Slice 12 -- independent audit of the LIVE dashboard numbers.

Re-derives every figure in the LIVE panel of ``/metrics`` from
``WEBHOOK_DB_PATH`` by a DIFFERENT query path than
``app.dashboard.live`` uses -- different SQL, different source columns, not the
same helper called twice -- and prints the dashboard value beside the audit
value with PASS / FAIL per metric.

Different-path choices (dashboard -> audit):

* terminal / skip counts   GROUP BY            -> per-value COUNT(*) in a loop
* arm                       decisions LEFT JOIN -> driven from normalized_events
                                                   with an IN (SELECT ...) filter
* arm invariant             (none)              -> every decisions row with
                                                   skip_reason='CONTROL_ARM' MUST
                                                   recompute to control; any
                                                   disagreement is a FAIL
* recovery signal           events.status       -> audit.action='outcome_observed'
* recovered value           decisions.ticket_inr-> normalized_events.amount_paise/100
* within-72h clock          events.updated_at   -> the outcome_observed audit row's
                                                   created_at
* dry-run send count        spend_ledger        -> COUNT of decisions terminal='ACT'
* would-be action cost      SUM decisions.action_cost_inr
                                                -> spend_ledger rung counts x the
                                                   policy cost_inr per rung
* real debit                status='debit'      -> status NOT IN ('dry_run','blocked')

Read-only throughout: the audit opens the DB as
``sqlite3.connect("file:...?mode=ro", uri=True)`` and writes nothing.

Exit code 0 iff every row is PASS.

Usage:
    python scripts/audit_metrics.py                     # WEBHOOK_DB_PATH or default
    python scripts/audit_metrics.py --db some.db
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.arms import assign_arm  # noqa: E402
from app.dashboard.live import load_live  # noqa: E402

_POLICY_PATH = REPO / "config" / "decision_policy.json"
_SEVENTY_TWO_H = timedelta(hours=72)
_UPLIFT_MIN_ARM_N = 30
_RUNGS = ("retry_silent", "email", "sms", "whatsapp", "agent_call")


# --------------------------------------------------------------------- helpers
def _seed_and_costs() -> tuple[int, dict[str, float]]:
    policy = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    seed = int(policy["experiment_seed"])
    costs = {r["name"]: float(r["cost_inr"]) for r in policy["action_ladder"]}
    return seed, costs


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_72h(occurred_at, transition_at) -> bool:
    a = _parse_iso(occurred_at)
    b = _parse_iso(transition_at)
    if a is None or b is None:
        return False
    return timedelta(0) <= (b - a) <= _SEVENTY_TWO_H


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _eq(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


# ------------------------------------------------------------- audit derivation
def audit_live(db_path: str) -> dict:
    """Independent re-derivation. Mirrors the keys of
    ``app.dashboard.live.load_live(...)['raw']``, plus ``_control_arm_invariant``.
    """
    seed, costs = _seed_and_costs()
    out: dict = {
        "decisions_total": 0,
        "terminal_counts": {},
        "skip_counts": {},
        "arm_treatment_n": 0,
        "arm_control_n": 0,
        "arm_unknown_n": 0,
        "control_share": None,
        "den_treatment_n": 0,
        "den_control_n": 0,
        "rec_treatment_n": 0,
        "rec_control_n": 0,
        "rate_treatment": None,
        "rate_control": None,
        "rate_pooled": None,
        "uplift_suppressed": True,
        "uplift_pp": None,
        "uplift_attributable": None,
        "would_be_action_cost_inr": 0.0,
        "dry_run_send_count": 0,
        "real_debit_inr": 0.0,
        "recovered_value_treatment_inr": 0.0,
        "recovered_value_control_inr": 0.0,
        "net_realised_ev_inr": None,
        "rung_counts": {r: 0 for r in _RUNGS},
        "_control_arm_invariant": (0, 0),  # (rows checked, disagreements)
    }
    if not os.path.exists(db_path):
        return out

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tbl = _tables(conn)

        # -- card 1: per-value counts in a loop (not GROUP BY) --------------
        if "decisions" in tbl:
            out["decisions_total"] = conn.execute(
                "SELECT COUNT(*) FROM decisions"
            ).fetchone()[0]
            terminals = [
                r[0] for r in conn.execute("SELECT DISTINCT terminal FROM decisions")
            ]
            for t in terminals:
                out["terminal_counts"][t] = conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE terminal = ?", (t,)
                ).fetchone()[0]
            skip_reasons = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT skip_reason FROM decisions WHERE terminal = 'SKIP'"
                )
            ]
            for sr in skip_reasons:
                key = sr if sr is not None else "(none)"
                if sr is None:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM decisions "
                        "WHERE terminal = 'SKIP' AND skip_reason IS NULL"
                    ).fetchone()[0]
                else:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM decisions "
                        "WHERE terminal = 'SKIP' AND skip_reason = ?", (sr,)
                    ).fetchone()[0]
                out["skip_counts"][key] = n

        # -- card 2: arm, driven from normalized_events ---------------------
        arm_by_pid: dict[str, str] = {}
        if {"decisions", "normalized_events"} <= tbl:
            rows = conn.execute(
                "SELECT reference, customer_id FROM normalized_events "
                "WHERE reference IN (SELECT payment_id FROM decisions)"
            ).fetchall()
            matched_refs = {ref for ref, _ in rows}
            decided_pids = {
                r[0] for r in conn.execute("SELECT payment_id FROM decisions")
            }
            for ref, cid in rows:
                if cid is None:
                    continue
                arm_by_pid[ref] = assign_arm(seed, cid)
            out["arm_unknown_n"] = len(decided_pids - matched_refs)
            out["arm_treatment_n"] = sum(
                1 for a in arm_by_pid.values() if a == "treatment"
            )
            out["arm_control_n"] = sum(
                1 for a in arm_by_pid.values() if a == "control"
            )
            n = out["arm_treatment_n"] + out["arm_control_n"]
            out["control_share"] = (out["arm_control_n"] / n) if n else None

            # -- genuine independent invariant --------------------------------
            checked = 0
            disagree = 0
            for (pid,) in conn.execute(
                "SELECT payment_id FROM decisions WHERE skip_reason = 'CONTROL_ARM'"
            ):
                checked += 1
                if arm_by_pid.get(pid) != "control":
                    disagree += 1
            out["_control_arm_invariant"] = (checked, disagree)

        # -- card 3: recovery from audit.action='outcome_observed' ---------
        capture_at: dict[str, str] = {}
        if "audit" in tbl:
            for pid, created_at in conn.execute(
                "SELECT payment_id, created_at FROM audit "
                "WHERE action = 'outcome_observed'"
            ):
                if pid is not None and pid not in capture_at:
                    capture_at[pid] = created_at

        norm: dict[str, tuple] = {}
        if "normalized_events" in tbl:
            for ref, cid, occurred_at, amount_paise in conn.execute(
                "SELECT reference, customer_id, occurred_at, amount_paise "
                "FROM normalized_events"
            ):
                norm[ref] = (cid, occurred_at, amount_paise)

        if {"decisions", "normalized_events"} <= tbl:
            for (pid,) in conn.execute("SELECT payment_id FROM decisions"):
                info = norm.get(pid)
                if info is None or info[0] is None:
                    continue
                cid, occurred_at, amount_paise = info
                arm = assign_arm(seed, cid)
                if arm == "control":
                    out["den_control_n"] += 1
                else:
                    out["den_treatment_n"] += 1
                cap = capture_at.get(pid)
                if cap is not None and _within_72h(occurred_at, cap):
                    value = (amount_paise or 0) / 100.0
                    if arm == "control":
                        out["rec_control_n"] += 1
                        out["recovered_value_control_inr"] += value
                    else:
                        out["rec_treatment_n"] += 1
                        out["recovered_value_treatment_inr"] += value

        dt, dc = out["den_treatment_n"], out["den_control_n"]
        rt, rc = out["rec_treatment_n"], out["rec_control_n"]
        out["rate_treatment"] = (rt / dt) if dt else None
        out["rate_control"] = (rc / dc) if dc else None
        out["rate_pooled"] = ((rt + rc) / (dt + dc)) if (dt + dc) else None

        # -- card 4: uplift ----------------------------------------------------
        suppressed = not (dt >= _UPLIFT_MIN_ARM_N and dc >= _UPLIFT_MIN_ARM_N)
        out["uplift_suppressed"] = suppressed
        if not suppressed and out["rate_treatment"] is not None and out["rate_control"] is not None:
            frac = out["rate_treatment"] - out["rate_control"]
            out["uplift_pp"] = frac * 100.0
            out["uplift_attributable"] = frac * dt

        # -- card 5: money, cross-checked from spend_ledger -----------------
        if "decisions" in tbl:
            out["dry_run_send_count"] = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE terminal = 'ACT'"
            ).fetchone()[0]
        if "spend_ledger" in tbl:
            for rung, c in conn.execute(
                "SELECT rung, COUNT(*) FROM spend_ledger "
                "WHERE status IN ('dry_run', 'debit') GROUP BY rung"
            ):
                out["would_be_action_cost_inr"] += c * costs.get(rung, 0.0)
            out["real_debit_inr"] = float(
                conn.execute(
                    "SELECT COALESCE(SUM(amount_inr), 0.0) FROM spend_ledger "
                    "WHERE status NOT IN ('dry_run', 'blocked')"
                ).fetchone()[0]
                or 0.0
            )

        if not suppressed and out["rate_control"] is not None:
            counterfactual_t = (out["recovered_value_control_inr"] / dc) * dt
            incr = out["recovered_value_treatment_inr"] - counterfactual_t
            out["net_realised_ev_inr"] = incr - out["real_debit_inr"]

        # -- card 6: rung distribution, per-value counts ------------------
        if "decisions" in tbl:
            acted_rungs = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT action FROM decisions "
                    "WHERE terminal = 'ACT' AND action IS NOT NULL"
                )
            ]
            for rung in acted_rungs:
                n = conn.execute(
                    "SELECT COUNT(*) FROM decisions "
                    "WHERE terminal = 'ACT' AND action = ?", (rung,)
                ).fetchone()[0]
                out["rung_counts"][rung] = out["rung_counts"].get(rung, 0) + n
    finally:
        conn.close()
    return out


# ----------------------------------------------------------------------- report
_METRIC_KEYS = [
    "decisions_total",
    "terminal_counts",
    "skip_counts",
    "arm_treatment_n",
    "arm_control_n",
    "arm_unknown_n",
    "control_share",
    "den_treatment_n",
    "den_control_n",
    "rec_treatment_n",
    "rec_control_n",
    "rate_treatment",
    "rate_control",
    "rate_pooled",
    "uplift_suppressed",
    "uplift_pp",
    "uplift_attributable",
    "would_be_action_cost_inr",
    "dry_run_send_count",
    "real_debit_inr",
    "recovered_value_treatment_inr",
    "recovered_value_control_inr",
    "net_realised_ev_inr",
    "rung_counts",
]


def run(db_path: str) -> int:
    dash = load_live(db_path)["raw"]
    aud = audit_live(db_path)

    name_w = max(len(k) for k in _METRIC_KEYS) + 2
    col_w = 34
    print(f"audit_metrics.py  --  db: {db_path}"
          f"{'  (file absent)' if not os.path.exists(db_path) else ''}")
    print("=" * (name_w + 2 * col_w + 8))
    print(f"{'METRIC':<{name_w}}{'DASHBOARD':<{col_w}}{'AUDIT':<{col_w}}RESULT")
    print("-" * (name_w + 2 * col_w + 8))

    failed = 0
    for key in _METRIC_KEYS:
        dv = dash.get(key)
        av = aud.get(key)
        ok = _eq(dv, av)
        if not ok:
            failed += 1
        print(
            f"{key:<{name_w}}{str(dv):<{col_w}}{str(av):<{col_w}}"
            f"{'PASS' if ok else 'FAIL'}"
        )

    checked, disagree = aud["_control_arm_invariant"]
    inv_ok = disagree == 0
    if not inv_ok:
        failed += 1
    print(
        f"{'control_arm_invariant':<{name_w}}"
        f"{('CONTROL_ARM rows: ' + str(checked)):<{col_w}}"
        f"{(str(disagree) + ' disagree'):<{col_w}}"
        f"{'PASS' if inv_ok else 'FAIL'}"
    )

    print("-" * (name_w + 2 * col_w + 8))
    total = len(_METRIC_KEYS) + 1
    print(f"{total - failed}/{total} PASS" + ("" if failed == 0 else f"  ({failed} FAIL)"))
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Slice 12 LIVE metrics audit")
    ap.add_argument(
        "--db",
        default=os.environ.get("WEBHOOK_DB_PATH", "webhook_events.db"),
        help="SQLite path (default: WEBHOOK_DB_PATH or webhook_events.db)",
    )
    args = ap.parse_args(argv)
    return run(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
