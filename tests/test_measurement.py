"""Slice 5 · Break phase for eval/measurement.py.

The measurement layer is the trust anchor: everything built after it is only
as credible as these tests. Uses the real datagen -> card_failure adapter ->
Ingestor path (never raw JSON) and the real eval.environment.Environment.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import datagen
from app.ingest import Ingestor
from eval.measurement import (
    DuplicateCustomerRows,
    assign_arm,
    do_nothing_policy,
    load_population,
    recover_everything_policy,
    run_policy,
)

REPO = Path(__file__).resolve().parent.parent
SEED = datagen.DEFAULT_SEED
N_EVENTS = 2000


@pytest.fixture(scope="module")
def dataset():
    events_doc, ground_truth_doc = datagen.generate_dataset(SEED, N_EVENTS)
    return events_doc, ground_truth_doc


@pytest.fixture(scope="module")
def data_files(tmp_path_factory, dataset):
    events_doc, ground_truth_doc = dataset
    out_dir = tmp_path_factory.mktemp("slice5")
    datagen.write_dataset(str(out_dir), events_doc, ground_truth_doc)
    return out_dir / "events.json", out_dir / "ground_truth.json"


@pytest.fixture(scope="module")
def population(data_files):
    events_path, _ = data_files
    rows, stats = load_population(events_path)
    return rows, stats


def _true_mean_lift(ground_truth_doc: dict, customer_ids=None) -> float:
    customers = ground_truth_doc["customers"]
    ids = customer_ids if customer_ids is not None else list(customers)
    return sum(customers[cid]["lift"] for cid in ids) / len(ids)


# --------------------------------------------------------- synthetic fixtures


def _entity(reference: str, customer_id: str | None = None, contactable: bool = True) -> dict:
    """A card_failure payment entity. `_entity(..., contactable=False)` omits
    both email and phone so the adapter rejects it with no_contact_channel,
    while still carrying customer_id (isolating the rejection reason)."""
    entity = {
        "id": reference,
        "amount": 129900,
        "method": "card",
        "status": "failed",
        "error_reason": "expired_card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "card has expired",
    }
    notes = {}
    if customer_id is not None:
        notes["customer_id"] = customer_id
        if contactable:
            notes["email"] = f"{customer_id}@example.test"
    entity["notes"] = notes
    return entity


def _events_doc(entities: dict[str, dict], seed: int = SEED) -> dict:
    """An events.json-shaped document (on-disk shape) from event_id -> entity."""
    events = [
        {
            "event_id": event_id,
            "created_at": 1735689600 + i,
            "payload": {"event": "payment.failed", "payload": {"payment": {"entity": entity}}},
        }
        for i, (event_id, entity) in enumerate(entities.items(), start=1)
    ]
    return {"meta": {"seed": seed, "n_events": len(events)}, "events": events}


def _write_events(tmp_path: Path, entities: dict[str, dict], seed: int = SEED) -> Path:
    path = tmp_path / "events.json"
    path.write_text(json.dumps(_events_doc(entities, seed)), encoding="utf-8")
    return path


def _minimal_ground_truth(customer_lifts: dict[str, tuple[float, float]], seed: int = SEED) -> dict:
    """A tiny hand-built ground_truth.json-shaped doc: customer_id -> (base, nudged)."""
    customers = {
        cid: {
            "customer_id": cid,
            "p_would_pay_anyway": base,
            "p_pay_if_nudged": nudged,
            "lift": round(nudged - base, 6),
        }
        for cid, (base, nudged) in customer_lifts.items()
    }
    return {"meta": {"seed": seed}, "customers": customers}


# ---------------------------------------------------------- a. do-nothing = 0


def test_do_nothing_uplift_lands_on_zero_ci_straddles(population, dataset):
    rows, _ = population
    _, ground_truth_doc = dataset
    result = run_policy(rows, ground_truth_doc, do_nothing_policy, policy_name="do_nothing")

    assert result.ci_low < 0.0 < result.ci_high, (
        "PASS gate: a do-nothing policy must not be measurable as an effect. "
        f"got CI [{result.ci_low}, {result.ci_high}]"
    )
    assert abs(result.uplift) < 0.02, f"do-nothing uplift should be ~0, got {result.uplift}"
    assert result.treatment.n > 0 and result.control.n > 0


# --------------------------------------------------- b. recover-everything = big


def test_recover_everything_uplift_is_large_and_matches_treatment_arm_true_lift(population, dataset):
    """The CI must exclude zero and bracket the TRUE mean lift of the
    treatment-arm customers specifically -- not the population-wide mean
    lift. Deterministic hashing means the ~1,100-customer treatment subset's
    true mean lift is its own draw from the population and will drift from
    0.1042 by chance; asserting against the population figure would be
    asserting against the wrong denominator.
    """
    rows, _ = population
    _, ground_truth_doc = dataset
    seed = ground_truth_doc["meta"]["seed"]
    result = run_policy(
        rows, ground_truth_doc, recover_everything_policy, policy_name="recover_everything"
    )

    assert result.ci_low > 0.0, f"CI must clearly exclude zero, got [{result.ci_low}, {result.ci_high}]"

    treatment_ids = [r["customer_id"] for r in rows if assign_arm(seed, r["customer_id"]) == "treatment"]
    treatment_true_lift = _true_mean_lift(ground_truth_doc, treatment_ids)
    population_true_lift = _true_mean_lift(ground_truth_doc)

    assert result.ci_low <= treatment_true_lift <= result.ci_high, (
        f"treatment-arm true mean lift {treatment_true_lift:.4f} (population: "
        f"{population_true_lift:.4f}, drift {treatment_true_lift - population_true_lift:+.4f}) "
        f"not bracketed by CI [{result.ci_low:.4f}, {result.ci_high:.4f}]"
    )


# --------------------------------------------------------------- c. ~70/30 split


def test_split_is_approximately_70_30(population):
    """Arm assignment is one independent Bernoulli(0.7) trial per customer
    (enforced by the population invariant: one row per customer_id), so the
    binomial SE on the observed treatment ratio is sqrt(p(1-p)/n). At this
    dataset's n=1611 customers, p=0.7: SE = sqrt(0.7*0.3/1611) ~= 0.0114
    (~1.1pp). A +-3 sigma band (~=0.0343) is used as the tolerance: wide
    enough that a correct hash essentially never fails this test, tight
    enough that a biased or broken assignment function (which would shift
    the ratio by many SEs) is still caught. See DECISIONS.md "Slice 5".
    """
    rows, _ = population
    n = len(rows)
    arms = [assign_arm(SEED, r["customer_id"]) for r in rows]
    ratio = sum(1 for a in arms if a == "treatment") / n

    se = math.sqrt(0.7 * 0.3 / n)
    tolerance = 3 * se
    assert abs(ratio - 0.7) < tolerance, (
        f"treatment ratio {ratio:.4f} not within {tolerance:.4f} (3 SE, n={n}) of 0.70"
    )


# ------------------------------------------------------------ d. order independence


def test_order_independence_shuffled_events_give_identical_uplift(dataset, tmp_path):
    events_doc, ground_truth_doc = dataset

    forward_dir = tmp_path / "forward"
    datagen.write_dataset(str(forward_dir), events_doc, ground_truth_doc)
    rows_forward, _ = load_population(forward_dir / "events.json")
    result_forward = run_policy(rows_forward, ground_truth_doc, recover_everything_policy)

    shuffled_events = list(events_doc["events"])
    random.Random(999).shuffle(shuffled_events)
    shuffled_doc = {**events_doc, "events": shuffled_events}
    shuffled_dir = tmp_path / "shuffled"
    datagen.write_dataset(str(shuffled_dir), shuffled_doc, ground_truth_doc)
    rows_shuffled, _ = load_population(shuffled_dir / "events.json")
    result_shuffled = run_policy(rows_shuffled, ground_truth_doc, recover_everything_policy)

    assert result_forward.uplift == result_shuffled.uplift
    assert result_forward.ci_low == result_shuffled.ci_low
    assert result_forward.ci_high == result_shuffled.ci_high
    assert result_forward.treatment.n == result_shuffled.treatment.n
    assert result_forward.control.n == result_shuffled.control.n
    assert {r["customer_id"] for r in rows_forward} == {r["customer_id"] for r in rows_shuffled}


# ---------------------------------------------------------- e. restart independence


def test_restart_independence_fresh_process_same_arms(data_files):
    events_path, ground_truth_path = data_files

    prog = textwrap.dedent(
        """
        import hashlib, json, sys
        from eval.measurement import load_population, assign_arms

        events_path, ground_truth_path = sys.argv[1], sys.argv[2]
        rows, _ = load_population(events_path)
        with open(ground_truth_path, encoding="utf-8") as fh:
            seed = json.load(fh)["meta"]["seed"]
        arms = assign_arms((r["customer_id"] for r in rows), seed)
        h = hashlib.sha256()
        for cid in sorted(arms):
            h.update(f"{cid}:{arms[cid]}".encode())
        print(h.hexdigest())
        """
    )
    outs = [
        subprocess.run(
            [sys.executable, "-c", prog, str(events_path), str(ground_truth_path)],
            check=True, capture_output=True, text=True, cwd=str(REPO),
        ).stdout.strip()
        for _ in range(2)
    ]
    assert outs[0] == outs[1]
    assert outs[0] != ""


# --------------------------------------------------- f. same customer, same arm


def test_customer_with_two_distinct_payment_ids_resolves_to_the_same_arm(tmp_path):
    """Not sha256 determinism-on-a-repeated-string: two DISTINCT payment
    references for one customer, pushed through the real card_failure
    adapter + Ingestor (so they do NOT dedupe into one row -- see the
    invariant test below), then through the real run_policy pipeline. Both
    rows must be resolved into the SAME arm's bucket, never split 1-1.
    run_policy's own assign_arm call is what's under test here, not ours.
    """
    cid = "cust_repeat_offender"
    ingestor = Ingestor(":memory:")
    r1 = ingestor.ingest("card_failure", {
        "event": "payment.failed", "created_at": 1735689600,
        "payload": {"payment": {"entity": _entity("pay_first", cid)}},
    })
    r2 = ingestor.ingest("card_failure", {
        "event": "payment.failed", "created_at": 1735689601,
        "payload": {"payment": {"entity": _entity("pay_second", cid)}},
    })
    ingestor.close()

    assert r1.row["reference"] != r2.row["reference"], "test setup must use two distinct payment ids"
    rows = [r1.row, r2.row]

    ground_truth = _minimal_ground_truth({cid: (0.3, 0.6)})
    result = run_policy(rows, ground_truth, do_nothing_policy)

    assert result.treatment.n + result.control.n == 2
    assert (result.treatment.n, result.control.n) in {(2, 0), (0, 2)}, (
        "both of this customer's rows must land in the same arm's bucket, "
        f"got treatment.n={result.treatment.n} control.n={result.control.n}"
    )


def test_assign_arm_has_no_runtime_randomness():
    cid = "cust_00042"
    before = assign_arm(SEED, cid)
    random.seed(1)
    random.random()
    random.seed(2)
    random.random()
    after = assign_arm(SEED, cid)
    assert before == after


# --------------------------------------------------------- population invariant


def test_load_population_raises_on_duplicate_customer_id(tmp_path):
    """A customer with two distinct payment ids must never silently produce
    two rows -- load_population's one-row-per-customer contract fires loudly
    instead of letting the recovery-rate denominator quietly go event-level.
    """
    entities = {
        "evt_1": _entity("pay_first", "cust_dup"),
        "evt_2": _entity("pay_second", "cust_dup"),
        "evt_3": _entity("pay_other", "cust_fine"),
    }
    events_path = _write_events(tmp_path, entities)

    with pytest.raises(DuplicateCustomerRows) as exc_info:
        load_population(events_path)
    assert exc_info.value.duplicate_customer_ids == ["cust_dup"]
    assert exc_info.value.duplicate_counts == {"cust_dup": 2}


# --------------------------------------------------- rejected / excluded events


def test_contactless_event_is_rejected_counted_and_excluded_from_experiment(tmp_path):
    good_ids = ["cust_a", "cust_b", "cust_c"]
    entities = {f"evt_good_{i}": _entity(f"pay_good_{i}", cid) for i, cid in enumerate(good_ids, 1)}
    entities["evt_bad"] = _entity("pay_bad", "cust_bad", contactable=False)
    events_path = _write_events(tmp_path, entities)

    rows, stats = load_population(events_path)

    assert stats["rejected"] == 1
    assert stats["rejected_by_reason"] == {"no_contact_channel": 1}
    assert stats["inserted"] == 3

    row_customer_ids = {r["customer_id"] for r in rows}
    assert row_customer_ids == set(good_ids)
    assert "cust_bad" not in row_customer_ids
    assert len(rows) == 3

    ground_truth = _minimal_ground_truth({
        "cust_a": (0.2, 0.5), "cust_b": (0.3, 0.6), "cust_c": (0.25, 0.55),
    })
    result = run_policy(rows, ground_truth, recover_everything_policy)
    assert result.treatment.n + result.control.n == 3, (
        "the rejected/contactless customer must never enter either arm's denominator"
    )


# ---------------------------------------------------------------- pipeline gate


def test_no_ground_truth_or_eval_reference_under_app():
    import re

    pattern = re.compile(r"ground_truth|eval\.")
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path}:{i}: {line}")
    assert hits == [], "app/ must never reference ground_truth or import eval.*:\n" + "\n".join(hits)
