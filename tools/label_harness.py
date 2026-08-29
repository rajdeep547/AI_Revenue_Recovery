"""Slice 6 rules-diagnosis harness -- blind hand-labelling first, rules second.

datagen.py wrote every ``error_code`` / ``error_description`` *from* the root
cause, so any rule scored straight against ground_truth.json is just reading
back the generator's own encoding: it looks near-perfect and proves nothing.
The only independent signal is a set of hand labels made blind from the
payload alone. So labels come FIRST, before the error-code map exists.

Subcommands
-----------
schema
    Dump the flattened event-payload schema (dotted paths, coverage, sample
    values) plus id-field coverage. Read this to write rules/error_code_map.json.

sample --n 100 --min-per-class 15 [--event-id P --truth-id P --cause-field P]
       [--include-successes] [--dedupe-by FIELD]
    Drop non-failure events (payment.authorized rows carry no error_code and
    are not diagnosis targets; --include-successes keeps them), collapse retry
    duplicates to the earliest row per customer (--dedupe-by, default = the
    event-side customer-id field; 'none' disables), join what is left to
    ground truth on an auto-detected id, sample STRATIFIED by true root cause
    with a per-class floor (seed 20260829), and write labels/blind_sample.json
    (root-cause field stripped) plus the held-out answer key
    labels/_truth_manifest.json.

label
    Walk labels/blind_sample.json in file order, skipping already-labelled
    rows. Show each row's diagnostic fields and read one keystroke
    (1-6 root cause, u unknown + required note, b back one row, q save+quit).
    The file is rewritten after every keystroke, so an interrupted run resumes
    at the first unlabelled row and loses nothing. Never reads _truth_manifest
    and never suggests a label -- the human makes every call.

score [--signal-paths PATH ...]
    Load the blind sample, the manifest, and a classifier built from
    rules/error_code_map.json, then print three confusion matrices:
    A human-vs-truth (payload ceiling), B rules-vs-human (the real test),
    C rules-vs-truth (inflated, contrast only).
    --signal-paths restricts what the rules may read (default: error_code +
    method); error_description / error_reason are rejected at load time and
    the allowed paths are printed above matrix B.

Windows / venv-without-activation invocation::

    .\\.venv\\Scripts\\python.exe tools\\label_harness.py schema
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import random
import sys

# --- paths (repo root is the parent of tools/) ------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_PATH = os.path.join(ROOT, "data", "events.json")
TRUTH_PATH = os.path.join(ROOT, "data", "ground_truth.json")
LABELS_DIR = os.path.join(ROOT, "labels")
BLIND_PATH = os.path.join(LABELS_DIR, "blind_sample.json")
MANIFEST_PATH = os.path.join(LABELS_DIR, "_truth_manifest.json")
RULES_DIR = os.path.join(ROOT, "rules")
EMAP_PATH = os.path.join(RULES_DIR, "error_code_map.json")

SEED = 20260829

# Exactly the id fields the `schema` printout must report on.
SCHEMA_ID_CANDIDATES = [
    "id",
    "event_id",
    "payment_id",
    "razorpay_payment_id",
    "payload.payment.entity.id",
    "entity.id",
]

# Broader lists used only for join auto-detection (schema is not affected).
EVENT_JOIN_ID_CANDIDATES = [
    "id",
    "event_id",
    "payment_id",
    "razorpay_payment_id",
    "entity.id",
    "payload.payment.entity.id",
    "payload.payload.payment.entity.id",
    "payload.payment.entity.notes.customer_id",
    "payload.payload.payment.entity.notes.customer_id",
]
TRUTH_ID_CANDIDATES = [
    "customer_id",
    "payment_id",
    "id",
    "event_id",
    "razorpay_payment_id",
    "_key",
]
CAUSE_FIELD_CANDIDATES = [
    "error_reason",
    "root_cause",
    "true_cause",
    "cause",
    "reason",
    "label",
]
# Per-row identity: unique, 100%-covered, never the (many:1) join key.
RECORD_ID_CANDIDATES = ["event_id", "id"]
# Event-side customer id: the default group key for --dedupe-by.
CUSTOMER_ID_CANDIDATES = [
    "customer_id",
    "notes.customer_id",
    "payload.payment.entity.notes.customer_id",
    "payload.payload.payment.entity.notes.customer_id",
    "payload.payment.entity.customer_id",
    "payload.payload.payment.entity.customer_id",
]
# Event timestamp: used to keep the *earliest* row per dedupe group.
CREATED_AT_CANDIDATES = [
    "created_at", "createdAt", "created", "timestamp", "ts", "time", "event_time",
]

_WRAPPER_KEYS = ("events", "records", "rows", "data", "items")
_TRUTH_WRAPPER_KEYS = ("customers", "records", "rows", "data", "items", "ground_truth", "truth")


# --- generic helpers -------------------------------------------------------
def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_events(path=None):
    """Return the list of event records, tolerating a bare list or a dict
    wrapping it under events/records/rows/data/items."""
    data = load_json(path or EVENTS_PATH)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _WRAPPER_KEYS:
            if isinstance(data.get(key), list):
                return data[key]
    raise SystemExit(f"no event list found inside {path or EVENTS_PATH}")


def load_truth_rows(path=None):
    """Return ground-truth rows as a list of dicts. Accepts a bare list, a
    wrapped list, an id->row dict, or a wrapped id->row dict. When rows come
    from a mapping, the mapping key is preserved on each row as ``_key``."""
    data = load_json(path or TRUTH_PATH)
    container = data
    if isinstance(data, dict):
        for key in _TRUTH_WRAPPER_KEYS:
            if key in data and isinstance(data[key], (list, dict)):
                container = data[key]
                break
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        rows = []
        for key, value in container.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("_key", key)
                rows.append(row)
        return rows
    raise SystemExit(f"no truth rows found inside {path or TRUTH_PATH}")


def resolve(obj, dotted):
    """Follow a dotted path through nested dicts. Returns None if any segment
    is missing or lands on a non-dict."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def coverage(records, path):
    if not records:
        return 0.0
    return sum(1 for r in records if resolve(r, path) is not None) / len(records)


