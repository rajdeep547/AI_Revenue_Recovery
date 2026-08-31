"""Slice 9 - guardrails. Evaluate-all-then-decide.

Seven guardrails sit between the EV ladder (Slice 7, which picks a best rung)
and a dispatch that this codebase still does not perform (Slice 8). Each is a
*pure predicate* over ``(event, chosen_rung, state, now)`` returning a
:class:`GuardrailResult`. :func:`evaluate_all` runs ALL SEVEN, in a fixed
order, with no early return and no short-circuit -- so a persisted evaluation
is always seven rows and a reviewer can see every guardrail's verdict even
after the first one blocked.

Fixed order == precedence order::

    kill_switch -> opt_out -> attempt_cap -> contact_limit
                -> quiet_hours -> spend_cap -> dry_run

``report.blocked_by`` is every guardrail that blocked (may be > 1). The
terminal code is ``BLOCKED/<PRIMARY>`` where PRIMARY is the FIRST blocker in
the order above; ``blocked_by`` still retains all of them.

The seven
-----
1. ``kill_switch``  - global bool. Blocks every rung, ``retry_silent`` included.
2. ``opt_out``      - customer on the suppression list. Blocks contact rungs;
                      ``retry_silent`` still runs (no message reaches anyone).
3. ``attempt_cap``  - max lifetime dispatched actions per ``payment_id``.
4. ``contact_limit``- max dispatched contact-rung actions per ``customer_id``
                      in a rolling window, counted across channels.
5. ``quiet_hours``  - blocks sms / whatsapp / agent_call 21:00-09:00 IST.
                      email and ``retry_silent`` pass.
6. ``spend_cap``    - daily INR ceiling; blocks when
                      ``spent_today + rung_cost > cap`` (strict).
7. ``dry_run``      - never blocks. Marks the action not-dispatched.

Ladder walk
-----------
:func:`walk_ladder` takes the rungs best-first (the engine's EV order), calls
:func:`evaluate_all` on each, and stops at the first rung whose report is not
blocked. Every rung tried keeps its own full report in ``attempts`` -- the
walk is never collapsed into one record. If every rung is blocked the
terminal is ``BLOCKED/<PRIMARY of the highest rung tried>`` (``attempts[0]``).

Persistence
-----------
``guardrail_evaluations`` - append-only, same trigger pattern as Slice 2's
``audit``. Seven rows per :func:`evaluate_all`, always
(``event_id, customer_id, rung, ts, guardrail_name, blocked, reason,
detail_json``). ``spend_ledger`` - append-only; a row is a real debit ONLY on
a real dispatch, otherwise amount 0.00 with a status flag. The spend-cap
window reads the sum of real debits for the current IST day.

Arm integrity
-------------
Guardrails never touch arm assignment. A treatment-arm event that gets fully
blocked stays treatment arm and is a THIRD outcome class, ``treatment_blocked``
-- distinct from ``control`` and ``treatment_acted``, reported separately and
excluded from the uplift denominator (see ``eval/measurement.py``). This needs
no schema change to the ``decisions`` table: the arm is already inferable from
the recorded Decision and the block is fully described by the
``guardrail_evaluations`` rows.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "guardrails.json"

# Asia/Kolkata is a DST-free fixed offset of +05:30, unchanged since 1945. The
# target interpreter ships no tzdata, so ZoneInfo("Asia/Kolkata") is
# unavailable -- a fixed-offset tzinfo is exact here and adds no dependency.
IST = timezone(timedelta(hours=5, minutes=30))

# Fixed evaluation order. This IS the precedence order: PRIMARY is the first
# blocked guardrail in this tuple.
GUARDRAIL_ORDER = (
    "kill_switch",
    "opt_out",
    "attempt_cap",
    "contact_limit",
    "quiet_hours",
    "spend_cap",
    "dry_run",
)

# retry_silent is the only rung that reaches no one; every other rung puts a
# message in front of the customer -> "contact rung". Mirrors
# config/decision_policy.json's action_ladder (requires_channel != null).
SILENT_RUNG = "retry_silent"


# --------------------------------------------------------------- rung helpers
def _rung_name(rung) -> str:
    return rung if isinstance(rung, str) else rung["name"]


def _is_contact_rung(rung) -> bool:
    """True for every rung except retry_silent. When a ladder dict is passed
    this reads ``requires_channel``; a bare name falls back to the constant."""
    if isinstance(rung, dict) and "requires_channel" in rung:
        return rung["requires_channel"] is not None
    return _rung_name(rung) != SILENT_RUNG


# ------------------------------------------------------------------ time helpers
def _parse_iso(value) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_ist(value) -> datetime:
    return _parse_iso(value).astimezone(IST)


def ist_day(value) -> str:
    """The ``YYYY-MM-DD`` IST calendar day containing ``value`` (any ISO
    string, offset-aware or assumed-UTC). This is the spend-cap window key."""
    return _to_ist(value).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------- config
@dataclass(frozen=True)
class GuardrailConfig:
    kill_switch: bool = False
    dry_run: bool = False
    attempt_cap_max: int = 3
    contact_limit_max: int = 2
    contact_limit_window_hours: int = 24
    quiet_hours_start_ist_hour: int = 21
    quiet_hours_end_ist_hour: int = 9
    quiet_hours_blocked_rungs: tuple = ("sms", "whatsapp", "agent_call")
    spend_cap_inr: float = 500.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> "GuardrailConfig":
        with open(path or CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            kill_switch=bool(raw["kill_switch"]),
            dry_run=bool(raw["dry_run"]),
            attempt_cap_max=int(raw["attempt_cap_max_per_payment_lifetime"]),
            contact_limit_max=int(raw["contact_limit_max_per_customer"]),
            contact_limit_window_hours=int(raw["contact_limit_window_hours"]),
            quiet_hours_start_ist_hour=int(raw["quiet_hours_start_ist_hour"]),
            quiet_hours_end_ist_hour=int(raw["quiet_hours_end_ist_hour"]),
            quiet_hours_blocked_rungs=tuple(raw["quiet_hours_blocked_rungs"]),
            spend_cap_inr=float(raw["spend_cap_inr_per_ist_day"]),
        )


@dataclass(frozen=True)
class GuardrailState:
    """Everything the predicates need that isn't in ``event``/``rung``/``now``.
    The caller assembles it (see :func:`load_state`); the predicates stay pure
    over it. ``config`` rides along so ``evaluate_all`` keeps its 4-arg shape.
    """

    config: GuardrailConfig = field(default_factory=GuardrailConfig)
    opted_out: bool = False
    payment_action_count: int = 0        # lifetime dispatched actions, this payment_id
    contact_actions_in_window: int = 0   # dispatched contact actions, this customer, rolling window
    spent_today_inr: float = 0.0         # sum of REAL debits for the current IST day


# ----------------------------------------------------------------------- result
@dataclass(frozen=True)
class GuardrailResult:
    name: str
    blocked: bool
    reason: str | None
    detail: dict


@dataclass(frozen=True)
class GuardrailReport:
    """The full verdict for one rung: exactly seven results in
    :data:`GUARDRAIL_ORDER`, blocked and not-blocked alike."""

    rung: str
    results: tuple

    def __post_init__(self):
        got = tuple(r.name for r in self.results)
        if got != GUARDRAIL_ORDER:
            raise ValueError(
                f"a GuardrailReport must carry all seven results in the fixed "
                f"order {GUARDRAIL_ORDER}; got {got}"
            )

    @property
    def blocked_by(self) -> list:
        """Every guardrail that blocked, in fixed order (so ``[0]`` is PRIMARY)."""
        return [r.name for r in self.results if r.blocked]

    @property
    def blocked(self) -> bool:
        return any(r.blocked for r in self.results)

    @property
    def primary(self) -> str | None:
        b = self.blocked_by
        return b[0] if b else None

    @property
    def terminal(self) -> str | None:
        """``BLOCKED/<PRIMARY>`` (PRIMARY upper-cased), or ``None`` if nothing blocked."""
        p = self.primary
        return f"BLOCKED/{p.upper()}" if p else None

    def result(self, name: str) -> GuardrailResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(name)

    @property
    def is_dry_run(self) -> bool:
        return bool(self.result("dry_run").detail.get("dry_run"))

    @property
    def dispatched(self) -> bool:
        """A real send happens iff nothing blocked AND we are not in dry-run."""
        return (not self.blocked) and not self.is_dry_run


# ------------------------------------------------------------------- predicates
def _g_kill_switch(event, rung, state, now) -> GuardrailResult:
    on = bool(state.config.kill_switch)
    return GuardrailResult(
        "kill_switch",
        on,
        "kill switch engaged: every recovery action is halted" if on else None,
        {"kill_switch": on, "rung": _rung_name(rung)},
    )


def _g_opt_out(event, rung, state, now) -> GuardrailResult:
    contact = _is_contact_rung(rung)
    blocked = bool(state.opted_out) and contact
    return GuardrailResult(
        "opt_out",
        blocked,
        "customer is on the suppression list: contact rungs blocked "
        "(retry_silent still permitted)" if blocked else None,
        {"opted_out": bool(state.opted_out), "is_contact_rung": contact,
         "rung": _rung_name(rung)},
    )


def _g_attempt_cap(event, rung, state, now) -> GuardrailResult:
    cap = int(state.config.attempt_cap_max)
    n = int(state.payment_action_count)
    blocked = n >= cap
    return GuardrailResult(
        "attempt_cap",
        blocked,
        f"payment already has {n} lifetime action(s); cap is {cap}" if blocked else None,
        {"payment_action_count": n, "cap": cap, "rung": _rung_name(rung)},
    )


def _g_contact_limit(event, rung, state, now) -> GuardrailResult:
    contact = _is_contact_rung(rung)
    cap = int(state.config.contact_limit_max)
    n = int(state.contact_actions_in_window)
    window = int(state.config.contact_limit_window_hours)
    blocked = contact and n >= cap
    return GuardrailResult(
        "contact_limit",
        blocked,
        f"{n} contact action(s) to this customer in the last {window}h; cap is {cap}"
        if blocked else None,
        {"contact_actions_in_window": n, "cap": cap, "window_hours": window,
         "is_contact_rung": contact, "rung": _rung_name(rung)},
    )


def _g_quiet_hours(event, rung, state, now) -> GuardrailResult:
    ist = _to_ist(now)
    minutes = ist.hour * 60 + ist.minute
    start = state.config.quiet_hours_start_ist_hour * 60
    end = state.config.quiet_hours_end_ist_hour * 60
    if start > end:  # window wraps midnight (21:00 -> 09:00)
        in_window = minutes >= start or minutes < end
    else:
        in_window = start <= minutes < end
    affected = _rung_name(rung) in tuple(state.config.quiet_hours_blocked_rungs)
    blocked = in_window and affected
    window_label = (
        f"{state.config.quiet_hours_start_ist_hour:02d}:00-"
        f"{state.config.quiet_hours_end_ist_hour:02d}:00 IST"
    )
    return GuardrailResult(
        "quiet_hours",
        blocked,
        f"{ist.strftime('%H:%M')} IST is inside quiet hours {window_label}; "
        f"{_rung_name(rung)} waits until morning" if blocked else None,
        {"ist_time": ist.isoformat(), "in_quiet_window": in_window,
         "rung_affected": affected, "window_ist": window_label,
         "rung": _rung_name(rung)},
    )


def _g_spend_cap(event, rung, state, now) -> GuardrailResult:
    cap = float(state.config.spend_cap_inr)
    spent = float(state.spent_today_inr)
    cost = float(rung["cost_inr"]) if isinstance(rung, dict) else 0.0
    would = round(spent + cost, 2)
    blocked = (spent + cost) > cap
    return GuardrailResult(
        "spend_cap",
        blocked,
        f"today's spend Rs {spent:.2f} + Rs {cost:.2f} (= Rs {would:.2f}) "
        f"exceeds the Rs {cap:.2f} daily cap" if blocked else None,
        {"spent_today_inr": spent, "rung_cost_inr": cost, "cap_inr": cap,
         "would_total_inr": would, "rung": _rung_name(rung)},
    )


def _g_dry_run(event, rung, state, now) -> GuardrailResult:
    dry = bool(state.config.dry_run)
    # dry_run NEVER blocks. It only records that no dispatch will occur.
    return GuardrailResult(
        "dry_run",
        False,
        "dry-run mode: the action is recorded but not dispatched" if dry else None,
        {"dry_run": dry, "dispatched": not dry, "rung": _rung_name(rung)},
    )


_PREDICATES = {
    "kill_switch": _g_kill_switch,
    "opt_out": _g_opt_out,
    "attempt_cap": _g_attempt_cap,
    "contact_limit": _g_contact_limit,
    "quiet_hours": _g_quiet_hours,
    "spend_cap": _g_spend_cap,
    "dry_run": _g_dry_run,
}
assert tuple(_PREDICATES) == GUARDRAIL_ORDER, "predicate registry out of contract order"


# ------------------------------------------------------------------ evaluate_all
def evaluate_all(event: dict, rung, state: GuardrailState, now: str) -> GuardrailReport:
    """Run ALL SEVEN guardrails over ``(event, rung, state, now)``.

    No early return, no short-circuit: every predicate runs and lands in the
    report even after another has already blocked. ``report.blocked_by`` lists
    every blocker in fixed order; ``report.terminal`` is ``BLOCKED/<first
    blocker>``.
    """
    results = tuple(
        _PREDICATES[name](event, rung, state, now) for name in GUARDRAIL_ORDER
    )
    return GuardrailReport(rung=_rung_name(rung), results=results)


# ------------------------------------------------------------------- ladder walk
@dataclass(frozen=True)
class LadderOutcome:
    terminal: str            # "ACT" | "BLOCKED/<PRIMARY>"
    chosen_rung: str | None  # the rung that passed, or None if all blocked
    dispatched: bool         # False under dry-run or when all rungs blocked
    attempts: tuple          # one GuardrailReport per rung tried, in walk order

    @property
    def blocked(self) -> bool:
        return self.chosen_rung is None


def walk_ladder(event: dict, ranked_rungs, state: GuardrailState, now: str) -> LadderOutcome:
    """``ranked_rungs``: ladder entries best-first by EV (the engine's order).

    Try each in turn; the first rung whose report is not blocked is chosen and
    the walk stops. Every rung tried gets its own full :class:`GuardrailReport`
    in ``attempts`` -- the walk is never collapsed. If every rung is blocked,
    the terminal is ``BLOCKED/<PRIMARY of the highest rung tried>``
    (``attempts[0]``).
    """
    ranked_rungs = list(ranked_rungs)
    if not ranked_rungs:
        raise ValueError("walk_ladder needs at least one rung")
    attempts: list[GuardrailReport] = []
    for rung in ranked_rungs:
        report = evaluate_all(event, rung, state, now)
        attempts.append(report)
        if not report.blocked:
            return LadderOutcome(
                terminal="ACT",
                chosen_rung=report.rung,
                dispatched=report.dispatched,
                attempts=tuple(attempts),
            )
    return LadderOutcome(
        terminal=attempts[0].terminal,
        chosen_rung=None,
        dispatched=False,
        attempts=tuple(attempts),
    )


# ------------------------------------------------------------------- persistence
_CREATE_EVALS = """
CREATE TABLE IF NOT EXISTS guardrail_evaluations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    rung           TEXT NOT NULL,
    ts             TEXT NOT NULL,
    guardrail_name TEXT NOT NULL,
    blocked        INTEGER NOT NULL,
    reason         TEXT,
    detail_json    TEXT NOT NULL
)
"""

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS spend_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    payment_id  TEXT NOT NULL,
    rung        TEXT NOT NULL,
    amount_inr  REAL NOT NULL,
    status      TEXT NOT NULL,   -- 'debit' | 'dry_run' | 'blocked'
    ist_day     TEXT NOT NULL,   -- YYYY-MM-DD in IST, the cap-window key
    ts          TEXT NOT NULL
)
"""

