"""Slice 6 · Rules diagnosis: build (app/diagnosis.py) + break (confusion
matrix against 100 hand-labeled real events).

Hand labels come from error_description text (eval.diagnosis_audit's
HAND_LABEL_BY_DESCRIPTION), never from error_reason or ground_truth.json --
an independent check on a classifier that only sees error_code.
"""
from __future__ import annotations

import pytest

import datagen
from app.diagnosis import CODE_TO_ROOT_CAUSE, UNKNOWN, diagnose, error_code_of
from eval.diagnosis_audit import confusion_matrix, hand_label, worst_class
from eval.measurement import load_population

SEED = datagen.DEFAULT_SEED
N_EVENTS = 2000
SAMPLE_SIZE = 100


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    events_doc, ground_truth_doc = datagen.generate_dataset(SEED, N_EVENTS)
    out_dir = tmp_path_factory.mktemp("slice6")
    datagen.write_dataset(str(out_dir), events_doc, ground_truth_doc)
    rows, _ = load_population(out_dir / "events.json")
    return rows[:SAMPLE_SIZE]


# ---------------------------------------------------------------------- build


def test_error_code_of_reads_the_raw_payload_not_the_normalized_reason():
    # normalize()'s `reason` field is error_reason -- diagnosis must ignore
    # it and re-derive from raw error_code instead, or this whole exercise
    # is measuring nothing.
    row = {
        "reason": "otp_timeout",  # would give the game away if read
        "raw": {"payload": {"payment": {"entity": {
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "otp_timeout",
        }}}},
    }
    assert error_code_of(row) == "BAD_REQUEST_ERROR"
    assert diagnose(row) == "insufficient_funds"  # the code-only majority default


def test_diagnose_unknown_code_maps_to_unknown():
    row = {"raw": {"payload": {"payment": {"entity": {"error_code": "SOMETHING_NEW"}}}}}
    assert diagnose(row) == UNKNOWN


def test_diagnose_missing_raw_maps_to_unknown():
    assert diagnose({}) == UNKNOWN


def test_code_to_root_cause_is_exactly_the_two_known_codes():
    assert set(CODE_TO_ROOT_CAUSE) == {"GATEWAY_ERROR", "BAD_REQUEST_ERROR"}


# ---------------------------------------------------------------------- break


def test_confusion_matrix_over_100_hand_labeled_events(sample):
    matrix = confusion_matrix(sample)

    n_labeled = sum(sum(preds.values()) for preds in matrix.values())
    assert n_labeled == SAMPLE_SIZE
    assert UNKNOWN not in matrix, "every one of the 100 sampled rows must carry a known error_description"

    # Both classes sharing GATEWAY_ERROR / BAD_REQUEST_ERROR with the chosen
    # majority default recover perfectly; every other class sharing that
    # code is always wrong. This is a direct, provable consequence of a
    # code-only rule, not a coincidence of this particular sample.
    assert matrix["bank_downtime"]["bank_downtime"] == sum(matrix["bank_downtime"].values())
    assert matrix["insufficient_funds"]["insufficient_funds"] == sum(matrix["insufficient_funds"].values())
    for victim in ("gateway_timeout", "expired_card", "invalid_card", "otp_timeout"):
        preds = matrix[victim]
        assert preds.get(victim, 0) == 0, f"{victim} should never be predicted correctly by a code-only rule"


def test_worst_class_is_otp_timeout_absorbed_into_insufficient_funds(sample):
    """The Pass gate: I can name the worst class and its failure mode
    without looking. otp_timeout is BAD_REQUEST_ERROR's second-largest true
    class (weight 0.22) behind insufficient_funds (0.28) -- the class the
    code defaults to -- so among the four causes that share
    BAD_REQUEST_ERROR and are therefore always misclassified, otp_timeout
    has the most instances in any real sample. Failure mode: error_code
    alone can't distinguish BAD_REQUEST_ERROR's four sub-causes, and the
    majority-vote default (insufficient_funds) silently absorbs every
    otp_timeout event.
    """
    matrix = confusion_matrix(sample)
    label, wrong, total = worst_class(matrix)

    assert label == "otp_timeout"
    assert wrong == total, "otp_timeout's error rate is 100%, same as the other three victims"
    assert matrix["otp_timeout"]["insufficient_funds"] == total, "every miss lands specifically on insufficient_funds"

    # otp_timeout has more instances than any other always-wrong class in
    # this sample -- that volume, not its (tied) error rate, is what makes
    # it "the worst" rather than merely "also wrong".
    other_victims = {"gateway_timeout", "expired_card", "invalid_card"}
    for other in other_victims:
        assert sum(matrix[other].values()) < total


def test_hand_label_never_reads_error_reason_field():
    # A row whose raw error_description is unrecognized must hand-label as
    # unknown even if error_reason is sitting right there in the payload --
    # proof the hand-labeler is reading description text, not the answer key.
    row = {"raw": {"payload": {"payment": {"entity": {
        "error_reason": "otp_timeout",
        "error_description": "some new gateway said something new",
    }}}}}
    assert hand_label(row) == UNKNOWN