def sample_digest(ids):
    """16-char sha256 over the comma-joined ids, in the order given (which is
    the order the rows appear in blind_sample.json)."""
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16]


def _short(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= 40 else text[:40]


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --- schema --------------------------------------------------------------
def _flatten(obj, prefix, out):
    """Append (dotted_path, scalar_value) pairs. List-valued keys render as
    ``parent[]`` and each element is flattened under that same path."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else key
            _flatten(value, child, out)
    elif isinstance(obj, list):
        child = f"{prefix}[]"
        if not obj:
            out.append((child, None))
        for item in obj:
            if isinstance(item, (dict, list)):
                _flatten(item, child, out)
            else:
                out.append((child, item))
    else:
        out.append((prefix, obj))


# --- failure filter ----------------------------------------------------
# Only failure events are diagnosis targets. A payment.authorized row carries
# no error_code/description; a blind reader can only mark it "unknown", which
# would depress matrix A's per-row accuracy and be misread as "no payload
# signal" rather than "not a real failure". So drop non-failures before
# stratification unless --include-successes is passed.
_FAILURE_MARKER_KEYS = ("error_code",)          # a real failure reports a code
_FAILURE_TEXT_KEYS = ("event", "event_type", "type", "status")


def _leaf(path):
    return path.rsplit(".", 1)[-1].removesuffix("[]")


def _pairs(record):
    out = []
    _flatten(record, "", out)
    return out


def is_failure_event(record):
    """True when the event actually carries a failure signal: a non-empty
    ``error_code`` leaf anywhere in the record, or failing that an
    event-type / status leaf whose value mentions 'fail'. Inspected against
    data/events.json: error_code presence, ``payload.event ==
    payment.failed`` and ``entity.status == failed`` pick the identical
    1863 rows -- this predicate is the generic form of that."""
    pairs = _pairs(record)
    for path, value in pairs:
        if _leaf(path) in _FAILURE_MARKER_KEYS and value not in (None, ""):
            return True
    for path, value in pairs:
        if _leaf(path) in _FAILURE_TEXT_KEYS and isinstance(value, str) and "fail" in value.lower():
            return True
    return False


def _event_kind(record):
    """Human-readable 'what kind of event is this' from any type/status
    leaves, for the exclusion audit line."""
    bits = {
        f"{_leaf(p)}={v}"
        for p, v in _pairs(record)
        if _leaf(p) in _FAILURE_TEXT_KEYS and isinstance(v, (str, int))
    }
    return ", ".join(sorted(bits)) or "(no type/status field)"


# --- retry dedupe ----------------------------------------------------------
DEDUPE_OFF = ("none", "off", "")


def _earliest_key(event, ts_field, rid_field):
    """Sort key that puts the earliest row first: numeric timestamps before
    string ones, missing timestamps last, ties broken by record id so the
    result is deterministic."""
    ts = resolve(event, ts_field) if ts_field else None
    numeric = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return (
        ts is None,
        not numeric,
        ts if numeric else str(ts),
        str(resolve(event, rid_field) if rid_field else "") or "",
    )


def dedupe_earliest(events, group_field, ts_field, rid_field):
    """Collapse each group (rows sharing ``group_field``) to its earliest
    row by ``ts_field``. Rows with no group key pass through untouched.
    Returns (kept_events, n_collapsed)."""
    groups = collections.OrderedDict()
    passthrough = []
    for event in events:
        key = resolve(event, group_field)
        if key is None:
            passthrough.append(event)
        else:
            groups.setdefault(str(key), []).append(event)

    kept = list(passthrough)
    collapsed = 0
    for members in groups.values():
        if len(members) == 1:
            kept.append(members[0])
        else:
            members.sort(key=lambda e: _earliest_key(e, ts_field, rid_field))
            kept.append(members[0])
            collapsed += len(members) - 1
    return kept, collapsed


def cmd_schema(_args):
    records = load_events()
    total = len(records)
    present = collections.Counter()
    samples = collections.defaultdict(list)

    for record in records:
        pairs = []
        _flatten(record, "", pairs)
        for path in {p for p, _ in pairs}:
            present[path] += 1
        for path, value in pairs:
            if value is None:
                continue
            bucket = samples[path]
            text = _short(value)
            if text not in bucket and len(bucket) < 6:
                bucket.append(text)

    print(f"# schema of {os.path.relpath(EVENTS_PATH, ROOT)}  ({total} records)")
    print(f"# {'coverage':>9}  path / sample values\n")
    for path, count in sorted(present.items(), key=lambda kv: (-kv[1], kv[0])):
        pct = 100.0 * count / total if total else 0.0
        print(f"  {pct:8.1f}%  {path}")
        if samples[path]:
            print(f"{'':>13}e.g. " + "  |  ".join(samples[path]))

    print("\n# candidate id fields (coverage across all records)")
    for cand in SCHEMA_ID_CANDIDATES:
        pct = 100.0 * coverage(records, cand)
        mark = "" if pct else "   (absent)"
        print(f"  {pct:8.1f}%  {cand}{mark}")


# --- sample --------------------------------------------------------------
def _bail_coverage(what, cov_map, flag):
    print(f"no single {what} candidate covers 100% of records:")
    for cand, cov in sorted(cov_map.items(), key=lambda kv: -kv[1]):
        print(f"  {100.0 * cov:6.1f}%  {cand}")
    print(f"pass {flag} explicitly to pick one.")
    raise SystemExit(2)


def _detect_record_id(events):
    for cand in RECORD_ID_CANDIDATES:
        values = [resolve(e, cand) for e in events]
        if all(v is not None for v in values):
            if len({str(v) for v in values}) == len(values):
                return cand
    return None


def _detect_join_fields(events, rows, event_id, truth_id):
    """Return (event_join_id_field, truth_id_field). Auto-detects any side not
    pinned by a flag, choosing the (event, truth) candidate pair -- among
    those with 100% coverage -- that joins the most events. First pair in
    candidate-list order wins ties, so the result is deterministic."""
    ev_cov = {c: coverage(events, c) for c in EVENT_JOIN_ID_CANDIDATES}
    tr_cov = {c: coverage(rows, c) for c in TRUTH_ID_CANDIDATES}
    ev_full = [c for c in EVENT_JOIN_ID_CANDIDATES if ev_cov[c] == 1.0]
    tr_full = [c for c in TRUTH_ID_CANDIDATES if tr_cov[c] == 1.0]

    if event_id is None and not ev_full:
        _bail_coverage("join id (event side)", ev_cov, "--event-id")
    if truth_id is None and not tr_full:
        _bail_coverage("join id (truth side)", tr_cov, "--truth-id")

    ev_options = [event_id] if event_id else ev_full
    tr_options = [truth_id] if truth_id else tr_full
    best = None  # (joined_count, event_field, truth_field)
    for ec in ev_options:
        for tc in tr_options:
            truth_ids = {str(resolve(r, tc)) for r in rows if resolve(r, tc) is not None}
            joined = sum(1 for e in events if str(resolve(e, ec)) in truth_ids)
            if best is None or joined > best[0]:
                best = (joined, ec, tc)
    return best[1], best[2]


def cmd_sample(args):
    n = args.n
    floor = args.min_per_class
    all_events = load_events()
    rows = load_truth_rows()

    # --- failure filter (before join / stratification) ------------------
    if args.include_successes:
        events = all_events
        excluded_non_failure = 0
        failure_predicate = "DISABLED (--include-successes): every event kept"
        print(f"failure filter      : {failure_predicate}")
    else:
        flags = [is_failure_event(e) for e in all_events]
        events = [e for e, ok in zip(all_events, flags) if ok]
        excluded_non_failure = len(all_events) - len(events)
        failure_predicate = "non-empty error_code leaf, or type/status mentions 'fail'"
        dropped_kinds = collections.Counter(
            _event_kind(e) for e, ok in zip(all_events, flags) if not ok
        )
        print(f"failure filter      : keep iff [{failure_predicate}]")
        print(f"excluded (success)  : {excluded_non_failure} non-failure event(s)  "
              f"{dict(dropped_kinds)}   (pass --include-successes to keep them)")

    n_failures = len(events)
    record_id_field = _detect_record_id(events)

    # --- retry dedupe (after failure filter, before stratification) -----
    dedupe_by = args.dedupe_by
    if dedupe_by is None:
        dedupe_by = next((c for c in CUSTOMER_ID_CANDIDATES if coverage(events, c) == 1.0), None)
        if dedupe_by is None:
            dedupe_by, _ = _detect_join_fields(events, rows, args.event_id, args.truth_id)
            print(f"dedupe-by           : {dedupe_by}  (no 100%-covered customer-id field; "
                  f"fell back to the event-side join id)")
    if str(dedupe_by).lower() in DEDUPE_OFF:
        dedupe_by = None

    if dedupe_by is None:
        dedupe_collapsed = 0
        dedupe_ts_field = None
        print("dedupe              : DISABLED")
    else:
        d_cov = coverage(events, dedupe_by)
        dedupe_ts_field = next(
            (c for c in CREATED_AT_CANDIDATES if coverage(events, c) == 1.0), None
        )
        before = len(events)
        events, dedupe_collapsed = dedupe_earliest(
            events, dedupe_by, dedupe_ts_field, record_id_field
        )
        tie = (f"earliest {dedupe_ts_field}" if dedupe_ts_field
               else f"lowest {record_id_field or 'input order'} (no timestamp field found)")
        cov_note = "" if d_cov == 1.0 else (
            f"   [coverage {100 * d_cov:.1f}% -- rows missing the key are kept as-is]"
        )
        print(f"dedupe-by           : {dedupe_by}{cov_note}")
        print(f"retry dups collapsed: {dedupe_collapsed}  "
              f"(kept {tie} per group; {before} -> {len(events)} events)")

    cause_field = args.cause_field
    if cause_field is None:
        cause_cov = {c: coverage(rows, c) for c in CAUSE_FIELD_CANDIDATES}
        full = [c for c in CAUSE_FIELD_CANDIDATES if cause_cov[c] == 1.0]
        if not full:
            _bail_coverage("root-cause field (truth side)", cause_cov, "--cause-field")
        cause_field = full[0]
    cause_basename = cause_field.split(".")[-1]

    event_id, truth_id = _detect_join_fields(events, rows, args.event_id, args.truth_id)

    truth_by_id = {}
    for row in rows:
        key = resolve(row, truth_id)
        if key is not None:
            truth_by_id.setdefault(str(key), row)

    joined, excluded = [], 0
    for index, event in enumerate(events):
        key = resolve(event, event_id)
        truth_row = truth_by_id.get(str(key)) if key is not None else None
        if truth_row is None:
            excluded += 1
            continue
        rid = resolve(event, record_id_field) if record_id_field else f"row-{index:05d}"
        joined.append({
            "id": str(rid),
            "cause": resolve(truth_row, cause_field),
            "event": event,
        })

    print(f"events (all)        : {len(all_events)}")
    print(f"events (failures)   : {n_failures}")
    print(f"events (deduped)    : {len(events)}")
    print(f"excluded (no truth) : {excluded}   (these are printed, never silently dropped)")
    print(f"joined              : {len(joined)}")
    print(f"event join id field : {event_id}")
    print(f"truth id field      : {truth_id}")
    print(f"truth cause field   : {cause_field}")
    print(f"row id field        : {record_id_field or '(synthesized row-NNNNN)'}")

    pool_counts = collections.Counter(r["cause"] for r in joined)
    print(f"\nevents per true class (post-filter, post-dedupe, pre-sample; floor = {floor}):")
    for cls in sorted(pool_counts):
        mark = "ok" if pool_counts[cls] >= floor else "!! BELOW FLOOR"
        print(f"  {str(cls):<22} {pool_counts[cls]:4d}   {mark}")

    if not joined:
        raise SystemExit(
            f"0 of {len(events)} events joined to a truth row on "
            f"{event_id} -> {truth_id}. Pass --event-id / --truth-id explicitly."
        )
    missing_cause = sum(1 for r in joined if r["cause"] is None)
    if missing_cause:
        raise SystemExit(
            f"{missing_cause} joined rows have no '{cause_field}' value; "
            f"pass --cause-field explicitly."
        )

    # --- stratified sample: floor per class first, then random top-up -----
    pools = collections.defaultdict(list)
    for row in joined:
        pools[row["cause"]].append(row)

    rng = random.Random(SEED)
    picked, picked_ids = [], set()
    for cls in sorted(pools):
        # Deterministic floor: sort the class pool by id, take the first
        # `floor` -- no RNG here, so the top-up below is the only random step.
        pool = sorted(pools[cls], key=lambda r: r["id"])
        for row in pool[:min(floor, len(pool))]:
            picked.append(row)
            picked_ids.add(row["id"])

    if len(picked) < n:
        rest = sorted((r for r in joined if r["id"] not in picked_ids), key=lambda r: r["id"])
        need = min(n - len(picked), len(rest))
        picked += rng.sample(rest, need)
    elif len(picked) > n:
        print(f"\nnote: {len(pools)} classes x floor {floor} = {len(picked)} rows already "
              f">= n ({n}); every floor row is kept, no random top-up.")

    rng.shuffle(picked)

    def strip_cause(node):
        if isinstance(node, dict):
            return {k: strip_cause(v) for k, v in node.items() if k != cause_basename}
        if isinstance(node, list):
            return [strip_cause(x) for x in node]
        return node

    blind = [
        {"id": row["id"], "label": None, "note": "", "payload": strip_cause(row["event"])}
        for row in picked
    ]
    ids_in_order = [row["id"] for row in blind]
    digest = sample_digest(ids_in_order)

    manifest = {
        "seed": SEED,
        "event_join_id_field": event_id,
        "truth_id_field": truth_id,
        "cause_field": cause_field,
        "record_id_field": record_id_field,
        "n_requested": n,
        "n_sampled": len(blind),
        "min_per_class": floor,
        "failure_filter": failure_predicate,
        "excluded_non_failure": excluded_non_failure,
        "dedupe_by": dedupe_by,
        "dedupe_timestamp_field": dedupe_ts_field,
        "dedupe_collapsed": dedupe_collapsed,
        "excluded_no_match": excluded,
        "digest": digest,
        "id_to_true_cause": {row["id"]: row["cause"] for row in picked},
    }

    _write_json(BLIND_PATH, blind)
    _write_json(MANIFEST_PATH, manifest)

    counts = collections.Counter(row["cause"] for row in picked)
    print(f"\nper-class sample counts (floor = {floor}):")
    thin = []
    for cls in sorted(counts):
        flag = ""
        if counts[cls] < floor:
            flag = "   << BELOW FLOOR"
            thin.append(cls)
        print(f"  {str(cls):<22} {counts[cls]:3d}{flag}")
    if thin:
        print("\nWARNING: thin class(es): " + ", ".join(thin))
        print("With a thin class the 'worst class' from matrix B is small-sample noise,")
        print("not a finding -- raise --n or lower --min-per-class.")

    print(f"\nwrote {os.path.relpath(BLIND_PATH, ROOT)}  ({len(blind)} rows, order shuffled)")
    print(f"wrote {os.path.relpath(MANIFEST_PATH, ROOT)}  -- answer key, do NOT open while labelling")


# --- label -------------------------------------------------------------
# Keystroke -> root cause. The human presses one of these while reading the
# payload; nothing here is derived from error_description or the manifest.
LABEL_KEYS = {
    "1": "bank_downtime",
    "2": "expired_card",
    "3": "gateway_timeout",
    "4": "insufficient_funds",
    "5": "invalid_card",
    "6": "otp_timeout",
}
LABEL_CLASSES = list(LABEL_KEYS.values()) + ["unknown"]
DIAG_ORDER = ("error_code", "method", "amount", "status", "created_at", "error_description")


def _getkey():
    """Read one keystroke, lower-cased, without waiting for Enter. Falls back
    to line input where raw reads are unavailable. Ctrl-C / Ctrl-D / EOF all
    read as 'q' (and the file is already saved), so an interrupt cannot lose
    a label."""
    try:
        import msvcrt  # Windows
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):   # a function/arrow key: swallow the pair
            msvcrt.getch()
            return "?"
        if ch in (b"\x03", b"\x04", b""):
            return "q"
        return ch.decode("latin-1", "ignore").lower()
    except ImportError:
        pass
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ch in ("\x03", "\x04", ""):
            return "q"
        return ch.lower()
    except Exception:
        line = sys.stdin.readline()
        if not line:
            return "q"
        return (line.strip()[:1] or "?").lower()


def _atomic_write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _diag_of(payload):
    found = {}
    for path, value in _pairs(payload):
        leaf = _leaf(path)
        if leaf in DIAG_ORDER and leaf not in found:
            found[leaf] = value
    return found


def _show_row(rows, cursor):
    row = rows[cursor]
    diag = _diag_of(row["payload"])
    print("\n" + "=" * 68)
    print(f"[ {cursor + 1} / {len(rows)} ]  id={row.get('id')}"
          + ("   (relabelling)" if row.get("label") not in (None, "") else ""))
    for field in DIAG_ORDER:
        if field not in diag:
            print(f"  {field:<18}: (absent)")
            continue
        value = diag[field]
        if field == "created_at" and isinstance(value, (int, float)):
            try:
                iso = datetime.datetime.fromtimestamp(
                    value, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                print(f"  {field:<18}: {value}  ({iso})")
                continue
            except (OverflowError, OSError, ValueError):
                pass
        print(f"  {field:<18}: {value}")
    print()
    print("  1 bank_downtime      2 expired_card       3 gateway_timeout")
    print("  4 insufficient_funds 5 invalid_card       6 otp_timeout")
    print("  u unknown (+note)    b back one row       q save & quit")


def _print_distribution(rows):
    counts = collections.Counter(
        r["label"] for r in rows if r.get("label") not in (None, "")
    )
    n_labeled = sum(counts.values())
    n_unlabeled = len(rows) - n_labeled
    print(f"\nlabel distribution ({n_labeled} labelled / {len(rows)} total):")
    for cls in LABEL_CLASSES:
        share = f"{100 * counts[cls] / n_labeled:5.1f}%" if n_labeled else "   -  "
        print(f"  {cls:<20} {counts[cls]:4d}  {share}")
    print(f"  {'(unlabelled)':<20} {n_unlabeled:4d}")
    if n_labeled and max(counts.values()) / n_labeled > 0.5:
        top = counts.most_common(1)[0][0]
        print(f"\n  NOTE: '{top}' is over half of all labels -- check this is a real "
              f"signal split, not a uniform fill.")


def cmd_label(_args):
    if not os.path.exists(BLIND_PATH):
        raise SystemExit(
            f"{os.path.relpath(BLIND_PATH, ROOT)} not found -- run `sample` first."
        )
    rows = load_json(BLIND_PATH)

    def is_unlabeled(row):
        return row.get("label") in (None, "")

    if not any(is_unlabeled(r) for r in rows):
        print(f"all {len(rows)} rows already labelled -- nothing to do.")
        _print_distribution(rows)
        return

    cursor = next(i for i, r in enumerate(rows) if is_unlabeled(r))
    print(f"{sum(is_unlabeled(r) for r in rows)} of {len(rows)} rows still need a label. "
          f"Resuming at row {cursor + 1}.")

    while cursor < len(rows):
        _show_row(rows, cursor)
        key = _getkey()

        if key == "q":
            _atomic_write_json(BLIND_PATH, rows)
            print("\nsaved.")
            break

        if key == "b":
            if cursor == 0:
                print("  (already at the first row)")
                continue
            cursor -= 1
            _atomic_write_json(BLIND_PATH, rows)
            print("  <- back one row")
            continue

        if key in LABEL_KEYS:
            rows[cursor]["label"] = LABEL_KEYS[key]
            rows[cursor]["note"] = ""
            print(f"  -> {LABEL_KEYS[key]}")
        elif key == "u":
            note = ""
            while not note.strip():
                try:
                    note = input("  note (required for 'unknown'): ")
                except EOFError:
                    note = ""
                    break
            rows[cursor]["label"] = "unknown"
            rows[cursor]["note"] = note.strip()
            if not rows[cursor]["note"]:
                # EOF with no note: don't record a bare abstention
                rows[cursor]["label"] = None
                _atomic_write_json(BLIND_PATH, rows)
                print("  no note given -- left unlabelled.")
                continue
            print(f"  -> unknown  ({rows[cursor]['note']})")
        else:
            shown = key if key.strip() else repr(key)
            print(f"  '{shown}' is not a valid key -- use 1-6, u, b, or q.")
            continue

        _atomic_write_json(BLIND_PATH, rows)
        cursor += 1
        while cursor < len(rows) and not is_unlabeled(rows[cursor]):
            cursor += 1

    else:
        _atomic_write_json(BLIND_PATH, rows)
        print(f"\nall {len(rows)} rows labelled.")

    _print_distribution(rows)


# --- score --------------------------------------------------------------
# What the rules classifier is allowed to read by default. error_code
# collapses 6 causes onto 2; method is a weak extra cue. Deliberately NOT
# error_description (the root cause in plain English) or error_reason (the
# label itself) -- keying on either scores ~100% and proves nothing.
DEFAULT_SIGNAL_PATHS = [
    "payload.payload.payment.entity.error_code",
    "payload.payload.payment.entity.method",
]
# Any signal path with one of these segments is the generator's own answer.
FORBIDDEN_SIGNAL_LEAVES = ("error_description", "error_reason")


def _check_signal_paths(paths):
    bad = [
        p for p in paths
        if any(seg in FORBIDDEN_SIGNAL_LEAVES for seg in p.replace("[]", "").split("."))
    ]
    if bad:
        raise SystemExit(
            "rules classifier may not read the generator's own answer -- "
            f"forbidden path(s) in signal_paths: {bad}. "
            "error_description is the root cause in plain English and "
            "error_reason is the label; a map keyed on either scores ~100% "
            "and measures nothing. Key on error_code / method / amount / "
            "status instead."
        )


def load_classifier(path=None, signal_paths=None):
    cfg = load_json(path or EMAP_PATH)
    cfg.setdefault("join", ":")
    cfg.setdefault("map", {})
    cfg.setdefault("default", "unknown")
    effective = signal_paths or cfg.get("signal_paths") or list(DEFAULT_SIGNAL_PATHS)
    _check_signal_paths(effective)
    cfg["signal_paths"] = list(effective)
    return cfg


def classify(record, cfg):
    """Join the values at signal_paths and look the result up in map; failing
    that, match any single signal value against map; failing that, default."""
    values = [resolve(record, p) for p in cfg["signal_paths"]]
    joined_key = cfg["join"].join("" if v is None else str(v) for v in values)
    mapping = cfg["map"]
    if joined_key in mapping:
        return mapping[joined_key]
    for value in values:
        if value is not None and str(value) in mapping:
            return mapping[str(value)]
    return cfg["default"]


def _print_matrix(title, pairs):
    print(f"\n{title}")
    if not pairs:
        print("  (no rows)")
        return 0.0
    labels = sorted({x for pair in pairs for x in pair if x is not None})
    ref_labels = [l for l in labels if any(a == l for a, _b in pairs)]
    counts = collections.Counter(pairs)
    lab_w = max([9] + [len(l) for l in labels])
    col_w = max(len(l) for l in labels) + 2

    header = " " * lab_w + " |" + "".join(f"{l:>{col_w}}" for l in labels)
    header += f" |{'n':>7}{'acc':>7}"
    print("  " + header)

    total = correct = 0
    for ref in ref_labels:
        row_n = sum(c for (a, _b), c in counts.items() if a == ref)
        row_hit = counts.get((ref, ref), 0)
        total += row_n
        correct += row_hit
        cells = "".join(f"{counts.get((ref, p), 0):>{col_w}}" for p in labels)
        acc = row_hit / row_n if row_n else 0.0
        print(f"  {ref:<{lab_w}} |{cells} |{row_n:>7}{acc:>7.2f}")

    orphan = sum(c for (a, _b), c in counts.items() if a is None)
    if orphan:
        cells = "".join(f"{counts.get((None, p), 0):>{col_w}}" for p in labels)
        print(f"  {'(no ref)':<{lab_w}} |{cells} |{orphan:>7}{'--':>7}")

    overall = correct / total if total else 0.0
    print(f"  -> rows(reference)={total}  overall accuracy={overall:.3f}")
    return overall


def _worst_row(pairs):
    """Worst reference class by per-row accuracy. Ties broken toward the
    larger row (more instances = less of a small-sample artefact), then by
    name for full determinism. Returns (acc, cls, n, confused_into)."""
    labels = sorted({a for a, _b in pairs if a is not None})
    if not labels:
        return None
    counts = collections.Counter(pairs)
    worst = worst_key = None
    for ref in labels:
        row_n = sum(c for (a, _b), c in counts.items() if a == ref)
        row_hit = counts.get((ref, ref), 0)
        acc = row_hit / row_n if row_n else 0.0
        off = [(c, p) for (a, p), c in counts.items() if a == ref and p != ref]
        confused_into = min(off, key=lambda t: (-t[0], t[1]))[1] if off else "-"
        key = (acc, -row_n, ref)
        if worst_key is None or key < worst_key:
            worst, worst_key = (acc, ref, row_n, confused_into), key
    return worst


def cmd_score(args):
    if not os.path.exists(BLIND_PATH):
        raise SystemExit(f"{os.path.relpath(BLIND_PATH, ROOT)} not found -- run `sample` first.")
    if not os.path.exists(MANIFEST_PATH):
        raise SystemExit(f"{os.path.relpath(MANIFEST_PATH, ROOT)} not found -- run `sample` first.")
    if not os.path.exists(EMAP_PATH):
        raise SystemExit(
            f"{os.path.relpath(EMAP_PATH, ROOT)} not found -- build it from `schema` output first."
        )

    blind = load_json(BLIND_PATH)
    manifest = load_json(MANIFEST_PATH)
    cfg = load_classifier(signal_paths=getattr(args, "signal_paths", None))
    truth_map = manifest["id_to_true_cause"]

    unlabeled = [r for r in blind if r["label"] in (None, "")]
    labeled = [r for r in blind if r["label"] not in (None, "")]
    abstained = [r for r in labeled if r["label"] == "unknown"]
    scored = [r for r in labeled if r["label"] != "unknown"]

    print(f"sampled   : {len(blind)}")
    print(f"labeled   : {len(labeled)}")
    print(f"unlabeled : {len(unlabeled)}")
    print(f"abstained : {len(abstained)}   (label == 'unknown', kept and counted)")
    if unlabeled:
        print(f"WARNING: {len(unlabeled)} row(s) unlabeled -- the score below is incomplete.")

    prediction = {r["id"]: classify(r["payload"], cfg) for r in blind}

    # rows = reference, cols = predicted. "X vs Y" => X is predicted, Y is
    # the reference it is judged against.
    rows_a = [(truth_map.get(r["id"]), r["label"]) for r in labeled]
    rows_b = [(r["label"], prediction[r["id"]]) for r in scored]
    rows_c = [(truth_map.get(r["id"]), prediction[r["id"]]) for r in scored]

    _print_matrix(
        "A. human labels vs ground truth -- the payload signal ceiling.\n"
        "   rows = true root cause, cols = blind human label.\n"
        "   per-row accuracy = how often a human recovers that cause from the\n"
        "   payload alone. Where a human cannot tell, no rule and no Slice 7\n"
        "   LLM can either -- this row is the ceiling for every later method.",
        rows_a,
    )
    src = ("--signal-paths" if getattr(args, "signal_paths", None)
           else "error_code_map.json" if load_json(EMAP_PATH).get("signal_paths")
           else "built-in default")
    print("\nrules classifier may read ONLY these paths (" + src + "):")
    for p in cfg["signal_paths"]:
        print(f"    {p}")
    print("  (error_description and error_reason are rejected at load time.)")
    _print_matrix(
        "B. rules vs human labels -- the real test.\n"
        "   rows = blind human label, cols = rules prediction.\n"
        "   per-row accuracy = how often the rules reproduce a human's blind call.",
        rows_b,
    )
    _print_matrix(
        "C. rules vs ground truth -- INFLATED, for contrast with B only.\n"
        "   rows = true root cause, cols = rules prediction. The error codes were\n"
        "   written from the root cause, so this over-credits the rules; the gap\n"
        "   between C and B is that inflation.",
        rows_c,
    )

    worst = _worst_row(rows_b)
    print("\n" + "-" * 70)
    if worst:
        acc, cls, row_n, into = worst
        print(f"matrix B worst class : {cls}  (per-row accuracy {acc:.2f}, n={row_n})")
        print(f"most often confused  : {cls} -> {into}")
    else:
        print("matrix B has no scored rows yet -- finish labeling, then re-run `score`.")

    print("\nPASS CONDITION -- say these two lines out loud, from understanding,")
    print("before you read them back off this report:")
    if worst:
        acc, cls, row_n, into = worst
        print(f"  1. The worst class for the rules is '{cls}' "
              f"(per-row accuracy {acc:.2f}, n={row_n}).")
        print(f"  2. When the rules miss '{cls}', they call it '{into}'.")
    else:
        print("  1. <worst class>          -- not computable until the sample is labeled.")
        print("  2. <class it collapses into> -- same.")
    print("If you cannot state them from memory, the audit has not really passed:")
    print("you are trusting a number you do not yet understand.")


# --- entrypoint --------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(prog="label_harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("schema", help="dump event payload schema + id-field coverage")

    sp = sub.add_parser("sample", help="draw a blind, stratified label sample")
    sp.add_argument("--n", type=int, default=100, help="target sample size (default 100)")
    sp.add_argument("--min-per-class", type=int, default=15,
                    help="minimum rows per true root cause (default 15)")
    sp.add_argument("--event-id", default=None, help="force the event-side join id path")
    sp.add_argument("--truth-id", default=None, help="force the truth-side join id path")
    sp.add_argument("--cause-field", default=None, help="force the truth-side root-cause field")
    sp.add_argument("--include-successes", action="store_true",
                    help="keep non-failure events (default: drop them; a "
                         "payment.authorized row is not a diagnosis target)")
    sp.add_argument("--dedupe-by", default=None, metavar="FIELD",
                    help="collapse rows sharing this event field to their "
                         "earliest (by created_at); default = the event-side "
                         "customer-id field. Pass 'none' to disable.")

    sub.add_parser("label", help="hand-label the blind sample, one keystroke per row")

    sc = sub.add_parser("score", help="print the three confusion matrices")
    sc.add_argument("--signal-paths", nargs="+", default=None, metavar="PATH",
                    help="restrict the rules classifier to these event paths "
                         "(default: error_code + method). Paths containing "
                         "error_description or error_reason are rejected.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    {
        "schema": cmd_schema,
        "sample": cmd_sample,
        "label": cmd_label,
        "score": cmd_score,
    }[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    main()
