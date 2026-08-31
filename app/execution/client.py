"""Slice 10 - the execution seam. One Protocol both clients satisfy, the value
objects that cross it, and the deterministic idempotency key.

BUSINESS ATTEMPT vs TRANSPORT RETRY
----------------------------------
``attempt_n`` is the BUSINESS attempt: a new, deliberate re-contact of the
customer -- a human or a policy decides "reach out again". It is the ONLY input
that changes the idem_key, so a genuine second nudge gets a fresh key and a
fresh provider object.

A TRANSPORT retry -- what ``executor._call_with_retries`` does after a 5xx /
timeout / connection error -- does NOT touch ``attempt_n`` and MUST reuse the
SAME idem_key. The provider therefore sees one logical operation no matter how
many HTTP attempts it took. See DECISIONS.md Slice 10.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from typing import Protocol, runtime_checkable


class ExecutionStatus(str, enum.Enum):
    SENT = "SENT"                          # provider accepted a fresh send
    DUPLICATE = "DUPLICATE"                # provider already had this idem_key
    FAILED_RETRIABLE = "FAILED_RETRIABLE"  # 5xx / timeout / conn error -- NON-terminal
    FAILED_TERMINAL = "FAILED_TERMINAL"    # 4xx / payload mismatch -- terminal

    def __str__(self) -> str:  # so str(status) == "SENT", not "ExecutionStatus.SENT"
        return self.value


TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.SENT, ExecutionStatus.DUPLICATE, ExecutionStatus.FAILED_TERMINAL}
)


@dataclasses.dataclass(frozen=True)
class ExecutionRequest:
    idem_key: str
    event_id: str
    action_type: str
    attempt_n: int
    payload: dict


@dataclasses.dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    provider_ref: str | None = None
    http_status: int | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@runtime_checkable
class ActionClient(Protocol):
    def send(self, req: ExecutionRequest) -> ExecutionResult: ...

    # Reconcile (Slice 10 item 8) needs the provider's own view keyed by
    # reference_id == idem_key. Kept on the same Protocol so one object serves
    # both the send path and startup reconciliation.
    def lookup(self, idem_key: str) -> ExecutionResult | None: ...


# Hash-space separator. Distinct from "assign:" (arm assignment, app/arms.py)
# and from the bare "{seed}:{customer_id}" outcome-resolution draw
# (eval/environment.py), so an idem_key can never collide with either.
_IDEM_PREFIX = "exec:"


def compute_idem_key(event_id: str, action_type: str, attempt_n: int) -> str:
    """``sha256("exec:{event_id}:{action_type}:{attempt_n}").hexdigest()[:32]``.

    Only the BUSINESS attempt (``attempt_n``) moves this. A transport retry
    after a 5xx reuses the exact key -- see the module docstring.
    """
    raw = f"{_IDEM_PREFIX}{event_id}:{action_type}:{attempt_n}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def request_fingerprint(payload: dict) -> str:
    """sha256 of the canonicalised payload. Detects a caller mutating the body
    under an idem_key that is already on the ledger (executor step c)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def redact(text: str | None, *secrets: str) -> str | None:
    """Replace every non-empty secret with '***'. Run on every error string
    before it can reach a log line or a ledger row."""
    if not text:
        return text
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out
