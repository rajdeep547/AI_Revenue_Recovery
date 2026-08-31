"""Slice 11 - full run and freeze.

Runs what already exists and pins the output. Invents no behaviour:

  * ingest        app.ingest.Ingestor + the card_failure adapter
  * diagnose      app.diagnosis.diagnose (rules, error_code only)
  * assign arm    app.arms.assign_arm  (sha256("assign:{seed}:{cid}"))
  * decide        app.pipeline.process_failure  -> app.decision.engine
                  (STEP 1: the REAL pipeline, so the guardrail walk is real)
                  app.decision.engine.decide_with_ladder
                  (STEP 2: the only existing seam that accepts a
                   treatment_fraction override; config is not touched)
  * resolve       eval.environment.Environment.resolve
                  (static per-customer counterfactual coin,
                   sha256("{seed}:{customer_id}") -- no time component,
                   hence attribution_window is "none" on every row)

DETERMINISM IS THE POINT. The committed artifacts
(results/final_run.json, results/split_comparison_500.json) must be
byte-identical across runs:

  * sort_keys, every float rounded to 6 dp, per-event rows sorted by event_id
  * NO wall-clock / uuid / duration / run_id inside the artifact -- those go
    to the sibling .meta.json (gitignored)
  * every random draw keys off the committed seeds and the assign:/resolve:
    hash prefixes already in app/ and eval/ -- no new unseeded source

Usage
-----
  python scripts/run_corpus.py final --out results/final_run.json
  python scripts/run_corpus.py split --limit-customers 500 \
        --splits 70:30,90:10 --out results/split_comparison_500.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.arms import assign_arm  # noqa: E402
from app.decision.engine import (  # noqa: E402
    _P_RUNG_CAP,
    Terminal,
    _compute_ladder,
    decide_with_ladder,
    load_policy,
)
from app.diagnosis import diagnose  # noqa: E402
from app.ingest import Ingestor  # noqa: E402
from app.pipeline import RULES_CONFIDENCE, process_failure  # noqa: E402
from eval.environment import Environment  # noqa: E402
from eval.measurement import load_population, run_policy  # noqa: E402

DEFAULT_EVENTS = REPO / "data" / "events.json"
DEFAULT_GROUND_TRUTH = REPO / "data" / "ground_truth.json"
DEFAULT_POLICY = REPO / "config" / "decision_policy.json"
ERROR_CODE_MAP = REPO / "rules" / "error_code_map.json"
GUARDRAILS_CONFIG = REPO / "config" / "guardrails.json"

# scipy.stats.norm.ppf(0.975) -- identical to eval.measurement._Z_95
Z_95 = 1.959963984540054

FLOAT_DP = 6

PROCESSING_ORDER = (
    "events processed in ascending payload created_at (unix epoch), ties "
    "broken by ascending event_id, so cooldown/guardrail state accumulates in "
    "corpus order; the per-event output block below is independently sorted by "
    "event_id (processing order and output order are separate concerns)"
)
ATTRIBUTION_WINDOW_NOTE = (
    "there is no time-windowed attribution in this system: `recovered` is a "
    "static per-customer counterfactual draw "
    "(eval.environment.Environment.resolve, sha256(\"{seed}:{customer_id}\")), "
    "so attribution_window is \"none\" on every row"
)
ACTION_TO_RECOVERY_MAPPING = (
    "terminal ACT -> customer resolved under action \"nudge\"; terminal SKIP / "
    "ROUTE_TO_HUMAN / BLOCKED/* -> resolved under \"none\"; every control-arm "
    "customer is resolved under \"none\" (the untouched baseline)"
)


# --------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str:
    out = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def iso_utc(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def require_timestamps(events: list[dict]) -> None:
    """Corrected Slice 11 spec: every event's now_utc is derived from its own
    committed payload timestamp -- the top-level integer `created_at` (unix
    epoch, UTC) on each object in data/events.json. There is no per-event
    timestamp anywhere else in the payload. If any event lacks a usable one,
    STOP; never fall back to wall-clock or to a pinned constant."""
    bad = [
        e.get("event_id", "<no event_id>")
        for e in events
        if not isinstance(e.get("created_at"), (int, float))
        or isinstance(e.get("created_at"), bool)
    ]
    if bad:
        raise SystemExit(
            "STOP: %d event(s) have no usable integer `created_at` timestamp "
            "(first few: %s). Corrected spec forbids a wall-clock or constant "
            "fallback." % (len(bad), bad[:5])
        )


def round_floats(obj):
    """Recursively round every float to FLOAT_DP and normalise -0.0 -> 0.0 so
    json.dumps is byte-stable across runs."""
    if isinstance(obj, float):
        r = round(obj, FLOAT_DP)
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        return {k: round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v) for v in obj]
    return obj


def dump_json(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(round_floats(doc), sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def write_meta(path: Path, extra: dict) -> None:
    """Wall-clock / environment facts that must NOT sit in the committed
    artifact. Sibling <name>.meta.json -- gitignored."""
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "argv": sys.argv,
        **extra,
    }
    path.write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------ stats primitives
def wilson_ci(x: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a single binomial proportion."""
    if n == 0:
        return float("nan"), float("nan")
    p = x / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return centre - half, centre + half


def newcombe_diff_ci(
    x1: int, n1: int, x2: int, n2: int, z: float = Z_95
) -> tuple[float, float]:
    """Newcombe (1998) 'method 10' hybrid score interval for the difference
    p1 - p2, built from the two single-proportion Wilson intervals. This is
    the 'Wilson on the difference' interval named in the Slice 11 spec.
    """
    p1 = x1 / n1
    p2 = x2 / n2
    l1, u1 = wilson_ci(x1, n1, z)
    l2, u2 = wilson_ci(x2, n2, z)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return lo, hi


def pearson(xs: list[float], ys: list[float]) -> float:
    """Plain Pearson r. 0.0 for a degenerate (zero-variance) series."""
    n = len(xs)
    if n == 0:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return cov / (sx * sy)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _two_sig_figs(x: float) -> float:
    """Round to two significant figures -- for order-of-magnitude statements
    where more digits would be false precision."""
    if not math.isfinite(x) or x <= 0.0:
        return x
    digits = math.floor(math.log10(x))
    return round(x, -(int(digits) - 1))


def email_breakeven_ticket_inr(policy: dict, cause: str) -> float:
    """The ticket at which the email rung's EV = min_ev_inr for `cause`, i.e.
    the ticket below which this event WOULD skip EV_BELOW_FLOOR at the email
    rung. Solved from the engine's own arithmetic:
        p_effective = conf*prior + (1-conf)*population_incremental   (engine step c)
        p_rung      = min(p_effective * email.effectiveness, _P_RUNG_CAP)
        EV          = p_rung * ticket - email.cost_inr
    conf is the flat rules-path confidence (app.pipeline.RULES_CONFIDENCE);
    history_multiplier is 1.0 on this corpus (no capture feed -> no prior
    control self-recovery)."""
    priors = policy["incremental_priors"]
    pop_inc = policy["population_incremental"]
    email = next(r for r in policy["action_ladder"] if r["name"] == "email")
    floor = policy["min_ev_inr"]
    p_eff = (
        RULES_CONFIDENCE * priors[cause]["p_incremental"]
        + (1.0 - RULES_CONFIDENCE) * pop_inc
    )
    p_rung = min(p_eff * email["effectiveness"], _P_RUNG_CAP)
    return (floor + email["cost_inr"]) / p_rung


