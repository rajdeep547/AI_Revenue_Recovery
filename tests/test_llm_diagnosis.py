"""Slice 7 - LLM diagnosis tail: build + break.

Build:
  * the model runs ONLY for the ambiguous BAD_REQUEST_ERROR cell;
    every other event returns its rules label, transport untouched
  * a strict, in-enum, whole-response JSON answer is parsed
  * ``TAIL_ACT_ENABLED`` False hard-gates every LLM-sourced diagnosis to
    the human queue, clean answers included
  * timeout / OSError retry up to the cap; a parse failure or an
    out-of-enum answer is terminal (one call)
  * an unrecognised rules label raises ``ValueError``
  * ``is_ambiguous`` reads only error_code

Break - four adversarial transports, each must fail closed:
  garbage text, a forced timeout, an out-of-enum cause, the network pulled.

Pass - every break path ends at ``unknown`` + the human queue, and none of
them reaches a money action.
"""

from __future__ import annotations

import inspect
import time

import pytest

from app import llm_diagnosis as tail
from app.diagnosis import ROOT_CAUSES, UNKNOWN
from app.llm_diagnosis import ROUTE_ACT, ROUTE_HUMAN, Diagnosis

# BAD_REQUEST_ERROR + card -> the ambiguous cell -> reaches the model.
AMBIGUOUS_EVENT = {
    "event_id": "evt_00042",
    "created_at": 1735689654,
    "payload": {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_000042",
            "amount": 47300,
            "method": "card",
            "status": "failed",
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "3DS/OTP was not completed in time",
        }}},
    },
}

# GATEWAY_ERROR + upi -> not ambiguous -> rules label, transport untouched.
NON_AMBIGUOUS_EVENT = {
    "event_id": "evt_00007",
    "created_at": 1735690000,
    "payload": {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_000007",
            "amount": 74900,
            "method": "upi",
            "status": "failed",
            "error_code": "GATEWAY_ERROR",
            "error_description": "issuer or UPI bank temporarily unavailable",
        }}},
    },
}