_CREATE_SUPPRESSION = """
CREATE TABLE IF NOT EXISTS suppression_list (
    customer_id TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    reason      TEXT
)
"""

# Append-only in the DB, same pattern as app/db.py's audit_no_update/_no_delete.
_APPEND_ONLY_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS guardrail_evaluations_no_update
       BEFORE UPDATE ON guardrail_evaluations
       BEGIN SELECT RAISE(ABORT, 'guardrail_evaluations is append-only: UPDATE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS guardrail_evaluations_no_delete
       BEFORE DELETE ON guardrail_evaluations
       BEGIN SELECT RAISE(ABORT, 'guardrail_evaluations is append-only: DELETE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS spend_ledger_no_update
       BEFORE UPDATE ON spend_ledger
       BEGIN SELECT RAISE(ABORT, 'spend_ledger is append-only: UPDATE not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS spend_ledger_no_delete
       BEFORE DELETE ON spend_ledger
       BEGIN SELECT RAISE(ABORT, 'spend_ledger is append-only: DELETE not allowed'); END""",
)

_REAL_DEBIT = "debit"
_DRY_RUN = "dry_run"
_BLOCKED = "blocked"


def init_guardrail_store(db_path: str | Path) -> None:
    """Idempotent. Creates ``guardrail_evaluations``, ``spend_ledger`` and
    ``suppression_list`` plus the append-only triggers on the first two."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_CREATE_EVALS)
        conn.execute(_CREATE_LEDGER)
        conn.execute(_CREATE_SUPPRESSION)
        for trig in _APPEND_ONLY_TRIGGERS:
            conn.execute(trig)
        conn.commit()
    finally:
        conn.close()


def record_evaluation(
    report: GuardrailReport, *, event_id: str, customer_id: str,
    db_path: str | Path, ts: str,
) -> None:
    """Persist the FULL report: exactly seven ``guardrail_evaluations`` rows,
    one per guardrail, always -- blocked and not-blocked alike."""
    rows = [
        (event_id, customer_id, report.rung, ts, r.name, 1 if r.blocked else 0,
         r.reason, json.dumps(r.detail, sort_keys=True, default=str))
        for r in report.results
    ]
    if len(rows) != 7:
        raise AssertionError(f"expected 7 guardrail rows, built {len(rows)}")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO guardrail_evaluations "
            "(event_id, customer_id, rung, ts, guardrail_name, blocked, reason, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def record_ladder_walk(
    outcome: LadderOutcome, *, event_id: str, customer_id: str,
    db_path: str | Path, ts: str,
) -> None:
    """One seven-row block per rung attempted -- the walk is never collapsed."""
    for report in outcome.attempts:
        record_evaluation(
            report, event_id=event_id, customer_id=customer_id, db_path=db_path, ts=ts
        )


def record_spend(
    *, event_id: str, customer_id: str, payment_id: str, rung, db_path: str | Path,
    now: str, dispatched: bool, dry_run: bool,
) -> float:
    """Append one ``spend_ledger`` row and return the amount written.

    Real dispatch (``dispatched and not dry_run``) -> ``status='debit'``,
    amount = the rung's cost. Dry-run -> ``status='dry_run'``, amount 0.00.
    Anything else (blocked) -> ``status='blocked'``, amount 0.00. Only a
    ``debit`` row moves the spend-cap window.
    """
    cost = float(rung["cost_inr"]) if isinstance(rung, dict) else 0.0
    if dispatched and not dry_run:
        amount, status = round(cost, 2), _REAL_DEBIT
    elif dry_run:
        amount, status = 0.00, _DRY_RUN
    else:
        amount, status = 0.00, _BLOCKED
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO spend_ledger "
            "(event_id, customer_id, payment_id, rung, amount_inr, status, ist_day, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, customer_id, payment_id, _rung_name(rung), amount, status,
             ist_day(now), now),
        )
        conn.commit()
    finally:
        conn.close()
    return amount


