"""Slice 10 - real execution.

The seam is :class:`ActionClient` (``send`` + ``lookup``); the two
implementations are :class:`FakeActionClient` (default, no network) and
:class:`RazorpayClient` (test mode, Payment Links). :func:`build_client` picks
one from config. :func:`execute` is the intent-before-send path;
:func:`reconcile` adopts crashed / retriable sends from the provider on
startup. All state lives in the append-only ``execution_intents`` /
``execution_outcomes`` ledger.
"""

from app.execution.client import (
    ActionClient,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    TERMINAL_STATUSES,
    compute_idem_key,
    redact,
    request_fingerprint,
)
from app.execution.config import build_client
from app.execution.executor import execute
from app.execution.fake_client import FakeActionClient
from app.execution.ledger import (
    all_intents,
    append_outcome,
    get_intent,
    init_execution_ledger,
    insert_intent,
    latest_outcome,
    terminal_outcome,
)

# NB: app.execution.reconcile and app.execution.run_once are CLI entrypoints
# (python -m ...); they are intentionally NOT re-exported here so that `-m`
# does not trip runpy's "already in sys.modules" warning. Import them directly.

__all__ = [
    "ActionClient",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionStatus",
    "TERMINAL_STATUSES",
    "compute_idem_key",
    "request_fingerprint",
    "redact",
    "build_client",
    "execute",
    "FakeActionClient",
    "init_execution_ledger",
    "insert_intent",
    "append_outcome",
    "get_intent",
    "latest_outcome",
    "terminal_outcome",
    "all_intents",
]
