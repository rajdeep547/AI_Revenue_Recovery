import hashlib
import pathlib
import random
import subprocess
import sys
import textwrap

import pytest

import datagen
from eval.environment import Environment, resolve

REPO = pathlib.Path(__file__).resolve().parent.parent


def _ground_truth(seed: int = datagen.DEFAULT_SEED):
    _, gt = datagen.generate_dataset(seed)
    return gt


def _digest(env: Environment) -> str:
    h = hashlib.sha256()
    for cid in env.customer_ids():
        for action in ("none", "nudge"):
            h.update(f"{cid}:{action}:{int(env.resolve(cid, action))}".encode())
    return h.hexdigest()


def test_resolve_is_order_independent():
    gt = _ground_truth()
    env = Environment(gt)
    ids = env.customer_ids()

    forward = {}
    for cid in ids:
        forward[(cid, "none")] = env.resolve(cid, "none")
        forward[(cid, "nudge")] = env.resolve(cid, "nudge")

    pairs = [(cid, a) for cid in ids for a in ("none", "nudge")]
    random.Random(12345).shuffle(pairs)
    shuffled = {key: env.resolve(*key) for key in pairs}
    assert shuffled == forward

    # a fresh instance, resolving in the shuffled order, agrees too
    env2 = Environment(gt)
    assert {key: env2.resolve(*key) for key in pairs} == forward


def test_resolve_matches_one_shot_helper():
    gt = _ground_truth()
    env = Environment(gt)
    for cid in env.customer_ids()[:50]:
        for action in ("none", "nudge"):
            assert resolve(cid, action, gt) == env.resolve(cid, action)


def test_resolve_is_stable_across_process_restarts(tmp_path):
    datagen.write_dataset(str(tmp_path), *datagen.generate_dataset())
    gt_path = tmp_path / "ground_truth.json"

    prog = textwrap.dedent(
        """
        import hashlib, sys
        from eval.environment import Environment
        env = Environment(sys.argv[1])
        h = hashlib.sha256()
        for cid in env.customer_ids():
            for action in ("none", "nudge"):
                h.update(f"{cid}:{action}:{int(env.resolve(cid, action))}".encode())
        print(h.hexdigest())
        """
    )
    outs = [
        subprocess.run(
            [sys.executable, "-c", prog, str(gt_path)],
            check=True, capture_output=True, text=True, cwd=str(REPO),
        ).stdout.strip()
        for _ in range(2)
    ]
    assert outs[0] == outs[1]
    assert outs[0] == _digest(Environment(str(gt_path)))


def test_nudge_minus_none_recovery_rate_matches_mean_lift():
    gt = _ground_truth()
    env = Environment(gt)
    ids = env.customer_ids()

    none_rate = sum(env.resolve(c, "none") for c in ids) / len(ids)
    nudge_rate = sum(env.resolve(c, "nudge") for c in ids) / len(ids)
    observed_lift = nudge_rate - none_rate

    mean_lift = sum(c["lift"] for c in gt["customers"].values()) / len(ids)
    assert abs(observed_lift - mean_lift) < 0.02
    assert abs(observed_lift - 0.10) < 0.03


def test_nudge_recovers_a_superset_of_none():
    """Monotone coupling: one uniform per customer, p_pay_if_nudged >=
    p_would_pay_anyway, so nobody who self-recovers fails when nudged."""
    env = Environment(_ground_truth())
    for cid in env.customer_ids():
        if env.resolve(cid, "none"):
            assert env.resolve(cid, "nudge")


def test_run_seed_changes_the_coins():
    gt = _ground_truth()
    a = _digest(Environment(gt, run_seed=1))
    b = _digest(Environment(gt, run_seed=2))
    assert a != b


def test_resolve_rejects_unknown_action_and_customer():
    env = Environment(_ground_truth())
    cid = env.customer_ids()[0]
    with pytest.raises(ValueError):
        env.resolve(cid, "discount")
    with pytest.raises(KeyError):
        env.resolve("cust_does_not_exist", "none")