class Spy:
    """Records every call; a stand-in for the money action."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


class _Landmine(dict):
    """A dict that raises if the answer-key fields are ever read."""

    _FORBIDDEN = frozenset({"error_reason", "error_description"})

    def __getitem__(self, key):
        if key in self._FORBIDDEN:
            raise AssertionError(f"is_ambiguous read forbidden field {key!r}")
        return super().__getitem__(key)

    def get(self, key, *default):
        if key in self._FORBIDDEN:
            raise AssertionError(f"is_ambiguous read forbidden field {key!r}")
        return super().get(key, *default)


def _route(diag):
    spend, queue = Spy(), []
    taken = tail.apply(diag, spend=spend, enqueue=queue.append)
    return taken, spend, queue


def _boom(_prompt):
    raise AssertionError("transport was called when it should not have been")


# --------------------------------------------------------- the trigger


def test_a_non_ambiguous_event_never_reaches_the_transport():
    diag = tail.diagnose(NON_AMBIGUOUS_EVENT, "bank_downtime", transport=_boom)

    assert diag.source == "rules"
    assert diag.root_cause == "bank_downtime"
    assert diag.route == ROUTE_ACT
    assert diag.attempts == 0


@pytest.mark.parametrize(
    "entity",
    [
        {"error_code": "GATEWAY_ERROR", "method": "upi"},
        {"error_code": "GATEWAY_ERROR", "method": "card"},
        {"error_code": "GATEWAY_ERROR", "method": "netbanking"},
        {"error_code": "GATEWAY_ERROR"},
        {},
    ],
)
def test_is_ambiguous_is_false_outside_the_bad_request_cell(entity):
    assert tail.is_ambiguous(entity) is False


@pytest.mark.parametrize(
    "entity",
    [
        {"error_code": "BAD_REQUEST_ERROR", "method": "card"},
        {"error_code": "BAD_REQUEST_ERROR", "method": "upi"},
        {"error_code": "BAD_REQUEST_ERROR", "method": "wallet"},
        {"error_code": "BAD_REQUEST_ERROR", "method": "netbanking"},
        {"error_code": "BAD_REQUEST_ERROR"},
    ],
)
def test_is_ambiguous_is_true_across_the_whole_bad_request_cell(entity):
    assert tail.is_ambiguous(entity) is True


def test_an_ambiguous_event_reaches_the_transport():
    calls = []

    def ok(_prompt):
        calls.append(1)
        return '{"root_cause": "invalid_card"}'

    # rules_label is a real cause, but the cell is ambiguous -> model runs.
    diag = tail.diagnose(AMBIGUOUS_EVENT, "invalid_card", transport=ok)

    assert calls == [1]
    assert tail.is_ambiguous(tail._entity(AMBIGUOUS_EVENT)) is True
    assert diag.source == "llm"


def test_is_ambiguous_never_reads_error_reason_or_error_description():
    entity = _Landmine({
        "error_code": "BAD_REQUEST_ERROR",
        "method": "card",
        "error_reason": "invalid_card",
        "error_description": "card number or CVV is invalid",
    })

    assert tail.is_ambiguous(entity) is True  # no AssertionError from the landmine

    # And the two fields are not even named as string constants in the body.
    consts = [
        c for c in tail.is_ambiguous.__code__.co_consts
        if isinstance(c, str) and c != tail.is_ambiguous.__doc__
    ]
    assert not any(
        "error_reason" in c or "error_description" in c for c in consts
    ), inspect.getsource(tail.is_ambiguous)


# --------------------------------------------------------- the accept gate


def test_a_clean_answer_is_gated_to_human_queue_while_tail_act_disabled():
    assert tail.TAIL_ACT_ENABLED is False

    calls = []

    def ok(_prompt):
        calls.append(1)
        return '{"root_cause": "expired_card", "rationale": "card has expired"}'

    diag = tail.diagnose(AMBIGUOUS_EVENT, "invalid_card", transport=ok, max_retries=2)

    assert diag.source == "llm"
    assert diag.root_cause == "expired_card"      # still recorded for the human
    assert diag.route == ROUTE_HUMAN
    assert diag.is_money_eligible is False
    assert len(calls) == 1, "a clean answer must not be retried"

    taken, spend, queue = _route(diag)
    assert taken == ROUTE_HUMAN
    assert spend.calls == []
    assert len(queue) == 1


def test_a_lone_json_fence_is_still_a_clean_answer():
    raw = '```json\n{"root_cause": "insufficient_funds"}\n```'
    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, transport=lambda _p: raw, max_retries=1)

    assert diag.source == "llm"                   # parsed fine
    assert diag.root_cause == "insufficient_funds"
    assert diag.route == ROUTE_HUMAN              # but still gated


def test_the_model_answering_unknown_is_an_abstention_not_a_failure():
    diag = tail.diagnose(
        AMBIGUOUS_EVENT, UNKNOWN,
        transport=lambda _p: '{"root_cause": "unknown", "rationale": "fields ambiguous"}',
        max_retries=2,
    )

    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN
    assert diag.source == "llm"  # honest abstention, distinct from llm_failed
    assert diag.attempts == 1, "an abstention is a clean answer - do not retry it"


def test_the_rules_path_can_still_act_while_the_tail_is_gated():
    diag = tail.diagnose(NON_AMBIGUOUS_EVENT, "bank_downtime", transport=_boom)
    taken, spend, queue = _route(diag)

    assert taken == ROUTE_ACT
    assert spend.calls == [("bank_downtime",)]
    assert queue == []


# --------------------------------------------------------- the parser


def test_prose_wrapped_json_is_rejected():
    raw = 'not sure, maybe {"root_cause": "expired_card"}'
    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, transport=lambda _p: raw)

    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN
    assert diag.source == "llm_failed"


def test_a_parse_failure_is_terminal_one_transport_call():
    seen = []

    def garbage(_prompt):
        seen.append(1)
        return "not json, just a hunch about the issuing bank"

    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, transport=garbage, max_retries=3)

    assert len(seen) == 1, "a parse failure must not be retried"
    assert diag.attempts == 1
    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN
    assert diag.source == "llm_failed"


def test_an_out_of_enum_answer_is_terminal_one_transport_call():
    seen = []

    def invented(_prompt):
        seen.append(1)
        return '{"root_cause": "mercury_retrograde"}'

    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, transport=invented, max_retries=3)

    assert len(seen) == 1, "an out-of-enum answer must not be retried"
    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN


# --------------------------------------------------------- retry / timeout


def test_a_timeout_retries_up_to_the_cap():
    seen = []

    def slow(_prompt):
        seen.append(1)
        time.sleep(30)

    diag = tail.diagnose(
        AMBIGUOUS_EVENT, UNKNOWN, transport=slow, timeout_s=0.05, max_retries=2
    )

    assert len(seen) == 3, "1 initial attempt + max_retries(2) timeouts"
    assert diag.attempts == 3
    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN
    assert "timeout" in diag.detail


def test_an_oserror_retries_up_to_the_cap():
    seen = []

    def flaky(_prompt):
        seen.append(1)
        raise ConnectionError("connection refused")

    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, transport=flaky, max_retries=2)

    assert len(seen) == 3
    assert diag.attempts == 3
    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN


def test_a_hung_transport_is_cut_off_by_the_timeout_not_waited_on():
    def hang(_prompt):
        time.sleep(30)

    started = time.monotonic()
    diag = tail.diagnose(
        AMBIGUOUS_EVENT, UNKNOWN, transport=hang, timeout_s=0.05, max_retries=1
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5, "returned on its own deadline, did not wait for the transport"
    assert diag.root_cause == UNKNOWN
    assert diag.route == ROUTE_HUMAN
    assert "timeout" in diag.detail


# --------------------------------------------------------- guard rails


def test_an_unrecognised_rules_label_raises_value_error():
    with pytest.raises(ValueError):
        tail.diagnose(AMBIGUOUS_EVENT, "something_new", transport=_boom)

    with pytest.raises(ValueError):
        tail.diagnose(NON_AMBIGUOUS_EVENT, "", transport=_boom)


# --------------------------------------------------------- break


def _garbage_text(_prompt):
    return "hmm, probably the customer's bank was down? honestly not sure"


def _forced_timeout(_prompt):
    time.sleep(30)


def _invented_cause(_prompt):
    return '{"root_cause": "mercury_retrograde", "rationale": "the stars"}'


def _network_pulled(_prompt):
    raise OSError("network is unreachable")


BREAK_CASES = {
    "garbage_text": dict(transport=_garbage_text, timeout_s=8.0, max_retries=2),
    "forced_timeout": dict(transport=_forced_timeout, timeout_s=0.05, max_retries=1),
    "out_of_enum_cause": dict(transport=_invented_cause, timeout_s=8.0, max_retries=2),
    "network_pulled": dict(transport=_network_pulled, timeout_s=8.0, max_retries=2),
}


@pytest.mark.parametrize("name", list(BREAK_CASES))
def test_break_path_fails_closed_to_unknown_and_the_human_queue(name):
    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, **BREAK_CASES[name])

    assert diag.root_cause == UNKNOWN, name
    assert diag.root_cause in ROOT_CAUSES, name
    assert diag.route == ROUTE_HUMAN, name
    assert diag.source == "llm_failed", name
    assert diag.is_money_eligible is False, name


@pytest.mark.parametrize("name", list(BREAK_CASES))
def test_break_path_never_reaches_a_money_action(name):
    diag = tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, **BREAK_CASES[name])

    taken, spend, queue = _route(diag)

    assert spend.calls == [], f"{name} triggered a money action"
    assert taken == ROUTE_HUMAN, name
    assert len(queue) == 1, f"{name} was not queued for a human"


def test_break_paths_return_quickly():
    started = time.monotonic()
    for name in BREAK_CASES:
        tail.diagnose(AMBIGUOUS_EVENT, UNKNOWN, **BREAK_CASES[name])
    assert time.monotonic() - started < 10


def test_apply_will_not_spend_on_a_non_real_cause_even_if_mislabelled_act():
    # Defence in depth: a bug elsewhere hands apply() an impossible Diagnosis.
    bogus = Diagnosis(
        root_cause=UNKNOWN, source="llm", route=ROUTE_ACT, detail="bug", attempts=1
    )
    taken, spend, queue = _route(bogus)

    assert spend.calls == []
    assert taken == ROUTE_HUMAN
    assert len(queue) == 1


def test_apply_hard_gates_an_llm_sourced_real_cause_while_disabled():
    # Even a Diagnosis that slipped through with route=act and a real cause
    # cannot spend while the tail is gated, because source is "llm".
    slipped = Diagnosis(
        root_cause="invalid_card", source="llm", route=ROUTE_ACT,
        detail="hand-built", attempts=1,
    )
    taken, spend, queue = _route(slipped)

    assert spend.calls == []
    assert taken == ROUTE_HUMAN
    assert len(queue) == 1
