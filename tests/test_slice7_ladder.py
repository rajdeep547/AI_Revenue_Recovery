"""Slice 7 - Fix B: ladder non-degeneracy.

Every rung must be the argmax somewhere. A rung that is dominated across the
whole swept grid is dead weight in the policy - either its cost is wrong or
its effectiveness is. This test does NOT tune the numbers to pass: if a rung
is dominated it fails and prints the window where it comes closest.

Grid assumes all channels are available, so every rung is eligible. The
argmax rule matches the engine: max EV, ties to the lower-cost rung.
"""

from __future__ import annotations

import math

from app.decision.engine import load_policy

_P_RUNG_CAP = 0.95

# Swept so the p_effective * ticket product ranges from ~0.01 to ~5e4, i.e.
# from "a nudge is almost pointless" to "a nudge is almost certain money".
_TICKETS = [5, 10, 20, 30, 50, 80, 120, 200, 350, 600, 1000, 2000,
            5000, 10000, 20000, 50000]
_P_EFFS = [0.004, 0.008, 0.015, 0.025, 0.04, 0.06, 0.09, 0.14, 0.22,
           0.33, 0.5, 0.7, 0.9, 0.95]


def _rung_ev(rung: dict, p_eff: float, ticket: float) -> float:
    p_rung = min(p_eff * rung["effectiveness"], _P_RUNG_CAP)
    return p_rung * ticket - rung["cost_inr"]


def _argmax(rungs: list[dict], p_eff: float, ticket: float) -> dict:
    return min(rungs, key=lambda r: (-_rung_ev(r, p_eff, ticket), r["cost_inr"]))


def test_every_rung_is_argmax_somewhere_on_the_grid(capsys):
    rungs = load_policy()["action_ladder"]
    names = [r["name"] for r in rungs]

    wins = {n: 0 for n in names}
    closest = {n: (math.inf, None) for n in names}  # smallest EV gap to the cell winner

    for t in _TICKETS:
        for pe in _P_EFFS:
            evs = {r["name"]: _rung_ev(r, pe, t) for r in rungs}
            winner = _argmax(rungs, pe, t)["name"]
            wins[winner] += 1
            top = evs[winner]
            for n in names:
                if n == winner:
                    continue
                gap = top - evs[n]
                if gap < closest[n][0]:
                    closest[n] = (gap, (t, pe))

    lines = [f"ladder dominance report  (grid = {len(_TICKETS) * len(_P_EFFS)} cells)"]
    for n in names:
        if wins[n]:
            lines.append(f"  {n:13s} argmax in {wins[n]:3d} cell(s)")
        else:
            gap, cell = closest[n]
            lines.append(
                f"  {n:13s} DOMINATED EVERYWHERE - closest at ticket=Rs {cell[0]}, "
                f"p_eff={cell[1]}, still Rs {gap:.4f} behind the winner"
            )
    report = "\n".join(lines)
    with capsys.disabled():
        print("\n" + report)

    dominated = sorted(n for n in names if not wins[n])
    assert not dominated, (
        f"rungs dominated across the entire grid (dead weight in the ladder): "
        f"{dominated}\n{report}"
    )
