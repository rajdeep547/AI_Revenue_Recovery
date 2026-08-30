"""Slice 7 - LLM diagnosis tail. An advisory classifier for the one
failure cell the rules layer (``app/diagnosis.py``) cannot separate.

**Why the accept path is off** (``TAIL_ACT_ENABLED = False``): in
``data/events.json`` every failed payment's ``error_description`` is a
fixed 1:1 transcription of its ``error_reason`` - the generator's own
answer key, six causes to six frozen strings with no variation. Any
classifier that reads the description (the model does, through the
prompt) therefore scores a fake 1.00 by transcription. Until the tail can
be validated on a corpus with real description variation, an
LLM-sourced diagnosis is never allowed to drive a money action: it is
recorded and queued for a human. The rules path is unaffected and still
acts. See DECISIONS.md Slice 7.

**Trigger** - the model is called ONLY when ``is_ambiguous(entity)`` is
True: the whole ``BAD_REQUEST_ERROR`` cell, a four-way collision of
invalid_card / expired_card / insufficient_funds / otp_timeout that
``error_code`` alone cannot split (rules score 0.00 there). ``method`` is
not consulted - in this generator it carries no signal about root cause
(e.g. pay_000001 is UPI with "card number or CVV is invalid"), so
gating on ``method == "card"`` would only drop ~812 equally-ambiguous
events. Every other event returns its rules label unchanged, with
``attempts = 0`` and the transport untouched.

**Fail-closed contract** - a real root cause is *proposed* only by a
clean, in-enum, whole-response JSON answer within the deadline. A timeout
or an ``OSError`` is retried up to the cap; a parse failure or an
out-of-enum answer is terminal (the prompt is identical - a retry only
triples the cost). Anything that is not a clean answer resolves to
``unknown`` + the human queue.

Pipeline code: imports nothing from the offline audit harness, never
reads the held-out labels.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading

from app.diagnosis import ROOT_CAUSES, UNKNOWN

# --- configuration ---------------------------------------------------------
# High-volume classification tail -> Haiku by default; override per env.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_TIMEOUT_S = float(os.environ.get("LLM_TAIL_TIMEOUT_S", "8.0"))
DEFAULT_MAX_RETRIES = int(os.environ.get("LLM_TAIL_MAX_RETRIES", "2"))

# Master switch for the accept path. While False, no LLM-sourced Diagnosis
# can act, regardless of model output. See DECISIONS.md Slice 7.
TAIL_ACT_ENABLED = False

ALLOWED_OUTPUT = frozenset(ROOT_CAUSES)              # six real causes + "unknown"
REAL_CAUSES = frozenset(c for c in ROOT_CAUSES if c != UNKNOWN)

ROUTE_ACT = "act"            # eligible for an automated money action (a paid nudge)
ROUTE_HUMAN = "human_queue"  # parked for a person; no money is spent

SRC_RULES = "rules"
SRC_LLM = "llm"              # a clean model answer, including an honest abstention
SRC_LLM_FAILED = "llm_failed"

_SYSTEM = (
    "You triage failed card/UPI payments. Given one payment's diagnostic "
    "fields, name the single most likely root cause.\n"
    'Reply with ONE line of JSON and nothing else: '
    '{"root_cause": "<value>", "rationale": "<= 12 words"}\n'
    f"<value> MUST be exactly one of: {', '.join(ROOT_CAUSES)}.\n"
    'Answer "unknown" when the fields do not point clearly to one cause. '
    "Never guess, never invent a category."
)

# Diagnostic fields shown to the model, in order.
_DIAG_FIELDS = ("error_code", "error_description", "method", "amount", "status")


# --- result type ---------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Diagnosis:
    root_cause: str          # always in ROOT_CAUSES; "unknown" on any failure/abstention
    source: str              # SRC_RULES | SRC_LLM | SRC_LLM_FAILED
    route: str               # ROUTE_ACT | ROUTE_HUMAN
    detail: str              # human-readable why (esp. on failure)
    attempts: int = 0        # model calls actually made

    @property
    def is_money_eligible(self) -> bool:
        return self.route == ROUTE_ACT and self.root_cause in REAL_CAUSES


class TailTimeout(Exception):
    """A single model call did not answer within its deadline."""


# --- ambiguity gate -----------------------------------------------------
def is_ambiguous(entity: dict) -> bool:
    """True for the whole ``BAD_REQUEST_ERROR`` cell - a four-way collision
    of invalid_card / expired_card / insufficient_funds / otp_timeout that
    ``error_code`` cannot split.

    ``method`` is deliberately excluded: in this generator it is
    independent of root cause (it carries no signal), so a
    ``method == "card"`` gate would only exclude ~812 events that are just
    as ambiguous.

    Reads only ``error_code``. It must never read the per-event root-cause
    field or the free-text failure description - those are the generator's
    answer key and would leak a fake 1.00.
    """
    return entity.get("error_code") == "BAD_REQUEST_ERROR"


# --- entity extraction ------------------------------------------------
def _entity(event: dict) -> dict:
    """The payment entity for the canonical event shape, with a recursive
    fallback for anything else."""
    node = event
    for key in ("payload", "payload", "payment", "entity"):
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(key)
    if isinstance(node, dict) and any(f in node for f in _DIAG_FIELDS):
        return node
    return _find_entity(event) or {}


def _find_entity(node, _depth: int = 0):
    if _depth > 8 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, dict):
        if any(f in node for f in _DIAG_FIELDS):
            return node
        for value in node.values():
            hit = _find_entity(value, _depth + 1)
            if hit is not None:
                return hit
    else:
        for value in node:
            hit = _find_entity(value, _depth + 1)
            if hit is not None:
                return hit
    return None


def _build_prompt(event: dict) -> str:
    ent = _entity(event)
    body = "\n".join(f"{field}: {ent.get(field)!r}" for field in _DIAG_FIELDS)
    return "Failed payment fields:\n" + body


# --- strict output parsing --------------------------------------------
def _strip_json_fence(text: str) -> str:
    """If the whole trimmed string is a single ``` ... ``` block, return
    its inner content with an optional language tag removed. Otherwise
    return the trimmed string unchanged - prose around a fence is not
    stripped, so it will fail the whole-string JSON parse below."""
    t = text.strip()
    if len(t) >= 6 and t.startswith("```") and t.endswith("```"):
        t = t[3:-3].strip()
        newline = t.find("\n")
        if newline != -1 and t[:newline].strip().isalpha():
            t = t[newline + 1 :].strip()
        elif newline == -1 and t.isalpha():
            t = ""
    return t


def parse_output(raw) -> str | None:
    """Return an allowed enum string, or ``None``. Requires the ENTIRE
    response (after stripping a lone ```json fence) to be a JSON object
    whose ``root_cause`` is in the enum. JSON embedded in surrounding
    prose is rejected."""
    if not isinstance(raw, str):
        return None
    text = _strip_json_fence(raw)
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    cause = obj.get("root_cause")
    if isinstance(cause, str) and cause in ALLOWED_OUTPUT:
        return cause
    return None


# --- transport --------------------------------------------------------
def _call_once(transport, prompt: str, timeout_s: float) -> str:
    """Run ``transport(prompt)`` on a daemon worker and abandon it if it
    overruns ``timeout_s``. Re-raises whatever the transport raised."""
    box: dict = {}

    def _run():
        try:
            box["value"] = transport(prompt)
        except Exception as exc:  # noqa: BLE001 - handed back to the caller verbatim
            box["error"] = exc

    worker = threading.Thread(target=_run, name="llm-tail", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TailTimeout(f"no model response within {timeout_s:.2f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _default_transport(*, timeout_s: float):
    """A callable ``(prompt) -> raw_text`` that asks Claude. Imported lazily
    so this module loads with no SDK installed; the retry loop owns retries,
    so the client is pinned to ``max_retries=0``."""

    def _call(prompt: str) -> str:
        import anthropic  # lazy: only the real transport needs the SDK

        client = anthropic.Anthropic().with_options(timeout=timeout_s, max_retries=0)
        message = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        )

    return _call


# --- the tail -------------------------------------------------------
def _route_clean_answer(cause: str, attempts: int) -> Diagnosis:
    if cause == UNKNOWN:
        return Diagnosis(UNKNOWN, SRC_LLM, ROUTE_HUMAN, "model_abstained", attempts)
    if TAIL_ACT_ENABLED:
        return Diagnosis(cause, SRC_LLM, ROUTE_ACT, "llm_confident", attempts)
    return Diagnosis(
        cause, SRC_LLM, ROUTE_HUMAN,
        "llm_confident; accept path disabled (TAIL_ACT_ENABLED=False)", attempts,
    )


def diagnose_unknown(
    event: dict,
    *,
    transport,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Diagnosis:
    """Ask the model to classify one ambiguous event. Retries ``TailTimeout``
    and ``OSError`` up to the cap; a parse failure, an out-of-enum answer,
    or any other transport exception is terminal. Fails closed to
    ``unknown`` + human queue. A model that answers ``"unknown"`` is an
    honest abstention (source ``llm``), also queued."""
    prompt = _build_prompt(event)
    attempts = 0
    last = "no attempt made"

    for _ in range(max(1, max_retries + 1)):
        attempts += 1
        try:
            raw = _call_once(transport, prompt, timeout_s)
        except TailTimeout as exc:
            last = f"timeout: {exc}"
            continue  # transport failure -> retry
        except OSError as exc:
            last = f"transport_error: {type(exc).__name__}: {exc}"
            continue  # transport failure -> retry
        except Exception as exc:  # noqa: BLE001 - not a transport failure: terminal
            return Diagnosis(
                UNKNOWN, SRC_LLM_FAILED, ROUTE_HUMAN,
                f"transport_raised (terminal): {type(exc).__name__}: {exc}", attempts,
            )

        cause = parse_output(raw)
        if cause is None:
            preview = raw[:120] if isinstance(raw, str) else repr(raw)
            return Diagnosis(
                UNKNOWN, SRC_LLM_FAILED, ROUTE_HUMAN,
                f"bad_output (terminal, no retry): {preview!r}", attempts,
            )
        return _route_clean_answer(cause, attempts)

    return Diagnosis(
        UNKNOWN, SRC_LLM_FAILED, ROUTE_HUMAN,
        f"exhausted after {attempts} attempt(s): {last}", attempts,
    )


def diagnose(
    event: dict,
    rules_label: str,
    *,
    transport=None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Diagnosis:
    """Ambiguity gate. The model runs ONLY when ``is_ambiguous(entity)`` is
    True (the whole ``BAD_REQUEST_ERROR`` cell). Every other event returns
    its rules label unchanged, ``attempts=0``, transport untouched.

    Raises ``ValueError`` if ``rules_label`` is not one of ``ROOT_CAUSES`` -
    an unrecognised label is a bug upstream, not a reason to spend a model
    call.
    """
    if rules_label not in ROOT_CAUSES:
        raise ValueError(
            f"rules_label {rules_label!r} is not one of {sorted(ROOT_CAUSES)}"
        )

    entity = _entity(event)
    if not is_ambiguous(entity):
        route = ROUTE_ACT if rules_label in REAL_CAUSES else ROUTE_HUMAN
        return Diagnosis(rules_label, SRC_RULES, route, "not_ambiguous", 0)

    if transport is None:
        transport = _default_transport(timeout_s=timeout_s)
    return diagnose_unknown(
        event, transport=transport, timeout_s=timeout_s, max_retries=max_retries
    )


# --- routing --------------------------------------------------------
def apply(diagnosis: Diagnosis, *, spend, enqueue) -> str:
    """Act on a diagnosis. ``spend(root_cause)`` - the money action, a paid
    nudge - fires only for a money-eligible diagnosis.

    While ``TAIL_ACT_ENABLED`` is False, any ``Diagnosis`` whose source is
    ``llm`` or ``llm_failed`` is hard-gated to the human queue *before*
    ``is_money_eligible`` is consulted - that check is the second layer.
    The rules path (source ``rules``) is unaffected and may still act.
    """
    hard_gated = (
        diagnosis.source in (SRC_LLM, SRC_LLM_FAILED) and not TAIL_ACT_ENABLED
    )
    if not hard_gated and diagnosis.is_money_eligible:
        spend(diagnosis.root_cause)
        return ROUTE_ACT
    enqueue(diagnosis)
    return ROUTE_HUMAN
