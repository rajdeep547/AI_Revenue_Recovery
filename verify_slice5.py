# tests/test_slice6_diagnosis.py
import json
from pathlib import Path

import pytest

from tools.label_harness import (
    BLIND, MANIFEST, MAP_FILE, UNKNOWN, load_classifier, load_json,
)

MIN_PER_CLASS = 15


@pytest.fixture(scope="module")
def blind():
    if not BLIND.exists():
        pytest.skip("run: python tools/label_harness.py sample")
    return load_json(BLIND)


@pytest.fixture(scope="module")
def manifest():
    if not MANIFEST.exists():
        pytest.skip("run: python tools/label_harness.py sample")
    return load_json(MANIFEST)


def test_blind_sample_carries_no_truth(blind):
    leaked = {"root_cause", "true_root_cause", "true_label", "cause"}
    for row in blind:
        assert row["label"] in (None, "") or isinstance(row["label"], str)
        assert not leaked & set(json.dumps(row["payload"]).split('"'))


def test_every_class_meets_floor(manifest):
    counts = {}
    for cls in manifest["truth"].values():
        counts[cls] = counts.get(cls, 0) + 1
    thin = {c: n for c, n in counts.items() if n < MIN_PER_CLASS}
    assert not thin, f"classes below n={MIN_PER_CLASS}: {thin} — worst-class claim is noise"


def test_sample_is_reproducible(blind, manifest):
    import hashlib
    digest = hashlib.sha256(",".join(r["id"] for r in blind).encode()).hexdigest()[:16]
    assert digest == manifest["sample_digest"]


def test_all_events_labeled(blind):
    missing = [r["id"] for r in blind if not r.get("label")]
    assert not missing, f"{len(missing)} events still unlabeled"


def test_abstentions_are_counted_not_dropped(blind):
    abstained = [r for r in blind if r.get("label") == UNKNOWN]
    for row in abstained:
        assert row.get("note"), f"{row['id']}: abstention needs a note saying why"


def test_error_code_map_is_total(blind):
    if not MAP_FILE.exists():
        pytest.skip("build rules/error_code_map.json first")
    classify = load_classifier()
    unmapped = [r["id"] for r in blind if classify(r["payload"]) == UNKNOWN]
    assert len(unmapped) / len(blind) < 0.10, (
        f"{len(unmapped)}/{len(blind)} events fall through the map"
    )


def test_app_never_reads_labels():
    app = Path(__file__).resolve().parents[1] / "app"
    if not app.exists():
        pytest.skip("no app/ dir")
    for py in app.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for banned in ("ground_truth", "blind_sample", "_truth_manifest", "label_harness"):
            assert banned not in src, f"{py.relative_to(app)} references {banned}"