def spent_today_inr(db_path: str | Path, *, now: str) -> float:
    """Sum of REAL debits (``status='debit'``) for the IST day containing
    ``now``. Dry-run and blocked rows (amount 0.00) never move this."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_inr), 0.0) FROM spend_ledger "
            "WHERE status = ? AND ist_day = ?",
            (_REAL_DEBIT, ist_day(now)),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0])


# ------------------------------------------------------------- suppression list
def add_suppression(db_path: str | Path, customer_id: str, *, now: str, reason: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO suppression_list (customer_id, added_at, reason) "
            "VALUES (?, ?, ?)",
            (customer_id, now, reason),
        )
        conn.commit()
    finally:
        conn.close()


def is_opted_out(db_path: str | Path, customer_id: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        hit = conn.execute(
            "SELECT 1 FROM suppression_list WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    finally:
        conn.close()
    return hit is not None


# ---------------------------------------------------------------- state assembly
def _count_payment_actions(db_path: str | Path, payment_id: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM spend_ledger WHERE payment_id = ? AND status = ?",
            (payment_id, _REAL_DEBIT),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _count_contact_actions_since(db_path: str | Path, customer_id: str, since_iso: str) -> int:
    since = _parse_iso(since_iso)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT rung, ts FROM spend_ledger WHERE customer_id = ? AND status = ?",
            (customer_id, _REAL_DEBIT),
        ).fetchall()
    finally:
        conn.close()
    return sum(
        1 for name, ts in rows if _is_contact_rung(name) and _parse_iso(ts) >= since
    )


def load_state(
    db_path: str | Path, *, customer_id: str, payment_id: str,
    config: GuardrailConfig | None = None, now: str,
) -> GuardrailState:
    """Assemble a :class:`GuardrailState` from the guardrail tables for one
    pending decision. Pure DB reads -- the predicates stay pure over what this
    returns. The rolling contact-limit window is ``now`` minus
    ``config.contact_limit_window_hours``."""
    config = config or GuardrailConfig.load()
    window_start = (
        _parse_iso(now) - timedelta(hours=config.contact_limit_window_hours)
    ).isoformat()
    return GuardrailState(
        config=config,
        opted_out=is_opted_out(db_path, customer_id),
        payment_action_count=_count_payment_actions(db_path, payment_id),
        contact_actions_in_window=_count_contact_actions_since(db_path, customer_id, window_start),
        spent_today_inr=spent_today_inr(db_path, now=now),
    )
