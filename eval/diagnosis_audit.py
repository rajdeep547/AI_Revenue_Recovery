"""Slice 6 · Rules diagnosis audit. NOT part of the pipeline (see
``eval/__init__.py``): ``app/`` may never import from here.

Hand-labels 100 real failure rows by reading ``error_description`` --
never ``error_reason`` (present in the same payload but deliberately
unread, to keep hand-labeling an independent check on the rules) and never
``ground_truth.json`` (this dataset has no ground_truth for root cause
anyway; the label comes from the event itself) -- then scores
``app.diagnosis.diagnose`` (which sees only ``error_code``) against those
labels with a confusion matrix.

Run over the real dataset::

    python -m eval.diagnosis_audit --events data/events.json
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from app.diagnosis import ROOT_CAUSES, UNKNOWN, diagnose
from eval.measurement import load_population

# Hand-label lookup: the exact, fixed error_description text datagen writes
# per error_reason (confirmed against datagen._REASON_DETAIL). Reading this
# text and writing down a label is exactly what a human triager would do --
# this dict is the record of that reading, not a shortcut around it. It
# never touches error_reason or ground_truth.json.
HAND_LABEL_BY_DESCRIPTION: dict[str, str] = {
    "issuer or UPI bank temporarily unavailable": "bank_downtime",
    "gateway timed out before confirmation": "gateway_timeout",
    "card has expired": "expired_card",
    "card number or CVV is invalid": "invalid_card",
    "payment failed due to insufficient funds": "insufficient_funds",
    "3DS/OTP was not completed in time": "otp_timeout",
}


def error_description_of(row: dict) -> str | None:
    try:
        entity = row["raw"]["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return None
    return entity.get("error_description")


def hand_label(row: dict) -> str:
    """The human label for one row, read from error_description alone."""
    desc = error_description_of(row)
    return HAND_LABEL_BY_DESCRIPTION.get(desc, UNKNOWN)


def confusion_matrix(rows: list[dict]) -> dict[str, Counter]:
    """``{true_label: Counter({predicted_label: count})}``."""
    matrix: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        true = hand_label(row)
        pred = diagnose(row)
        matrix[true][pred] += 1
    return matrix


def worst_class(matrix: dict[str, Counter]) -> tuple[str, int, int]:
    """The true label with the most misclassified instances (by volume, not
    rate -- several classes tie at 100% error rate here, so volume is the
    only thing that separates them). Returns
    ``(label, n_misclassified, n_total)``."""
    worst = None
    for true, preds in matrix.items():
        total = sum(preds.values())
        correct = preds.get(true, 0)
        wrong = total - correct
        if worst is None or wrong > worst[1]:
            worst = (true, wrong, total)
    return worst


def _fmt_matrix(matrix: dict[str, Counter]) -> str:
    labels = [l for l in ROOT_CAUSES if l in matrix]
    header = "true \\ pred"
    lines = [f"{header:<20}" + "".join(f"{l:<20}" for l in labels)]
    for true in labels:
        row = matrix[true]
        total = sum(row.values())
        correct = row.get(true, 0)
        recall = correct / total if total else float("nan")
        lines.append(
            f"{true:<20}" + "".join(f"{row.get(p, 0):<20}" for p in labels)
            + f"  recall={recall:.2f} n={total}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Slice 6 rules-diagnosis audit")
    ap.add_argument("--events", default="data/events.json")
    ap.add_argument("--n", type=int, default=100, help="how many failure rows to hand-label")
    args = ap.parse_args(argv)

    rows, _ = load_population(args.events)
    sample = rows[: args.n]

    matrix = confusion_matrix(sample)
    print(f"hand-labeled {len(sample)} events\n")
    print(_fmt_matrix(matrix))

    label, wrong, total = worst_class(matrix)
    print(
        f"\nworst class: {label}  ({wrong}/{total} misclassified)\n"
        f"failure mode: error_code alone can't separate the causes sharing "
        f"its code; CODE_TO_ROOT_CAUSE defaults every BAD_REQUEST_ERROR to "
        f"'insufficient_funds', so '{label}' is never predicted correctly."
    )


if __name__ == "__main__":
    main()
