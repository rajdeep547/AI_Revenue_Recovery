"""Slice 6 - blind-label harness gate.

These tests guard the *integrity* of the blind-labelling design, not the
accuracy of any rule:

  - the blind sample must not carry the generator's own answer
    (``error_reason``) into the payload a human reads;
  - every true class must clear the ``--min-per-class`` floor, or the
    matrix-B "worst class" is small-sample noise rather than a finding;
  - the sample file and the held-out answer key must agree (digest);
  - labelling must actually be finished before a score means anything;
  - an abstention is a finding to be *counted* (with a reason), never a
    row to wave through;
  - the error-code map must not dump most events into ``default``;
  - nothing under ``app/`` may even mention the measurement layer.

The whole module SKIPS (never fails) until ``labels/blind_sample.json``
exists, so the suite stays green before the sample is drawn.
"""
from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path

import pytest

from tools.label_harness import (
    BLIND_PATH,
    EMAP_PATH,
    MANIFEST_PATH,
    classify,
    load_classifier,
    load_events,
)

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"

FORBIDDEN_IN_APP = ("ground_truth", "blind_sample", "_truth_manifest", "label_harness")

pytestmark = pytest.mark.skipif(
    not Path(BLIND_PATH).exists(),
    reason="labels/blind_sample.json not drawn yet -- run `label_harness.py sample`",
)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def blind():
    return _load(BLIND_PATH)


@pytest.fixture(scope="module")
def manifest():
    return _load(MANIFEST_PATH)


def _all_keys(node):
    """Every dict key anywhere inside a nested structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _all_keys(item)


# ---------------------------------------------------------------- integrity


def test_blind_payloads_do_not_leak_the_root_cause_field(blind, manifest):
    cause_leaf = manifest["cause_field"].split(".")[-1]
    offenders = [
        row["id"] for row in blind if cause_leaf in set(_all_keys(row["payload"]))
    ]
    assert not offenders, (
        f"{len(offenders)} blind payload(s) still carry a '{cause_leaf}' key "
        f"(e.g. {offenders[:5]}). The sample would then be labelled from the "
        f"generator's own encoding, not from the payload signal -- the one "
        f"thing this design exists to prevent."
    )


def test_every_true_class_meets_the_min_per_class_floor(blind, manifest):
    floor = manifest["min_per_class"]
    id_to_cause = manifest["id_to_true_cause"]

    unmapped = [row["id"] for row in blind if row["id"] not in id_to_cause]
    assert not unmapped, f"manifest is missing {len(unmapped)} sampled id(s): {unmapped[:5]}"

    counts = collections.Counter(id_to_cause[row["id"]] for row in blind)
    thin = sorted(cls for cls, k in counts.items() if k < floor)
    assert not thin, (
        f"class(es) below the {floor}-row floor: {', '.join(thin)} "
        f"(counts: {dict(counts)}). With a thin class the matrix-B 'worst class' "
        f"is small-sample noise, not a finding -- redraw with a larger --n or a "
        f"lower --min-per-class."
    )


def test_recomputed_sample_digest_matches_the_manifest(blind, manifest):
    ids_in_file_order = [row["id"] for row in blind]
    recomputed = hashlib.sha256(
        ",".join(ids_in_file_order).encode("utf-8")
    ).hexdigest()[:16]
    assert recomputed == manifest["digest"], (
        f"digest over the blind-sample id order ({recomputed}) != manifest "
        f"({manifest['digest']}): the sample file and the answer key disagree "
        f"about which rows were drawn."
    )


def test_no_row_is_left_unlabeled(blind):
    unlabeled = [row["id"] for row in blind if row["label"] in (None, "")]
    assert not unlabeled, (
        f"{len(unlabeled)} of {len(blind)} rows still have a null/empty label "
        f"(e.g. {unlabeled[:5]}). Finish the blind labelling before scoring; a "
        f"partial score is not a score."
    )


def test_every_unknown_abstention_carries_a_non_empty_note(blind):
    abstentions = [row for row in blind if row["label"] == "unknown"]
    missing = [row["id"] for row in abstentions if not str(row.get("note", "")).strip()]
    assert not missing, (
        f"{len(missing)} of {len(abstentions)} 'unknown' abstention(s) have an "
        f"empty note (e.g. {missing[:5]}). An abstention is a finding to be "
        f"counted, not dropped -- record why the payload could not be called."
    )


@pytest.mark.skipif(
    not Path(EMAP_PATH).exists(),
    reason="rules/error_code_map.json not built yet",
)
def test_fewer_than_10pct_of_events_fall_through_to_default():
    events = load_events()
    cfg = load_classifier()
    default = cfg["default"]
    fell_through = sum(1 for event in events if classify(event, cfg) == default)
    frac = fell_through / len(events)
    assert frac < 0.10, (
        f"{fell_through}/{len(events)} ({frac:.1%}) events fall through the "
        f"error-code map to '{default}'. The map is too sparse to diagnose "
        f"from -- add signal paths or entries."
    )


# ---------------------------------------------------------------- app/ walls


def test_no_app_module_references_the_measurement_layer():
    hits = []
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_APP:
            if needle in text:
                hits.append(f"{path.relative_to(ROOT)} mentions '{needle}'")
    assert not hits, (
        "app/ must not know the Slice 6 measurement layer exists: "
        + "; ".join(hits)
    )
