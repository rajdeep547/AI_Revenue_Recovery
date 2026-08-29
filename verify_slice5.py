"""Slice 5 independent auditor. Not a test file -- deliberately outside pytest.

Re-derives every number from source rather than trusting eval/measurement.py's
own reporting. May read ground_truth.json (this is not app/ code).

Exit 0 = all green. Exit 1 = something is wrong. Exit 2 = could not bind to
your API; read the SKIP reasons, fix the shims at the top, re-run.
SKIP is never a pass -- an auditor that can't check something says so.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SEED = 20260826
DATA = ROOT / "data"
EVENTS = DATA / "events.json"
GROUND_TRUTH = DATA / "ground_truth.json"

_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    tag = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "INFO": "    "}[status]
    print(f"  [{tag}] {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")


def section(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def guard(name: str):
    """Decorator: an exploding check is a FAIL, not a crashed run."""
    def deco(fn):
        def wrapped(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                record("FAIL", name, f"raised {type(exc).__name__}: {exc}")
                return None
        return wrapped
    return deco


# --------------------------------------------------------------------------
# Shims. Adjust ONLY these if your signatures differ.
# --------------------------------------------------------------------------

def _call_flex(fn, kwargs: dict, positional_order: list[str]):
    """Call fn with whichever of kwargs it actually accepts."""
    sig = inspect.signature(fn)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    if accepted:
        return fn(**accepted)
    args = [kwargs[k] for k in positional_order if k in kwargs]
    return fn(*args)


def get_population(m):
    result = _call_flex(m.load_population, {"seed": SEED, "run_seed": SEED,
                                          "events_path": EVENTS, "path": EVENTS},
                      ["events_path"])
    # Shim fix: load_population returns (rows, stats), not bare rows.
    if isinstance(result, tuple) and len(result) == 2:
        return result[0]
    return result


def run_policy(m, policy, population=None, seed=SEED, ground_truth=None):
    # Shim fix: run_policy's signature is (rows, ground_truth, policy, ...) --
    # ground_truth is required and was missing from this shim entirely.
    kwargs = {"policy": policy, "population": population, "rows": population,
              "seed": seed, "run_seed": seed, "ground_truth": ground_truth}
    return _call_flex(m.run_policy, kwargs, ["rows", "ground_truth", "policy", "seed"])


def customer_id_of(row) -> str:
    for attr in ("customer_id", "customerId"):
        if hasattr(row, attr):
            return getattr(row, attr)
        if isinstance(row, dict) and attr in row:
            return row[attr]
    raise KeyError(f"no customer_id on row of type {type(row).__name__}")


def unpack(result):
    """Pull (uplift, lo, hi, n_t, n_c, rate_t, rate_c) out of whatever came back."""
    def g(*names, default=None):
        for n in names:
            if isinstance(result, dict) and n in result:
                return result[n]
            if hasattr(result, n):
                return getattr(result, n)
        return default

    ci = g("ci", "confidence_interval", "ci_95")
    lo, hi = (ci if ci is not None else (g("ci_low", "ci_lower"), g("ci_high", "ci_upper")))
    t, c = g("treatment"), g("control")

    def arm(a, *names):
        if a is None:
            return g(*names)
        for n in ("n", "count", "size"):
            if n in names[0] and hasattr(a, n):
                return getattr(a, n)
        return None

    n_t = g("n_treatment", "treatment_n") or (getattr(t, "n", None) if t else None)
    n_c = g("n_control", "control_n") or (getattr(c, "n", None) if c else None)
    r_t = g("treatment_rate", "rate_treatment") or (getattr(t, "rate", None) if t else None)
    r_c = g("control_rate", "rate_control") or (getattr(c, "rate", None) if c else None)
    return g("uplift"), lo, hi, n_t, n_c, r_t, r_c


def wald(p_t, n_t, p_c, n_c):
    se = math.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)
    d = p_t - p_c
    return d, d - 1.96 * se, d + 1.96 * se


# --------------------------------------------------------------------------

print("=" * 70)
print(f"SLICE 5 AUDIT   seed={SEED}   root={ROOT}")
print("=" * 70)

section("0. Imports and data files")

for p in (EVENTS, GROUND_TRUTH):
    record("PASS" if p.exists() else "FAIL", f"{p.name} exists",
           "" if p.exists() else f"missing: {p}")

try:
    import eval.measurement as M
    record("PASS", "import eval.measurement")
except Exception as exc:  # noqa: BLE001
    record("FAIL", "import eval.measurement", str(exc))
    print("\nCannot continue without the measurement module.")
    sys.exit(2)

gt_raw = json.loads(GROUND_TRUTH.read_text())
# Shim fix: our ground_truth.json is {"meta": ..., "customers": {cid: record}} --
# not a flat cid-> record map or a bare list of records.
customers_raw = gt_raw.get("customers", gt_raw) if isinstance(gt_raw, dict) else gt_raw
if isinstance(customers_raw, dict):
    gt = customers_raw
else:
    gt = {r["customer_id"]: r for r in customers_raw}
record("INFO", f"ground_truth: {len(gt)} customers")


section("1. app/ purity")

@guard("app/ never references ground_truth or eval.")
def check_purity():
    pat = re.compile(r"ground_truth|eval\.")
    hits = []
    for py in (ROOT / "app").rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{py.relative_to(ROOT)}:{i}: {line.strip()}")
    record("FAIL" if hits else "PASS",
           "app/ never references ground_truth or eval.",
           "\n".join(hits) if hits else "grep clean")
check_purity()


section("2. Population invariant  (item 1 -- load-bearing)")

population = None

@guard("load_population() returns one row per customer")
def check_population():
    global population
    population = get_population(M)
    ids = [customer_id_of(r) for r in population]
    dupes = [c for c, n in Counter(ids).items() if n > 1]
    record("INFO", f"population rows={len(ids)}  unique customers={len(set(ids))}")
    if dupes:
        record("FAIL", "load_population() returns one row per customer",
               f"{len(dupes)} customers appear more than once, e.g. {dupes[:5]}\n"
               "Assignment is customer-level but the denominator has gone "
               "event-level. The Wald CI is now computed over non-independent "
               "observations and is too narrow.")
    else:
        record("PASS", "load_population() returns one row per customer")
check_population()

@guard("invariant is enforced in code, not merely satisfied")
def check_invariant_enforced():
    src = inspect.getsource(M.load_population)
    enforced = ("set(" in src and ("raise" in src or "assert" in src)) or "Duplicate" in src
    record("PASS" if enforced else "FAIL",
           "invariant is enforced in code, not merely satisfied",
           "" if enforced else
           "load_population() has no explicit uniqueness check. It passes today "
           "only because datagen reuses one payment id per customer and dedupe "
           "collapses the rest. Make it a contract that raises.")
check_invariant_enforced()


section("3. Assignment  (items 2 and 5)")

@guard("assign_arm is deterministic")
def check_determinism():
    ids = [customer_id_of(r) for r in population][:200]
    a = [M.assign_arm(SEED, c) for c in ids]
    b = [M.assign_arm(SEED, c) for c in reversed(ids)][::-1]
    record("PASS" if a == b else "FAIL", "assign_arm is deterministic")
check_determinism()

@guard("assignment uses no runtime randomness")
def check_no_random():
    src = inspect.getsource(M)
    bad = [f"line {i}: {l.strip()}" for i, l in enumerate(src.splitlines(), 1)
           if re.search(r"\brandom\.(random|choice|shuffle|randint)|\bnp\.random", l)
           and "shuffle" not in l.lower().split("#")[-1]]
    record("FAIL" if bad else "PASS", "assignment uses no runtime randomness",
           "\n".join(bad) if bad else "no random() calls in measurement module")
check_no_random()

@guard("salt is the approved 'assign:' deviation")
def check_salt():
    src = inspect.getsource(M)
    has = "assign:" in src
    documented = bool(re.search(r"assign:.{0,400}?(environment|resolution|bias|correlat)",
                                src, re.S | re.I))
    if has and documented:
        record("PASS", "salt is the approved 'assign:' deviation",
               "rationale present at the assignment site")
    elif has:
        record("FAIL", "salt is the approved 'assign:' deviation",
               "'assign:' prefix found but no rationale near it. State in one "
               "line why it differs from environment.py's draw.")
    else:
        record("FAIL", "salt is the approved 'assign:' deviation",
               "no 'assign:' prefix -- assignment may share a hash with "
               "environment.py, correlating arm with self-recovery propensity.")
check_salt()

@guard("same customer, two payment ids -> same arm (pipeline level)")
def check_same_customer_pipeline():
    try:
        from app.ingest import Ingestor
        import app.ingest as ing_mod
    except Exception as exc:  # noqa: BLE001
        record("SKIP", "same customer, two payment ids -> same arm (pipeline level)",
               f"could not import app.ingest ({exc}). Break (f) unverified at "
               "pipeline level -- function-level determinism alone only tests sha256.")
        return

    cid = "cust_audit_dup"
    def body(pay_id):
        # Shim fix: created_at lives at the TOP level of the payload the
        # adapter expects, not inside payload.payload -- it was missing
        # entirely, so the adapter used to raise missing_required_field.
        return {"event": "payment.failed", "created_at": 1735689600,
                "payload": {"payment": {"entity": {
            "id": pay_id, "amount": 34900, "method": "upi", "status": "failed",
            "error_reason": "bank_downtime", "error_code": "BAD",
            "error_description": "x",
            "notes": {"customer_id": cid, "email": f"{cid}@example.test"}}}}}

    # Shim fix: the adapter is registered in ADAPTERS["card_failure"] (or
    # reached via Ingestor.ingest("card_failure", ...)) -- there is no
    # module-level `card_failure` attribute.
    ingestor = Ingestor(":memory:")
    rows = []
    for pid in ("pay_audit_a", "pay_audit_b"):
        result = ingestor.ingest("card_failure", body(pid))
        if result.row is None:
            record("FAIL", "same customer, two payment ids -> same arm (pipeline level)",
                   f"ingest was rejected: {result.reason_code}")
            return
        rows.append(result.row)
    ingestor.close()
    arms = {M.assign_arm(SEED, customer_id_of(r)) for r in rows}
    record("PASS" if len(arms) == 1 else "FAIL",
           "same customer, two payment ids -> same arm (pipeline level)",
           "" if len(arms) == 1 else f"landed in {arms} -- both arms contaminated")
check_same_customer_pipeline()


section("4. Split ratio  (item 4a)")

@guard("split is 70/30 within 3 sigma")
def check_split():
    arms = Counter(M.assign_arm(SEED, customer_id_of(r)) for r in population)
    n = sum(arms.values())
    t = arms.get("treatment", 0)
    p = t / n
    se = math.sqrt(0.7 * 0.3 / n)
    tol = 3 * se
    ok = abs(p - 0.70) <= tol
    record("PASS" if ok else "FAIL", "split is 70/30 within 3 sigma",
           f"treatment={t} control={arms.get('control', 0)} n={n}\n"
           f"observed p={p:.4f}  target 0.7000  3-sigma tolerance +/-{tol:.4f}\n"
           f"(SE = sqrt(0.7*0.3/{n}) = {se:.5f})")
check_split()


section("5. Policy results  (items 3 and 4b)")

policies = {}
for attr, label in (("do_nothing_policy", "do_nothing"),
                    ("recover_everything_policy", "recover_everything"),
                    ("targeted_card_policy", "targeted_card")):
    fn = getattr(M, attr, None)
    if fn:
        policies[label] = fn

results = {}
for label, fn in policies.items():
    try:
        results[label] = unpack(run_policy(M, fn, population, SEED, ground_truth=gt_raw))
        u, lo, hi, n_t, n_c, r_t, r_c = results[label]
        record("INFO", f"{label:<20} uplift {u:+.4f}  CI [{lo:+.4f}, {hi:+.4f}]  "
                       f"n_t={n_t} rate={r_t:.4f}  n_c={n_c} rate={r_c:.4f}")
    except Exception as exc:  # noqa: BLE001
        record("FAIL", f"run {label}", f"{type(exc).__name__}: {exc}")

@guard("CI arithmetic is independently reproducible")
def check_ci_math():
    for label, (u, lo, hi, n_t, n_c, r_t, r_c) in results.items():
        d, l2, h2 = wald(r_t, n_t, r_c, n_c)
        if max(abs(lo - l2), abs(hi - h2), abs(u - d)) > 1e-6:
            record("FAIL", "CI arithmetic is independently reproducible",
                   f"{label}: reported [{lo:+.4f},{hi:+.4f}] vs recomputed [{l2:+.4f},{h2:+.4f}]")
            return
    record("PASS", "CI arithmetic is independently reproducible",
           "recomputed Wald matches reported CI for every policy")
check_ci_math()

@guard("PASS GATE: do_nothing lands on zero, CI straddles it")
def check_gate():
    u, lo, hi, *_ = results["do_nothing"]
    straddles = lo < 0 < hi
    small = abs(u) < 0.01
    record("PASS" if (straddles and small) else "FAIL",
           "PASS GATE: do_nothing lands on zero, CI straddles it",
           f"uplift {u:+.4f}, CI [{lo:+.4f}, {hi:+.4f}]" +
           ("" if straddles and small else
            "\nSTOP. A measurement layer that cannot detect 'we did nothing' "
            "cannot be trusted for anything. Do not adjust the test."))
check_gate()

@guard("recover_everything CI brackets TREATMENT-ARM true mean lift")
def check_true_effect():
    if not gt:
        record("SKIP", "recover_everything CI brackets TREATMENT-ARM true mean lift",
               "ground_truth.json did not parse into customer records")
        return
    lift_key = next((k for k in ("p_pay_if_nudged",) if k in next(iter(gt.values()))), None)
    base_key = next((k for k in ("p_would_pay_anyway",) if k in next(iter(gt.values()))), None)
    if not (lift_key and base_key):
        record("SKIP", "recover_everything CI brackets TREATMENT-ARM true mean lift",
               f"expected p_pay_if_nudged / p_would_pay_anyway, saw {list(next(iter(gt.values())))}")
        return

    ids = [customer_id_of(r) for r in population]
    treat = [c for c in ids if M.assign_arm(SEED, c) == "treatment" and c in gt]
    pop_lift = sum(gt[c][lift_key] - gt[c][base_key] for c in ids if c in gt) / max(
        1, len([c for c in ids if c in gt]))
    arm_lift = sum(gt[c][lift_key] - gt[c][base_key] for c in treat) / max(1, len(treat))

    u, lo, hi, *_ = results["recover_everything"]
    brackets = lo <= arm_lift <= hi
    excludes_zero = lo > 0
    record("PASS" if (brackets and excludes_zero) else "FAIL",
           "recover_everything CI brackets TREATMENT-ARM true mean lift",
           f"population mean lift  {pop_lift:.4f}  (n={len([c for c in ids if c in gt])})\n"
           f"treatment mean lift   {arm_lift:.4f}  (n={len(treat)})   <-- the correct target\n"
           f"drift                 {arm_lift - pop_lift:+.4f}\n"
           f"CI [{lo:+.4f}, {hi:+.4f}]  brackets={brackets}  excludes_zero={excludes_zero}")
check_true_effect()


section("6. Order independence")

@guard("shuffled event order -> identical uplift")
def check_order():
    shuffled = list(population)
    random.Random(1).shuffle(shuffled)
    u0 = results["recover_everything"][0]
    u1 = unpack(run_policy(M, policies["recover_everything"], shuffled, SEED, ground_truth=gt_raw))[0]
    record("PASS" if abs(u0 - u1) < 1e-12 else "FAIL",
           "shuffled event order -> identical uplift",
           f"{u0:.12f} vs {u1:.12f}")
check_order()


section("7. Restart independence")

@guard("fresh process -> identical arms")
def check_restart():
    # Shim fix: load_population returns (rows, stats), not bare rows -- the
    # snippet used to iterate the tuple itself (2 items: a list and a dict).
    snippet = (
        "import sys, json, hashlib;"
        f"sys.path.insert(0, r'{ROOT}');"
        "import eval.measurement as M;"
        f"result = M.load_population(r'{EVENTS}');"
        "P = result[0] if isinstance(result, tuple) else result;"
        "ids = [getattr(r, 'customer_id', None) or r['customer_id'] for r in P];"
        f"print(hashlib.sha256(''.join(M.assign_arm({SEED}, c) for c in sorted(ids))"
        ".encode()).hexdigest())"
    )
    ids = sorted(customer_id_of(r) for r in population)
    here = hashlib.sha256("".join(M.assign_arm(SEED, c) for c in ids).encode()).hexdigest()
    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                          text=True, cwd=ROOT)
    if proc.returncode != 0:
        record("SKIP", "fresh process -> identical arms",
               f"subprocess failed:\n{proc.stderr.strip()[:400]}")
        return
    there = proc.stdout.strip().splitlines()[-1]
    record("PASS" if here == there else "FAIL", "fresh process -> identical arms",
           f"in-process {here[:16]}  subprocess {there[:16]}")
check_restart()


section("8. Exclusions are counted, not dropped  (item 4b)")

@guard("unresolvable events are excluded AND counted")
def check_exclusions():
    src = inspect.getsource(M)
    has_path = bool(re.search(r"exclud|rejected|unresolv", src, re.I))
    tested = False
    tf = ROOT / "tests" / "test_measurement.py"
    if tf.exists():
        t = tf.read_text(encoding="utf-8", errors="replace")
        tested = bool(re.search(r"no_contact|contact_channel|unresolv|exclud", t, re.I))
    if has_path and tested:
        record("PASS", "unresolvable events are excluded AND counted")
    else:
        record("FAIL", "unresolvable events are excluded AND counted",
               f"exclusion path in measurement.py: {has_path}\n"
               f"exercised by a test fixture: {tested}\n"
               "rejected==0 in your report means this path has never fired. Add a "
               "contactless event; assert it is excluded, counted by reason, and "
               "absent from both arms' denominators.")
check_exclusions()


section("9. Test suite")

@guard("pytest suite is green")
def check_pytest():
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no"],
                          capture_output=True, text=True, cwd=ROOT)
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    line = next((l for l in reversed(tail) if "passed" in l or "failed" in l or "error" in l), "")
    m = re.search(r"(\d+) passed", line)
    count = int(m.group(1)) if m else None
    ok = proc.returncode == 0
    record("PASS" if ok else "FAIL", "pytest suite is green",
           f"{line}\ntest count: {count if count is not None else 'unparsed'} "
           f"(was 110 before hardening; expect >= 113)")
check_pytest()


# --------------------------------------------------------------------------
print("\n" + "=" * 70)
counts = Counter(s for s, _, _ in _results if s != "INFO")
print(f"SUMMARY   pass={counts['PASS']}  fail={counts['FAIL']}  skip={counts['SKIP']}")
for s, n, _ in _results:
    if s == "FAIL":
        print(f"  FAIL  {n}")
    elif s == "SKIP":
        print(f"  SKIP  {n}   (unverified -- not a pass)")
print("=" * 70)

if counts["FAIL"]:
    print("\nSlice 5 is NOT clear. Fix the failures above before Slice 6.")
    sys.exit(1)
if counts["SKIP"]:
    print("\nNo failures, but some checks could not run. Fix the shims and re-run.")
    sys.exit(2)
print("\nSlice 5 audit clear.")
sys.exit(0)