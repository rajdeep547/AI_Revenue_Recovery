import json
import math
import pathlib
import subprocess
import sys

import datagen

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_exactly_2000_events():
    events_doc, _ = datagen.generate_dataset()
    assert events_doc["meta"]["n_events"] == 2000
    assert len(events_doc["events"]) == 2000


def test_reruns_are_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    datagen.write_dataset(str(a), *datagen.generate_dataset(seed=424242))
    datagen.write_dataset(str(b), *datagen.generate_dataset(seed=424242))
    for name in ("events.json", "ground_truth.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_default_seed_run_is_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    datagen.write_dataset(str(a), *datagen.generate_dataset())
    datagen.write_dataset(str(b), *datagen.generate_dataset())
    assert (a / "events.json").read_bytes() == (b / "events.json").read_bytes()
    assert (a / "ground_truth.json").read_bytes() == (b / "ground_truth.json").read_bytes()


def test_different_seed_produces_different_output():
    _, gt1 = datagen.generate_dataset(seed=1)
    _, gt2 = datagen.generate_dataset(seed=2)
    assert gt1["customers"] != gt2["customers"]


def test_every_customer_has_both_probabilities_and_an_arm():
    _, gt = datagen.generate_dataset()
    custs = gt["customers"]
    assert custs
    for c in custs.values():
        assert 0.0 <= c["p_would_pay_anyway"] <= 1.0
        assert 0.0 <= c["p_pay_if_nudged"] <= 1.0
        assert c["arm"] in ("control", "treated")
        assert isinstance(c["realized"], bool)
        assert c["error_reason"] in datagen._ERROR_REASONS


def test_nudged_is_never_below_base_and_lift_is_the_gap():
    _, gt = datagen.generate_dataset()
    for c in gt["customers"].values():
        assert c["p_pay_if_nudged"] >= c["p_would_pay_anyway"]
        assert c["lift"] >= 0.0
        assert abs((c["p_would_pay_anyway"] + c["lift"]) - c["p_pay_if_nudged"]) < 1e-9


def test_population_mean_lift_is_in_target_band():
    _, gt = datagen.generate_dataset()
    lifts = [c["lift"] for c in gt["customers"].values()]
    mean_lift = sum(lifts) / len(lifts)
    assert 0.08 <= mean_lift <= 0.12


def test_baseline_probability_is_not_moved():
    _, gt = datagen.generate_dataset()
    base = [c["p_would_pay_anyway"] for c in gt["customers"].values()]
    assert abs(sum(base) / len(base) - 0.29) < 0.02


def test_base_and_lift_are_negatively_correlated():
    _, gt = datagen.generate_dataset()
    custs = list(gt["customers"].values())
    base = [c["p_would_pay_anyway"] for c in custs]
    lift = [c["lift"] for c in custs]
    assert datagen._pearson(base, lift) < -0.3


def test_control_arm_share_is_near_30_percent():
    _, gt = datagen.generate_dataset()
    custs = list(gt["customers"].values())
    share = sum(1 for c in custs if c["arm"] == "control") / len(custs)
    assert abs(share - 0.30) < 0.035


def test_control_realizes_against_base_treated_against_nudged():
    """Sanity on the arm-dependent realization: treated recover at a clearly
    higher rate than control, since lift is positive on average."""
    _, gt = datagen.generate_dataset()
    custs = list(gt["customers"].values())
    ctrl = [c for c in custs if c["arm"] == "control"]
    trt = [c for c in custs if c["arm"] == "treated"]
    ctrl_rate = sum(c["realized"] for c in ctrl) / len(ctrl)
    trt_rate = sum(c["realized"] for c in trt) / len(trt)
    assert trt_rate > ctrl_rate


def test_ground_truth_covers_every_payment_in_the_event_stream():
    events_doc, gt = datagen.generate_dataset()
    pay_ids_in_events = {
        e["payload"]["payload"]["payment"]["entity"]["id"] for e in events_doc["events"]
    }
    pay_ids_in_gt = {c["payment_id"] for c in gt["customers"].values()}
    assert pay_ids_in_events <= pay_ids_in_gt


def test_every_failed_event_carries_an_error_reason():
    events_doc, _ = datagen.generate_dataset()
    failed = [
        e for e in events_doc["events"] if e["payload"]["event"] == "payment.failed"
    ]
    assert failed
    for e in failed:
        entity = e["payload"]["payload"]["payment"]["entity"]
        assert entity["error_reason"] in datagen._ERROR_REASONS


def test_events_file_leaks_no_arm_probability_or_lift():
    events_doc, _ = datagen.generate_dataset()
    blob = json.dumps(events_doc)
    for token in (
        '"arm"',
        '"p_would_pay_anyway"',
        '"p_pay_if_nudged"',
        '"lift"',
        '"realized"',
        '"would_pay_anyway"',
        "control",
        "treated",
    ):
        assert token not in blob


def test_log_amount_is_approximately_normal_not_uniform():
    """The real claim is *lognormal*, so test log(amount) against a fitted
    normal with a KS distance, not merely 'not uniform'."""
    _, gt = datagen.generate_dataset()
    amounts = [c["amount"] for c in gt["customers"].values()]
    ks = datagen._ks_stat_normal([math.log(a) for a in amounts])
    assert ks < 0.08

    # and it is still visibly right-skewed in rupee space
    amounts.sort()
    n = len(amounts)
    assert sum(amounts) / n > amounts[n // 2] * 1.15


def test_no_pipeline_module_references_ground_truth():
    """The grep from the Slice 3 break step, as an assertion: zero hits for
    `ground_truth` anywhere under app/."""
    hits = [
        str(py.relative_to(REPO))
        for py in (REPO / "app").rglob("*.py")
        if "ground_truth" in py.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_cli_writes_both_files(tmp_path):
    out = tmp_path / "data"
    subprocess.run(
        [sys.executable, str(REPO / "datagen.py"), "--seed", "7", "--out-dir", str(out)],
        check=True,
        cwd=str(REPO),
    )
    assert (out / "events.json").is_file()
    gt = json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))
    assert gt["meta"]["seed"] == 7
    assert "HELD OUT" in gt["meta"]["note"]


def test_hist_flag_writes_nothing(tmp_path):
    out = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, str(REPO / "datagen.py"), "--hist", "--out-dir", str(out)],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert not out.exists()
    assert "Rs" in proc.stdout
