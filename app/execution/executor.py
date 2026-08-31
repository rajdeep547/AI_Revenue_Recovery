"""Slice 10 - the execute path. Intent-before-send ordering is non-negotiable.

    a. compute idem_key + request_fingerprint
    b. intent exists with a TERMINAL outcome  -> return it, ZERO provider calls
    c. intent exists, SAME idem_key, DIFFERENT fingerprint
                                             -> FAILED_TERMINAL
                                                'idem_key_payload_mismatch', no send
    d. insert intent, COMMIT, THEN call the provider (with transport retries)
    e. append the outcome

An intent that exists with a non-terminal outcome (or no outcome) and a
matching fingerprint is NOT re-sent here -- that is a crash / in-flight state
and belongs to ``reconcile`` (item 8). ``execute`` returns the last known
non-terminal state instead, so two workers racing the same idem_key make
exactly one provider call (the PRIMARY KEY on ``execution_intents`` is the
lock).
"""

from __future__ import annotations

import random
import sqlite3
import time

from app.execution.client import (
    ActionClient,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    compute_idem_key,
    request_fingerprint,
)
from app.execution.ledger import (
    _utcnow_iso,
    append_outcome,
    get_intent,
    init_execution_ledger,
    insert_intent,
    latest_outcome,
    terminal_outcome,
)

# 3 transport retries after the first attempt = up to 4 HTTP attempts, the SAME
# idem_key on every one. Exponential 0.5 / 1 / 2 s, plus 0-25% jitter.
_BACKOFFS_S = (0.5, 1.0, 2.0)


def _call_with_retries(client: ActionClient, req: ExecutionRequest, *,
                       sleep=time.sleep, rng: random.Random | None = None) -> ExecutionResult:
    rng = rng or random.Random()
    result = client.send(req)
    for base in _BACKOFFS_S:
        if result.status is not ExecutionStatus.FAILED_RETRIABLE:
            return result
        sleep(base + rng.uniform(0.0, base * 0.25))  # exponential backoff + jitter
        result = client.send(req)                     # SAME req -> SAME idem_key
    return result  # may still be FAILED_RETRIABLE -> exhausted, left for reconcile


def execute(
    client: ActionClient,
    *,
    db_path,
    event_id: str,
    action_type: str,
    attempt_n: int,
    payload: dict,
    now: str | None = None,
    sleep=time.sleep,
    rng: random.Random | None = None,
    _pre_commit=None,
    _post_commit=None,
) -> ExecutionResult:
    now = now or _utcnow_iso()
    idem_key = compute_idem_key(event_id, action_type, attempt_n)  # (a)
    fp = request_fingerprint(payload)

    init_execution_ledger(db_path)
    intent = get_intent(db_path, idem_key)

    if intent is not None:
        term = terminal_outcome(db_path, idem_key)
        if term is not None:                                       # (b)
            return term
        if intent["request_fingerprint"] != fp:                    # (c)
            res = ExecutionResult(
                ExecutionStatus.FAILED_TERMINAL, error="idem_key_payload_mismatch"
            )
            append_outcome(db_path, idem_key=idem_key, result=res, now=now)
            return res
        # same key, same payload, not terminal: an earlier attempt did not
        # finish. execute() does NOT re-send (double-send risk across workers);
        # reconcile owns recovery. Return the last known non-terminal state.
        return latest_outcome(db_path, idem_key) or ExecutionResult(
            ExecutionStatus.FAILED_RETRIABLE, error="execution_in_flight"
        )

    try:                                                          # (d) intent-before-send
        insert_intent(
            db_path, idem_key=idem_key, event_id=event_id, action_type=action_type,
            attempt_n=attempt_n, request_fingerprint=fp, now=now, _pre_commit=_pre_commit,
        )
    except sqlite3.IntegrityError:
        # a concurrent executor won the PRIMARY KEY race. Do NOT send.
        term = terminal_outcome(db_path, idem_key)
        if term is not None:
            return term
        return ExecutionResult(
            ExecutionStatus.FAILED_RETRIABLE, error="concurrent_execution_in_progress"
        )

    if _post_commit is not None:
        _post_commit()  # crash-test seam: after COMMIT, before any network call

    req = ExecutionRequest(idem_key, event_id, action_type, attempt_n, payload)
    result = _call_with_retries(client, req, sleep=sleep, rng=rng)      # (d)
    append_outcome(db_path, idem_key=idem_key, result=result, now=_utcnow_iso())  # (e)
    return result