def _bootstrap_seed(seed: int, i: int) -> int:
    """Per-resample RNG seed. NEW hash namespace 'bootstrap:' -- deliberately
    distinct from 'assign:' (arm assignment) and the bare '{seed}:{cid}'
    outcome-resolution draw, so the resampling shares no entropy with either."""
    digest = hashlib.sha256(f"bootstrap:{seed}:{i}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _percentile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 1]) of an already-sorted list."""
    if not sorted_xs:
        return float("nan")
    idx = q * (len(sorted_xs) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_xs[int(lo)]
    frac = idx - lo
    return sorted_xs[int(lo)] * (1.0 - frac) + sorted_xs[int(hi)] * frac


def bootstrap_incremental_value(
    seed: int,
    t_recovered_values: list[float],
    c_recovered_values: list[float],
    n_treatment: int,
    action_cost_distinct: float,
    n_resamples: int = 10000,
) -> dict:
    """Stratified (within-arm, with replacement, at observed n) percentile
    bootstrap for the counterfactual-subtracted incremental recovered value.

    Per-customer recovered value is zero-inflated and heavy-tailed, so this is
    a percentile interval, not a normal approximation. The per-resample
    statistic is identical in form to the point estimate:
        per_treated = mean(resampled treatment recovered value)
                    - mean(resampled control recovered value)
    Deterministic: resample i draws from `random.Random(_bootstrap_seed(...))`,
    so the same bytes come out every run (Gate B stays three-way identical).
    """
    import random

    tn = len(t_recovered_values)
    cn = len(c_recovered_values)
    per_treated: list[float] = []
    gross: list[float] = []
    net: list[float] = []
    for i in range(n_resamples):
        rng = random.Random(_bootstrap_seed(seed, i))
        tb = sum(rng.choices(t_recovered_values, k=tn)) / tn
        cb = sum(rng.choices(c_recovered_values, k=cn)) / cn
        pt = tb - cb
        per_treated.append(pt)
        g = pt * n_treatment
        gross.append(g)
        net.append(g - action_cost_distinct)
    per_treated.sort()
    gross.sort()
    net.sort()
    method = (
        f"stratified within-arm percentile bootstrap, {n_resamples} resamples; "
        f"treatment n={tn} and control n={cn} resampled independently, with "
        f"replacement, at observed n; per-resample RNG seeded "
        f"sha256('bootstrap:{{seed}}:{{i}}'); 2.5/97.5 linear-interpolated "
        f"percentiles"
    )
    return {
        "per_treated_low": _percentile(per_treated, 0.025),
        "per_treated_high": _percentile(per_treated, 0.975),
        "gross_low": _percentile(gross, 0.025),
        "gross_high": _percentile(gross, 0.975),
        "net_low": _percentile(net, 0.025),
        "net_high": _percentile(net, 0.975),
        "method": method,
        "n_resamples": n_resamples,
    }


def uplift_ci_block(t_rec: int, t_n: int, c_rec: int, c_n: int) -> dict:
    lo, hi = newcombe_diff_ci(t_rec, t_n, c_rec, c_n)
    return {
        "method": "Newcombe 1998 hybrid score (Wilson-based) on the difference, 95%",
        "low": lo,
        "high": hi,
        "width": hi - lo,
        "low_pp": lo * 100.0,
        "high_pp": hi * 100.0,
        "width_pp": (hi - lo) * 100.0,
    }


# ---------------------------------------------------------------- event build
def build_event(row: dict, now_utc: str) -> dict:
    """The engine `event` dict, built exactly as app.pipeline.process_failure
    builds it (pipeline.py lines 298-310)."""
    entity = row["raw"]["payload"]["payment"]["entity"]
    return {
        "payment_id": row["reference"],
        "cause": diagnose(row),
        "cause_confidence": RULES_CONFIDENCE,
        "ticket_inr": row["amount_paise"] / 100.0,
        "risk_blocked": entity.get("error_reason") == "risk_declined",
        "already_recovered": entity.get("status") in ("captured", "recovered"),
        "email": row["email"],
        "phone": row["phone"],
        "now_utc": now_utc,
    }


def best_rung_and_ev(policy: dict, event: dict, decision_row: dict) -> tuple:
    """(best_rung_name, ev_at_chosen_rung) using the engine's own
    `_compute_ladder`. Only defined once the ladder was reached
    (p_effective is not NULL); pre-ladder skips return (None, None).

    `ev_at_chosen_rung`:
      * ACT, no walk-down  -> EV of the best (== chosen) rung
      * ACT, walked down   -> EV of the rung actually chosen
      * CONTROL_ARM / EV_BELOW_FLOOR / ROUTE_TO_HUMAN -> EV of the best rung
        the skip/route was decided against
    """
    p_eff = decision_row["p_effective"]
    if p_eff is None:
        return None, None
    best, ladder = _compute_ladder(policy, event, p_eff, event["ticket_inr"])
    chosen = decision_row["action"]
    if chosen and chosen != best.name:
        ev = next(e.ev for e in ladder if e.name == chosen)
        return best.name, ev
    return best.name, best.ev


# ------------------------------------------------------------------- provenance
def provenance(events_path: Path, ground_truth_path: Path, policy: dict,
               corpus_min: str, corpus_max: str, n_customers: int,
               n_events: int) -> dict:
    seed = policy["experiment_seed"]
    return {
        "head_commit_sha": git_head(),
        "sha256_events_json": sha256_file(events_path),
        "sha256_decision_policy_json": sha256_file(DEFAULT_POLICY),
        "sha256_error_code_map_json": sha256_file(ERROR_CODE_MAP),
        "sha256_ground_truth_json": sha256_file(ground_truth_path),
        "sha256_guardrails_json": sha256_file(GUARDRAILS_CONFIG),
        "rng_seed": seed,
        "rng_seed_basis": (
            "single value 20260826: config/decision_policy.json.experiment_seed "
            "(arm assignment, sha256(\"assign:{seed}:{cid}\")) AND "
            "data/ground_truth.json.meta.seed (outcome resolution, "
            "sha256(\"{seed}:{cid}\")). The value-weighted bootstrap CI adds a "
            "THIRD, non-overlapping namespace: per-resample RNG seeded "
            "sha256(\"bootstrap:{seed}:{i}\") for i in 0..n_resamples-1. No "
            "other random source is used."
        ),
        "bootstrap_seed_basis": (
            "sha256(\"bootstrap:20260826:{i}\") -> first 8 bytes -> big-endian "
            "int -> random.Random seed, one per resample. Deliberately distinct "
            "from the \"assign:\" and bare \"{seed}:{cid}\" prefixes so the "
            "resampling shares no entropy with arm assignment or outcome "
            "resolution."
        ),
        "corpus_time_min": corpus_min,
        "corpus_time_max": corpus_max,
        "n_customers": n_customers,
        "n_events": n_events,
        "policy_version": policy["policy_version"],
    }


# =========================================================================
# STEP 1 -- full run, 2,000 events, locked 70/30, via the REAL pipeline
# =========================================================================
def run_final(args) -> None:
    t0 = time.time()
    events_path = Path(args.events)
    ground_truth_path = Path(args.ground_truth)
    policy = load_policy(args.policy)
    seed = policy["experiment_seed"]

    doc = json.loads(events_path.read_text(encoding="utf-8"))
    events = doc["events"]
    require_timestamps(events)

    def ekey(ev):
        return (ev["created_at"], ev["event_id"])

    events_sorted = sorted(events, key=ekey)
    corpus_min = iso_utc(events_sorted[0]["created_at"])
    corpus_max = iso_utc(events_sorted[-1]["created_at"])

    out_path = Path(args.out)
    scratch_db = out_path.parent / ("_%s.db" % out_path.stem)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if scratch_db.exists():
        scratch_db.unlink()

    # -- ingest every event through the real adapter, in corpus order --
    with Ingestor(str(scratch_db)) as ing:
        for ev in events_sorted:
            payload = {**ev["payload"], "created_at": ev["created_at"]}
            ing.ingest("card_failure", payload)
        rows = ing.rows()
    row_by_pid = {r["reference"]: r for r in rows}

    # -- drive the real pipeline once per event, in corpus order --
    # (a second call for a payment_id is idempotent: it returns the decision
    #  already on record and writes nothing.)
    for ev in events_sorted:
        entity = ev["payload"]["payload"]["payment"]["entity"]
        process_failure(
            entity["id"],
            db_path=str(scratch_db),
            policy=policy,
            now_utc=iso_utc(ev["created_at"]),
        )

    # -- read back the authoritative decision record --
    import sqlite3

    con = sqlite3.connect(str(scratch_db))
    con.row_factory = sqlite3.Row
    dec_rows = con.execute("SELECT * FROM decisions").fetchall()
    con.close()
    decision_by_pid = {r["payment_id"]: dict(r) for r in dec_rows}

    if set(decision_by_pid) != set(row_by_pid):
        missing = sorted(set(row_by_pid) - set(decision_by_pid))
        raise SystemExit(
            f"STOP: {len(missing)} ingested payments have no decision row "
            f"(first few: {missing[:5]})"
        )

    env = Environment(str(ground_truth_path))
    if env.run_seed != seed:
        raise SystemExit(
            f"STOP: ground_truth seed {env.run_seed} != policy experiment_seed {seed}"
        )

    # -- per customer: arm, best rung, EV at chosen rung, recovered --
    per_customer: dict[str, dict] = {}
    for pid, drow in decision_by_pid.items():
        row = row_by_pid[pid]
        cid = row["customer_id"]
        now_utc = row["occurred_at"]
        event = build_event(row, now_utc)
        arm = assign_arm(seed, cid)

        best_rung, ev_chosen = best_rung_and_ev(policy, event, drow)

        # consistency gate: the engine's own ladder must reproduce the
        # persisted best-rung EV for every ladder-reached decision.
        if drow["p_effective"] is not None and drow["ev_inr"] is not None:
            best, _ = _compute_ladder(
                policy, event, drow["p_effective"], event["ticket_inr"]
            )
            if abs(best.ev - drow["ev_inr"]) > 1e-9:
                raise SystemExit(
                    f"STOP: ladder recompute for {pid} gives EV {best.ev!r} but "
                    f"decisions.ev_inr is {drow['ev_inr']!r}"
                )

        action_for_env = "nudge" if drow["terminal"] == Terminal.ACT else "none"
        recovered = env.resolve(cid, action_for_env)

        per_customer[cid] = {
            "payment_id": pid,
            "cause": drow["cause"],
            "confidence": drow["cause_confidence"],
            "ticket_inr": drow["ticket_inr"],
            "arm": arm,
            "best_rung": best_rung,
            "ev_at_chosen_rung": ev_chosen,
            "terminal": drow["terminal"],
            "action": drow["action"],
            "skip_reason": drow["skip_reason"],
            "action_cost_inr": drow["action_cost_inr"],
            "recovered": bool(recovered),
        }

    # -- per-event block: 2,000 rows, one per event, sorted by event_id --
    cid_by_pid = {r["reference"]: r["customer_id"] for r in rows}
    per_event = []
    for ev in events:
        entity = ev["payload"]["payload"]["payment"]["entity"]
        pid = entity["id"]
        cid = cid_by_pid[pid]
        d = per_customer[cid]
        per_event.append({
            "event_id": ev["event_id"],
            "customer_id": cid,
            "payment_id": pid,
            "cause": d["cause"],
            "confidence": d["confidence"],
            "ticket_inr": d["ticket_inr"],
            "arm": d["arm"],
            "best_rung": d["best_rung"],
            "ev_at_chosen_rung": d["ev_at_chosen_rung"],
            "terminal": d["terminal"],
            "recovered": d["recovered"],
            "attribution_window": "none",
        })
    per_event.sort(key=lambda r: r["event_id"])

    # -- event -> customer count, for the event-level aggregates --
    events_per_customer = Counter(cid_by_pid[e["payload"]["payload"]["payment"]
                                  ["entity"]["id"]] for e in events)

    # ---- customer-level aggregate (n_customers = 1611) ----
    treat = [d for d in per_customer.values() if d["arm"] == "treatment"]
    ctrl = [d for d in per_customer.values() if d["arm"] == "control"]
    # a treatment customer fully blocked by guardrails on every rung is a
    # third bucket, excluded from the uplift denominator (see eval.measurement).
    t_blocked = [d for d in treat if str(d["terminal"]).startswith("BLOCKED/")]
    t_acted = [d for d in treat if not str(d["terminal"]).startswith("BLOCKED/")]

    t_n = len(t_acted)
    c_n = len(ctrl)
    t_rec = sum(1 for d in t_acted if d["recovered"])
    c_rec = sum(1 for d in ctrl if d["recovered"])
    t_rate = t_rec / t_n if t_n else float("nan")
    c_rate = c_rec / c_n if c_n else float("nan")
    uplift = t_rate - c_rate

    total_recovered_value = sum(
        d["ticket_inr"] for d in per_customer.values() if d["recovered"]
    )
    recovered_value_treatment = sum(
        d["ticket_inr"] for d in t_acted if d["recovered"]
    )
    recovered_value_control = sum(
        d["ticket_inr"] for d in ctrl if d["recovered"]
    )

    # ---- event-level aggregate (n_events = 2000) ----
    # process_failure is idempotent per payment_id, so an ACT decision for a
    # multi-failure customer is ONE distinct send that N events map onto. Count
    # both: distinct (unique payment_id per rung) and event-weighted.
    actions_events: Counter = Counter()
    actions_distinct: Counter = Counter()
    skip_counts: Counter = Counter()
    route_to_human = 0
    suppressed_by_guardrail = 0
    action_cost_events = 0.0
    action_cost_distinct = 0.0
    for cid, d in per_customer.items():
        n_ev = events_per_customer[cid]
        term = d["terminal"]
        if term == Terminal.ACT:
            actions_events[d["action"]] += n_ev
            actions_distinct[d["action"]] += 1
            action_cost_events += (d["action_cost_inr"] or 0.0) * n_ev
            action_cost_distinct += (d["action_cost_inr"] or 0.0)
        elif term == Terminal.ROUTE_TO_HUMAN:
            route_to_human += n_ev
        elif str(term).startswith("BLOCKED/"):
            suppressed_by_guardrail += n_ev
        else:  # SKIP
            skip_counts[d["skip_reason"] or "UNKNOWN"] += n_ev

    event_level = {
        "n_events": len(per_event),
        "action_counts_by_rung": dict(sorted(actions_events.items())),
        "distinct_actions_by_rung": dict(sorted(actions_distinct.items())),
        "skip_counts_by_reason": dict(sorted(skip_counts.items())),
        "route_to_human_count": route_to_human,
        "suppressed_by_guardrail_count": suppressed_by_guardrail,
        "total_action_cost_inr": action_cost_distinct,
        "total_action_cost_events_inr": action_cost_events,
        "action_cost_note": (
            "total_action_cost_inr is over DISTINCT actions (unique payment_id "
            "per rung -- process_failure is idempotent, so one send per "
            "customer); total_action_cost_events_inr multiplies each send by "
            "that customer's event count and is kept only for comparison"
        ),
    }

    # ---- FIX 1: incremental (counterfactual-subtracted) value, not raw ----
    counterfactual_value_treatment = (
        (recovered_value_control / c_n) * t_n if c_n else float("nan")
    )
    incremental_recovered_value = (
        recovered_value_treatment - counterfactual_value_treatment
    )
    incremental_value_per_treated = (
        incremental_recovered_value / t_n if t_n else float("nan")
    )
    net_incremental_ev = incremental_recovered_value - action_cost_distinct

    # ---- FIX 2: count basis vs value basis, and where they diverge ----
    incremental_recoveries_count = t_rec - (c_rate * t_n)
    rec_t_tickets = [d["ticket_inr"] for d in t_acted if d["recovered"]]
    rec_c_tickets = [d["ticket_inr"] for d in ctrl if d["recovered"]]
    mean_ticket_recovered_treatment = mean(rec_t_tickets)
    mean_ticket_recovered_control = mean(rec_c_tickets)
    pooled_mean_recovered_ticket = mean(rec_t_tickets + rec_c_tickets)

    mean_ticket_all_treatment = mean([d["ticket_inr"] for d in treat])
    mean_ticket_all_control = mean([d["ticket_inr"] for d in ctrl])
    ticket_abs_diff = mean_ticket_all_treatment - mean_ticket_all_control
    ticket_pct_diff = (
        ticket_abs_diff / mean_ticket_all_control * 100.0
        if mean_ticket_all_control else float("nan")
    )
    ticket_balance_check = {
        "mean_ticket_all_treatment_inr": mean_ticket_all_treatment,
        "mean_ticket_all_control_inr": mean_ticket_all_control,
        "abs_difference_inr": ticket_abs_diff,
        "pct_difference": ticket_pct_diff,
        "basis": "mean ticket over ALL customers in each arm, not just recovered",
    }

    # the true incremental band: nudge recovers, do-nothing would not. Computed
    # over every customer (resolve is arm-independent) so the skew is a property
    # of the environment, not of the 70:30 split.
    band_tickets: list[float] = []
    pop_tickets: list[float] = []
    none_flags: list[float] = []
    for cid, d in per_customer.items():
        tk = d["ticket_inr"]
        pop_tickets.append(tk)
        got_none = bool(env.resolve(cid, "none"))
        got_nudge = bool(env.resolve(cid, "nudge"))
        none_flags.append(1.0 if got_none else 0.0)
        if got_nudge and not got_none:
            band_tickets.append(tk)
    mean_ticket_incremental_band = mean(band_tickets)
    mean_ticket_population = mean(pop_tickets)
    band_ratio = (
        mean_ticket_incremental_band / mean_ticket_population
        if mean_ticket_population else float("nan")
    )
    corr_ticket_recovery = pearson(pop_tickets, none_flags)

    count_implied_value = (
        incremental_recoveries_count * pooled_mean_recovered_ticket
    )
    value_fraction_of_count = (
        incremental_recovered_value / count_implied_value
        if count_implied_value else float("nan")
    )
    total_shortfall = count_implied_value - incremental_recovered_value

    # ---- FIX 5: seeded stratified bootstrap CI on the value-weighted figure ---
    t_recovered_values = [
        (d["ticket_inr"] if d["recovered"] else 0.0) for d in t_acted
    ]
    c_recovered_values = [
        (d["ticket_inr"] if d["recovered"] else 0.0) for d in ctrl
    ]
    boot = bootstrap_incremental_value(
        seed, t_recovered_values, c_recovered_values, t_n, action_cost_distinct
    )
    incremental_value_per_treated_customer_ci_95 = {
        "low": boot["per_treated_low"],
        "high": boot["per_treated_high"],
        "method": boot["method"],
        "n_resamples": boot["n_resamples"],
    }
    incremental_recovered_value_ci_95 = {
        "low": boot["gross_low"],
        "high": boot["gross_high"],
        "method": boot["method"],
        "n_resamples": boot["n_resamples"],
    }
    net_incremental_ev_ci_95 = {
        "low": boot["net_low"],
        "high": boot["net_high"],
        "method": boot["method"],
        "n_resamples": boot["n_resamples"],
    }
    net_lower_at_or_below_zero = boot["net_low"] <= 0.0
    count_implied_in_gross_interval = (
        boot["gross_low"] <= count_implied_value <= boot["gross_high"]
    )

    # ---- FIX 6: decompose the count-vs-value shortfall; do not overclaim ----
    implied_mean_ticket_per_incremental_recovery = (
        incremental_recovered_value / incremental_recoveries_count
        if incremental_recoveries_count else float("nan")
    )
    # what the small-ticket skew of the incremental band accounts for:
    band_explained_shortfall = incremental_recoveries_count * (
        pooled_mean_recovered_ticket - mean_ticket_incremental_band
    )
    residual_unexplained_shortfall = (
        total_shortfall - band_explained_shortfall
    )

    arms_balanced = abs(ticket_pct_diff) < 5.0
    if not arms_balanced:
        balance_clause = (
            f"The arms are NOT balanced on ticket ({ticket_pct_diff:+.1f}%: Rs "
            f"{mean_ticket_all_treatment:.0f} treatment vs Rs "
            f"{mean_ticket_all_control:.0f} control), which alone can move the "
            f"value-weighted figure."
        )
    else:
        balance_clause = (
            f"The arms are balanced on ticket ({ticket_pct_diff:+.1f}%: Rs "
            f"{mean_ticket_all_treatment:.0f} treatment vs Rs "
            f"{mean_ticket_all_control:.0f} control; assignment is a pure sha256 "
            f"on customer_id), so the gap is not arm imbalance."
        )
    if count_implied_in_gross_interval:
        power_clause = (
            f"contains the count-implied Rs {count_implied_value:,.0f}, so the "
            f"count and value estimates are NOT statistically distinguishable at "
            f"this sample size (n={c_n} control): the apparent gap is consistent "
            f"with sampling noise in a heavy-tailed, zero-inflated value "
            f"estimator. This run is underpowered on value at n={c_n} control."
        )
    else:
        power_clause = (
            f"does NOT contain the count-implied Rs {count_implied_value:,.0f}, "
            f"so the residual is beyond what within-arm resampling explains at "
            f"n={c_n} control."
        )
    value_vs_count_note = (
        f"(1) Count basis: {incremental_recoveries_count:.1f} incremental "
        f"recoveries ({t_rec} minus {c_rate:.4f} x {t_n} expected at the control "
        f"rate); at the pooled mean recovered ticket of Rs "
        f"{pooled_mean_recovered_ticket:.0f} that implies Rs "
        f"{count_implied_value:,.0f}. Value basis: Rs "
        f"{incremental_recovered_value:,.0f} "
        f"({value_fraction_of_count:.0%} of it), a gap of Rs "
        f"{total_shortfall:,.0f}. "
        f"(2) {balance_clause} "
        f"(3) The incremental band skews to smaller tickets (mean Rs "
        f"{mean_ticket_incremental_band:.0f} vs population Rs "
        f"{mean_ticket_population:.0f}, ratio {band_ratio:.2f}), and raw "
        f"self-recovery probability has ~no linear ticket correlation (Pearson "
        f"{corr_ticket_recovery:+.2f}) -- so it is lift, not baseline recovery, "
        f"that concentrates in small tickets. But that skew accounts for only Rs "
        f"{band_explained_shortfall:,.0f} of the Rs {total_shortfall:,.0f} gap "
        f"(implied mean ticket per incremental recovery is Rs "
        f"{implied_mean_ticket_per_incremental_recovery:.0f}, below the band mean "
        f"of Rs {mean_ticket_incremental_band:.0f}). "
        f"(4) That leaves Rs {residual_unexplained_shortfall:,.0f} unexplained by "
        f"ticket skew. The stratified bootstrap 95% interval on incremental "
        f"recovered value is [Rs {boot['gross_low']:,.0f}, Rs "
        f"{boot['gross_high']:,.0f}] and {power_clause} "
        f"(5) Headline: net_incremental_ev_inr = Rs {net_incremental_ev:,.0f}, "
        f"95% bootstrap [Rs {boot['net_low']:,.0f}, Rs {boot['net_high']:,.0f}]"
        + (
            " -- the lower bound is at or below zero, so on this corpus the "
            "value-weighted incremental EV is NOT distinguishable from zero at "
            "95%"
            if net_lower_at_or_below_zero else ""
        )
        + f". It is the value-weighted, counterfactual-subtracted figure, robust "
        f"to the raw-recovery fallacy; the count-basis uplift "
        f"({uplift * 100.0:.2f} pp, Wilson [{uplift_ci_block(t_rec, t_n, c_rec, c_n)['low_pp']:.2f}, "
        f"{uplift_ci_block(t_rec, t_n, c_rec, c_n)['high_pp']:.2f}] pp) is "
        f"reported beside it, not instead of it."
    )

    customer_level = {
        "n_customers": len(per_customer),
        "n_treatment_customers": t_n,
        "n_control_customers": c_n,
        "n_treatment_blocked_customers": len(t_blocked),
        "recovery_rate_treatment": t_rate,
        "recovery_rate_control": c_rate,
        "recovery_count_treatment": t_rec,
        "recovery_count_control": c_rec,
        "incremental_uplift_pp": uplift * 100.0,
        "uplift_ci_95": uplift_ci_block(t_rec, t_n, c_rec, c_n),
        "incremental_recoveries_count": incremental_recoveries_count,
        "total_recovered_value_inr": total_recovered_value,
        "total_recovered_value_treatment_inr": recovered_value_treatment,
        "total_recovered_value_control_inr": recovered_value_control,
        "counterfactual_recovered_value_treatment_inr": (
            counterfactual_value_treatment
        ),
        "incremental_recovered_value_inr": incremental_recovered_value,
        "incremental_recovered_value_ci_95": incremental_recovered_value_ci_95,
        "incremental_value_per_treated_customer_inr": incremental_value_per_treated,
        "incremental_value_per_treated_customer_ci_95": (
            incremental_value_per_treated_customer_ci_95
        ),
        "net_incremental_ev_inr": net_incremental_ev,
        "net_incremental_ev_ci_95": net_incremental_ev_ci_95,
        "count_implied_incremental_value_inr": count_implied_value,
        "implied_mean_ticket_per_incremental_recovery_inr": (
            implied_mean_ticket_per_incremental_recovery
        ),
        "band_explained_shortfall_inr": band_explained_shortfall,
        "residual_unexplained_shortfall_inr": residual_unexplained_shortfall,
        "mean_ticket_recovered_treatment_inr": mean_ticket_recovered_treatment,
        "mean_ticket_recovered_control_inr": mean_ticket_recovered_control,
        "mean_ticket_incremental_band_inr": mean_ticket_incremental_band,
        "mean_ticket_population_inr": mean_ticket_population,
        "corr_ticket_self_recovery": corr_ticket_recovery,
        "ticket_balance_check": ticket_balance_check,
        "value_vs_count_note": value_vs_count_note,
        "net_incremental_ev_note": (
            "net_incremental_ev_inr = incremental_recovered_value_inr minus "
            "event_level.total_action_cost_inr (distinct actions). Both terms "
            "are on the treated-customer basis (n_treatment_customers). This is "
            "the headline EV figure -- NOT gross_recovered_value_all_arms_inr. "
            "It carries a seeded stratified-bootstrap 95% interval "
            "(net_incremental_ev_ci_95); read that, not the point value alone."
        ),
    }

    # ---- FIX 1: demote the raw all-arms figure, do not delete it ----
    gross_recovered_value_all_arms = total_recovered_value - action_cost_events

    # ---- FIX 4: the EV gate never fires on this corpus -- record why ----
    causes_present = sorted({d["cause"] for d in per_customer.values()})
    cause_counts = Counter(d["cause"] for d in per_customer.values())
    phone_rows = sum(1 for r in rows if r.get("phone"))
    phone_coverage = phone_rows / len(rows) if rows else 0.0
    min_ticket = min(d["ticket_inr"] for d in per_customer.values())
    breakevens = {c: email_breakeven_ticket_inr(policy, c) for c in causes_present}
    worst_cause = max(breakevens, key=breakevens.get)
    worst_breakeven = breakevens[worst_cause]
    email_cost = next(
        r for r in policy["action_ladder"] if r["name"] == "email"
    )["cost_inr"]
    floor = policy["min_ev_inr"]
    be_phrase = "; ".join(
        f"{c} Rs {breakevens[c]:.2f} (n={cause_counts[c]})" for c in causes_present
    )
    n_ev_total = len(per_event)
    policy_selectivity_note = (
        f"On this corpus the policy is behaviourally identical to the "
        f"recover_everything baseline: across {n_ev_total} events there were "
        f"zero EV_BELOW_FLOOR skips, zero ROUTE_TO_HUMAN, zero guardrail blocks, "
        f"and exactly one rung fired (email). Reason: the email rung costs "
        f"Rs {email_cost:.2f} and EV = p_incremental_effective * ticket - cost "
        f"clears the Rs {floor:.2f} floor for every event in the observed ticket "
        f"distribution. The rules classifier emits only "
        f"{len(causes_present)} cause(s) here -- {be_phrase} -- where the "
        f"Rs-value is that cause's email-rung break-even ticket (EV = floor). "
        f"The smallest ticket in the corpus is Rs {min_ticket:.2f}, above every "
        f"break-even; the tightest margin is Rs {min_ticket - worst_breakeven:.2f} "
        f"for {worst_cause}. Phone coverage is {phone_coverage:.0%}, so the sms / "
        f"whatsapp / agent_call rungs (requires_channel=phone) are unreachable "
        f"and the five-rung ladder collapses to email-only -- the higher-cost "
        f"rungs where the EV floor and the human-review route actually bind "
        f"cannot be exercised by this corpus. Slice 8's live webhook DID produce "
        f"SKIP / EV_BELOW_FLOOR on a Rs 10 ticket (pay_TW67GAczusj3yl, EV "
        f"Rs 0.46 vs the Rs 2.00 floor), which is the standing evidence the gate "
        f"works."
    )

    # ---- FIX 7: this uplift is a property of the generator, not the world ----
    nev_lb = net_incremental_ev_ci_95["low"]
    nev_half_width_below_point = net_incremental_ev - nev_lb  # ~= z * SE
    # SE shrinks as 1/sqrt(n); to push the lower bound to zero we need
    # z*SE' = point, i.e. SE'/SE = point / half_width, so n' = n * (SE/SE')^2.
    se_shrink_needed = (
        nev_half_width_below_point / net_incremental_ev
        if net_incremental_ev > 0 else float("nan")
    )
    n_scale = se_shrink_needed ** 2 if math.isfinite(se_shrink_needed) else float("nan")
    n_control_needed = c_n * n_scale
    n_control_oom = _two_sig_figs(n_control_needed)
    synthetic_provenance_note = (
        f"SYNTHETIC CORPUS -- read before the numbers. Outcomes are resolved by "
        f"eval/environment.py from the latent per-customer parameters in "
        f"data/ground_truth.json (written by our own datagen.py); they are NOT "
        f"observed from a payment processor. The {uplift * 100.0:.2f} pp "
        f"count-basis uplift is therefore a property of that generator. What "
        f"this run validates is the MEASUREMENT APPARATUS -- blind arm "
        f"assignment hashed on customer_id, counterfactual subtraction of the "
        f"control rate, the attribution window (degenerate 'none' here), and "
        f"Wilson/Newcombe + seeded-bootstrap interval estimation -- it is NOT a "
        f"claim about real-world recovery performance. The one real-world "
        f"datapoint in the project is Slice 8's live Razorpay test-mode "
        f"webhook: a genuine end-to-end decision (insufficient_funds, Rs 10 "
        f"ticket, best rung sms, terminal SKIP / EV_BELOW_FLOOR). To make the "
        f"uplift a real claim you would run this same pipeline against live "
        f"webhooks with an observed recovery feed and a control arm large "
        f"enough to bound the value-weighted estimate: this run's "
        f"net_incremental_ev_ci_95 lower bound is Rs {nev_lb:,.0f} at "
        f"n_control = {c_n}, and pushing it above zero at the observed effect "
        f"size needs the sampling error to shrink by roughly "
        f"{se_shrink_needed:.1f}x, i.e. a control arm on the order of "
        f"~{n_control_oom:,.0f} customers (about {n_control_needed / c_n:.0f}x "
        f"today's {c_n}) -- a few thousand customers total at the locked 70:30 "
        f"split, order 1e3. That assumes the effect size holds and the "
        f"heavy-tailed value distribution does not worsen, so treat it as a "
        f"floor."
    )

    aggregate = {
        "customer_level": customer_level,
        "event_level": event_level,
        "gross_recovered_value_all_arms_inr": gross_recovered_value_all_arms,
        "gross_recovered_value_all_arms_note": (
            "DEMOTED (was 'net_ev_realised_inr' in the first frozen draft). "
            "This is gross recovered value across BOTH arms minus the "
            "event-level action cost. It counts self-recovery the policy did "
            "not cause and is NOT an uplift, incremental or net-EV figure -- "
            "do not quote it as one. The headline is "
            "customer_level.net_incremental_ev_inr."
        ),
        "multi_failure_customers": sum(
            1 for v in events_per_customer.values() if v > 1
        ),
    }

    result = {
        "artifact": "slice11_final_run",
        "unit_note": (
            "aggregates are split into customer_level (n_customers=1611, the "
            "arm/recovery/uplift denominator -- one normalized row per "
            "customer after Ingestor source:reference dedupe) and event_level "
            "(n_events=2000). Every field name states its unit."
        ),
        "synthetic_provenance_note": synthetic_provenance_note,
        "processing_order": PROCESSING_ORDER,
        "attribution_window_note": ATTRIBUTION_WINDOW_NOTE,
        "action_to_recovery_mapping": ACTION_TO_RECOVERY_MAPPING,
        "policy_selectivity_note": policy_selectivity_note,
        "aggregate": aggregate,
        "provenance": provenance(
            events_path, ground_truth_path, policy,
            corpus_min, corpus_max, len(per_customer), len(per_event),
        ),
        "per_event": per_event,
    }

    dump_json(result, out_path)
    write_meta(
        out_path.with_suffix(".meta.json"),
        {
            "artifact": str(out_path),
            "duration_seconds": round(time.time() - t0, 3),
            "scratch_db": str(scratch_db),
            "n_decisions": len(decision_by_pid),
        },
    )

    _print_block("STEP 1 -- FINAL RUN (locked 70/30, real pipeline)", {
        "aggregate": aggregate,
        "provenance": result["provenance"],
    })


# =========================================================================
# STEP 2 -- first 500 customers, 70:30 vs 90:10, decide_with_ladder direct
# =========================================================================
def _decide_slice(rows, policy, seed, frac, env):
    hist = {"last_contact_at": None, "prior_recoveries": []}
    per = []
    for row in rows:
        cid = row["customer_id"]
        arm = assign_arm(seed, cid, frac)
        event = build_event(row, row["occurred_at"])
        decision, _ = decide_with_ladder(event, policy, arm, hist)
        action_for_env = "nudge" if decision.terminal == Terminal.ACT else "none"
        per.append({
            "customer_id": cid,
            "payment_id": row["reference"],
            "ticket_inr": row["amount_paise"] / 100.0,
            "arm": arm,
            "terminal": decision.terminal,
            "action": decision.action,
            "skip_reason": (
                decision.skip_reason.name if decision.skip_reason else None
            ),
            "action_cost_inr": decision.action_cost_inr,
            "recovered": bool(env.resolve(cid, action_for_env)),
        })
    return per


def _split_aggregate(per, events_per_customer):
    treat = [d for d in per if d["arm"] == "treatment"]
    ctrl = [d for d in per if d["arm"] == "control"]
    t_blocked = [d for d in treat if str(d["terminal"]).startswith("BLOCKED/")]
    t_acted = [d for d in treat if not str(d["terminal"]).startswith("BLOCKED/")]
    t_n, c_n = len(t_acted), len(ctrl)
    t_rec = sum(1 for d in t_acted if d["recovered"])
    c_rec = sum(1 for d in ctrl if d["recovered"])
    t_rate = t_rec / t_n if t_n else float("nan")
    c_rate = c_rec / c_n if c_n else float("nan")

    actions_events: Counter = Counter()
    actions_distinct: Counter = Counter()
    skip_counts: Counter = Counter()
    route_to_human = 0
    suppressed = 0
    cost_events = 0.0
    cost_distinct = 0.0
    for d in per:
        n_ev = events_per_customer[d["customer_id"]]
        term = d["terminal"]
        if term == Terminal.ACT:
            actions_events[d["action"]] += n_ev
            actions_distinct[d["action"]] += 1
            cost_events += (d["action_cost_inr"] or 0.0) * n_ev
            cost_distinct += (d["action_cost_inr"] or 0.0)
        elif term == Terminal.ROUTE_TO_HUMAN:
            route_to_human += n_ev
        elif str(term).startswith("BLOCKED/"):
            suppressed += n_ev
        else:
            skip_counts[d["skip_reason"] or "UNKNOWN"] += n_ev

    total_recovered_value = sum(
        d["ticket_inr"] for d in per if d["recovered"]
    )
    recovered_value_treatment = sum(
        d["ticket_inr"] for d in t_acted if d["recovered"]
    )
    recovered_value_control = sum(
        d["ticket_inr"] for d in ctrl if d["recovered"]
    )
    counterfactual_value_treatment = (
        (recovered_value_control / c_n) * t_n if c_n else float("nan")
    )
    incremental_recovered_value = (
        recovered_value_treatment - counterfactual_value_treatment
    )
    net_incremental_ev = incremental_recovered_value - cost_distinct
    gross_recovered_value_all_arms = total_recovered_value - cost_events

    return {
        "customer_level": {
            "n_customers": len(per),
            "n_treatment_customers": t_n,
            "n_control_customers": c_n,
            "n_treatment_blocked_customers": len(t_blocked),
            "recovery_rate_treatment": t_rate,
            "recovery_rate_control": c_rate,
            "recovery_count_treatment": t_rec,
            "recovery_count_control": c_rec,
            "control_self_recovery_rate": c_rate,
            "incremental_uplift_pp": (t_rate - c_rate) * 100.0,
            "uplift_ci_95": uplift_ci_block(t_rec, t_n, c_rec, c_n),
            "incremental_recoveries_count": t_rec - (c_rate * t_n),
            "total_recovered_value_inr": total_recovered_value,
            "total_recovered_value_treatment_inr": recovered_value_treatment,
            "total_recovered_value_control_inr": recovered_value_control,
            "counterfactual_recovered_value_treatment_inr": (
                counterfactual_value_treatment
            ),
            "incremental_recovered_value_inr": incremental_recovered_value,
            "net_incremental_ev_inr": net_incremental_ev,
        },
        "event_level": {
            "action_counts_by_rung": dict(sorted(actions_events.items())),
            "distinct_actions_by_rung": dict(sorted(actions_distinct.items())),
            "skip_counts_by_reason": dict(sorted(skip_counts.items())),
            "route_to_human_count": route_to_human,
            "suppressed_by_guardrail_count": suppressed,
            "total_action_cost_inr": cost_distinct,
            "total_action_cost_events_inr": cost_events,
        },
        "gross_recovered_value_all_arms_inr": gross_recovered_value_all_arms,
        "gross_recovered_value_all_arms_note": (
            "DEMOTED (was 'net_ev_realised_inr'). Gross recovered value across "
            "BOTH arms minus event-level action cost; counts self-recovery the "
            "policy did not cause. NOT an uplift figure. Headline is "
            "customer_level.net_incremental_ev_inr."
        ),
        "_raw": {"t_rec": t_rec, "t_n": t_n, "c_rec": c_rec, "c_n": c_n},
    }


def run_split(args) -> None:
    t0 = time.time()
    events_path = Path(args.events)
    ground_truth_path = Path(args.ground_truth)
    policy = load_policy(args.policy)
    seed = policy["experiment_seed"]

    splits = []
    for tok in args.splits.split(","):
        a, b = tok.split(":")
        splits.append((int(a), int(b)))

    rows, _ = load_population(str(events_path))
    rows = sorted(rows, key=lambda r: r["customer_id"])
    limit = args.limit_customers
    slice_rows = rows[:limit]
    slice_cids = {r["customer_id"] for r in slice_rows}
    slice_rule = (
        f"the first {limit} customer_ids in ascending lexical order of the "
        f"normalized rows returned by eval.measurement.load_population "
        f"(one row per customer after Ingestor source:reference dedupe)"
    )

    doc = json.loads(events_path.read_text(encoding="utf-8"))
    events = doc["events"]
    require_timestamps(events)

    def cid_of(ev):
        return ev["payload"]["payload"]["payment"]["entity"]["notes"]["customer_id"]

    events_in_slice = [e for e in events if cid_of(e) in slice_cids]
    events_per_customer = Counter(cid_of(e) for e in events_in_slice)
    ts_all = [e["created_at"] for e in events]

    env = Environment(str(ground_truth_path))
    if env.run_seed != seed:
        raise SystemExit("STOP: ground_truth seed != policy experiment_seed")

    per_split = {}
    for a, b in splits:
        frac = a / (a + b)
        label = f"{a}:{b}"
        per = _decide_slice(slice_rows, policy, seed, frac, env)
        agg = _split_aggregate(per, events_per_customer)

        # cross-check against the locked measurement harness, which assigns
        # arms the same way and accepts a treatment_fraction override.
        hist = {"last_contact_at": None, "prior_recoveries": []}

        def engine_policy(row):
            ev = build_event(row, row["occurred_at"])
            d, _ = decide_with_ladder(ev, policy, "treatment", hist)
            return "nudge" if d.terminal == Terminal.ACT else "none"

        mres = run_policy(
            slice_rows, str(ground_truth_path), engine_policy,
            policy_name=label, run_seed=seed, treatment_fraction=frac,
        )
        raw = agg.pop("_raw")
        if (raw["t_rec"], raw["t_n"], raw["c_rec"], raw["c_n"]) != (
            mres.treatment.n_recovered, mres.treatment.n,
            mres.control.n_recovered, mres.control.n,
        ):
            raise SystemExit(
                f"STOP: split {label} disagrees with eval.measurement.run_policy "
                f"-- script {raw} vs harness "
                f"t=({mres.treatment.n_recovered}/{mres.treatment.n}) "
                f"c=({mres.control.n_recovered}/{mres.control.n})"
            )
        per_split[label] = agg

    # delta block: 90:10 minus 70:30 (order of `splits` as given)
    labels = [f"{a}:{b}" for a, b in splits]
    base, alt = labels[0], labels[-1]
    b_cl = per_split[base]["customer_level"]
    a_cl = per_split[alt]["customer_level"]
    delta = {
        "compared": f"{alt} minus {base}",
        "d_n_treatment_customers": (
            a_cl["n_treatment_customers"] - b_cl["n_treatment_customers"]
        ),
        "d_n_control_customers": (
            a_cl["n_control_customers"] - b_cl["n_control_customers"]
        ),
        "d_control_self_recovery_rate_pp": (
            a_cl["control_self_recovery_rate"] - b_cl["control_self_recovery_rate"]
        ) * 100.0,
        "d_incremental_uplift_pp": (
            a_cl["incremental_uplift_pp"] - b_cl["incremental_uplift_pp"]
        ),
        "d_uplift_ci_width_pp": (
            a_cl["uplift_ci_95"]["width_pp"] - b_cl["uplift_ci_95"]["width_pp"]
        ),
    }

    reading = (
        f"The locked decision is {base}. This artifact does NOT change it. "
        f"Moving to {alt} buys more treated customers "
        f"({b_cl['n_treatment_customers']} -> {a_cl['n_treatment_customers']}) "
        f"but shrinks the control arm "
        f"({b_cl['n_control_customers']} -> {a_cl['n_control_customers']}), so "
        f"the control self-recovery estimate gets noisier and the 95% interval "
        f"on incremental uplift widens from "
        f"{b_cl['uplift_ci_95']['width_pp']:.2f} pp to "
        f"{a_cl['uplift_ci_95']['width_pp']:.2f} pp. 90/10 is a "
        f"measurement-cost trade, not a lift improvement; {base} is kept."
    )

    corpus_min = iso_utc(min(ts_all))
    corpus_max = iso_utc(max(ts_all))
    result = {
        "artifact": "slice11_split_comparison",
        "scope": "500 customers",
        "slice_rule": slice_rule,
        "n_customers_in_slice": len(slice_rows),
        "n_events_in_slice": len(events_in_slice),
        "locked_split": base,
        "mechanism_note": (
            "STEP 2 runs app.decision.engine.decide_with_ladder directly with "
            "arm assignment at the swept treatment_fraction -- the only "
            "existing seam that accepts the override without editing "
            "config/decision_policy.json or eval.measurement. Each split is "
            "cross-checked against eval.measurement.run_policy (same arm hash, "
            "same resolver). Guardrails are not re-run here; STEP 1 (the full "
            "pipeline, guardrail walk included) shows every ACT is the email "
            "rung and all seven guardrails pass, so the walk is inert and "
            "n_treatment_blocked_customers is 0 there."
        ),
        "processing_order": (
            "customers taken in ascending customer_id order; arm assignment and "
            "outcome resolution are both order-invariant pure hashes"
        ),
        "attribution_window_note": ATTRIBUTION_WINDOW_NOTE,
        "action_to_recovery_mapping": ACTION_TO_RECOVERY_MAPPING,
        "reading": reading,
        "splits": per_split,
        "delta": delta,
        "provenance": provenance(
            events_path, ground_truth_path, policy,
            corpus_min, corpus_max, len(slice_rows), len(events_in_slice),
        ),
    }

    dump_json(result, Path(args.out))
    write_meta(
        Path(args.out).with_suffix(".meta.json"),
        {
            "artifact": args.out,
            "duration_seconds": round(time.time() - t0, 3),
            "splits": args.splits,
        },
    )

    lines = {}
    for label, agg in per_split.items():
        cl = agg["customer_level"]
        lines[label] = {
            "n_control_customers": cl["n_control_customers"],
            "n_treatment_customers": cl["n_treatment_customers"],
            "control_self_recovery_rate": cl["control_self_recovery_rate"],
            "incremental_uplift_pp": cl["incremental_uplift_pp"],
            "uplift_ci_95_width_pp": cl["uplift_ci_95"]["width_pp"],
        }
    _print_block("STEP 2 -- 500-CUSTOMER SPLIT SWEEP", {
        "per_split": lines,
        "delta": delta,
        "reading": reading,
    })


def _print_block(title: str, payload: dict) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps(round_floats(payload), sort_keys=True, indent=2))
    print("=" * 72)


# --------------------------------------------------------------------- cli
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Slice 11 full run and freeze")
    sub = ap.add_subparsers(dest="mode", required=True)

    common = dict()
    f = sub.add_parser("final", help="STEP 1: full 2,000-event run, locked 70/30")
    f.add_argument("--events", default=str(DEFAULT_EVENTS))
    f.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    f.add_argument("--policy", default=str(DEFAULT_POLICY))
    f.add_argument("--out", default=str(REPO / "results" / "final_run.json"))
    f.set_defaults(func=run_final)

    s = sub.add_parser("split", help="STEP 2: first-500-customers split sweep")
    s.add_argument("--events", default=str(DEFAULT_EVENTS))
    s.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    s.add_argument("--policy", default=str(DEFAULT_POLICY))
    s.add_argument("--limit-customers", type=int, default=500)
    s.add_argument("--splits", default="70:30,90:10")
    s.add_argument("--out", default=str(REPO / "results" / "split_comparison_500.json"))
    s.set_defaults(func=run_split)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
