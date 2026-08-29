"""Rules-based root-cause diagnosis from ``error_code`` alone.

A real gateway integration doesn't always hand you a clean semantic
failure-reason enum -- often just a coarse machine code (`GATEWAY_ERROR`,
`BAD_REQUEST_ERROR`, ...) plus free-text description. This module
deliberately ignores the normalized row's ``reason`` field, which in this
dataset already equals the internal ``error_reason`` and would make
diagnosis trivial (an identity lookup, nothing to get wrong). Instead it
re-derives a root cause from the raw ``error_code`` on the original payload,
to find out how much a coarse code alone can actually tell you.

error code -> root cause map
-----------------------------
Razorpay's ``error_code`` collapses six distinct failure reasons onto two
codes. This map picks the majority root cause per code -- built from the
known Slice 3 error_reason frequency table in DECISIONS.md, before looking
at a single real event:

    GATEWAY_ERROR      -> bank_downtime       (0.18 vs gateway_timeout 0.14)
    BAD_REQUEST_ERROR  -> insufficient_funds  (0.28, plurality of
                           {expired_card .10, invalid_card .08,
                            insufficient_funds .28, otp_timeout .22})

This is a majority-vote default per code and nothing more -- there is no
description-text rule here on purpose, so the confusion matrix in
``eval/diagnosis_audit.py`` measures exactly what code-only diagnosis buys
you, and where it doesn't.
"""

from __future__ import annotations

CODE_TO_ROOT_CAUSE: dict[str, str] = {
    "GATEWAY_ERROR": "bank_downtime",
    "BAD_REQUEST_ERROR": "insufficient_funds",
}

UNKNOWN = "unknown"

ROOT_CAUSES = (
    "bank_downtime",
    "gateway_timeout",
    "expired_card",
    "invalid_card",
    "insufficient_funds",
    "otp_timeout",
    UNKNOWN,
)


def error_code_of(row: dict) -> str | None:
    """Pull the raw Razorpay ``error_code`` back out of a normalized row's
    preserved ``raw`` payload. Returns ``None`` if the row isn't a
    card_failure entity or carries no error_code (e.g. an authorized, never-
    failed payment)."""
    try:
        entity = row["raw"]["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return None
    return entity.get("error_code")


def diagnose(row: dict) -> str:
    """Root-cause guess for one normalized card_failure row, from
    ``error_code`` alone. Unrecognized or missing codes map to
    :data:`UNKNOWN`."""
    return CODE_TO_ROOT_CAUSE.get(error_code_of(row), UNKNOWN)
