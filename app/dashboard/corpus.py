"""CORPUS RUN panel -- read verbatim from ``results/final_run.json``.

This is the Slice 11 frozen run of the SAME pipeline over the synthetic
2,000-event corpus. Its recovery outcomes come from the eval harness
(``eval/environment.py`` resolving latent per-customer parameters), NOT from
live traffic -- so every corpus card on the page carries the badge
"simulated outcomes · eval harness · not live traffic" and the panel is drawn
in its own colour. The corpus numbers are shown EXACTLY as stored: the file is
parsed with ``parse_float=str`` / ``parse_int=str`` so no value is re-rounded
or recomputed here, and nothing is ever blended with the LIVE panel.

If the file is absent, :func:`load_corpus` returns ``None`` and the page
renders one line saying so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "results" / "final_run.json"

BADGE = "simulated outcomes · eval harness · not live traffic"


def _path() -> Path:
    override = os.environ.get("CORPUS_RESULT_PATH")
    return Path(override) if override else _DEFAULT_PATH


def load_corpus() -> dict | None:
    """The corpus panel context, or ``None`` if ``results/final_run.json`` is
    absent. Every leaf value is the verbatim string from the JSON file."""
    path = _path()
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh, parse_float=str, parse_int=str)

    cl = doc["aggregate"]["customer_level"]
    el = doc["aggregate"]["event_level"]
    prov = doc["provenance"]

    def rung_rows(mapping: dict) -> list[tuple[str, str]]:
        return [(k, v) for k, v in mapping.items()]

    return {
        "badge": BADGE,
        "source_file": "results/final_run.json",
        "artifact": doc.get("artifact"),
        "policy_version": prov.get("policy_version"),
        "n_customers": prov.get("n_customers"),
        "n_events": prov.get("n_events"),
        "corpus_time_min": prov.get("corpus_time_min"),
        "corpus_time_max": prov.get("corpus_time_max"),
        # card 1 equivalent
        "distinct_actions_by_rung": rung_rows(el.get("distinct_actions_by_rung", {})),
        "action_counts_by_rung": rung_rows(el.get("action_counts_by_rung", {})),
        "skip_counts_by_reason": rung_rows(el.get("skip_counts_by_reason", {})),
        "route_to_human_count": el.get("route_to_human_count"),
        "suppressed_by_guardrail_count": el.get("suppressed_by_guardrail_count"),
        # card 2 equivalent
        "n_treatment_customers": cl.get("n_treatment_customers"),
        "n_control_customers": cl.get("n_control_customers"),
        "n_treatment_blocked_customers": cl.get("n_treatment_blocked_customers"),
        # card 3 equivalent
        "recovery_rate_treatment": cl.get("recovery_rate_treatment"),
        "recovery_rate_control": cl.get("recovery_rate_control"),
        "recovery_count_treatment": cl.get("recovery_count_treatment"),
        "recovery_count_control": cl.get("recovery_count_control"),
        # card 4 equivalent -- headline
        "incremental_uplift_pp": cl.get("incremental_uplift_pp"),
        "uplift_ci_low_pp": cl.get("uplift_ci_95", {}).get("low_pp"),
        "uplift_ci_high_pp": cl.get("uplift_ci_95", {}).get("high_pp"),
        "incremental_recoveries_count": cl.get("incremental_recoveries_count"),
        # card 5 equivalent
        "total_recovered_value_treatment_inr": cl.get("total_recovered_value_treatment_inr"),
        "total_recovered_value_control_inr": cl.get("total_recovered_value_control_inr"),
        "incremental_recovered_value_inr": cl.get("incremental_recovered_value_inr"),
        "total_action_cost_inr": el.get("total_action_cost_inr"),
        "net_incremental_ev_inr": cl.get("net_incremental_ev_inr"),
        "net_incremental_ev_ci_low": cl.get("net_incremental_ev_ci_95", {}).get("low"),
        "net_incremental_ev_ci_high": cl.get("net_incremental_ev_ci_95", {}).get("high"),
    }
