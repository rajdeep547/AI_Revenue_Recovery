"""Slice 8 - integration. Wire the existing layers into one runtime path.

``process_failure`` and ``ingest_live_failure`` are the surfaces here. They
add NO decision logic, NO diagnosis logic and NO action execution. Every
step calls an earlier slice's code:

  * Slice 4  ``app.ingest`` card_failure adapter   - bridge the live webhook
                                                     payload into a normalized row
                                                     (``ingest_live_failure``)
  * Slice 4  ``app.ingest.Ingestor``               - load the normalized row
  * Slice 6  ``app.diagnosis.diagnose``            - rules root-cause (RULES path only)
  * Slice 5  ``app.arms.assign_arm``               - deterministic treatment / control
  * Slice 7  ``app.decision.engine.decide``        - the Decision
  * Slice 7  ``app.decision.store.record_decision``- append-only persistence

A terminal of ``ACT`` means the Decision was *recorded*, NOT that a nudge
was sent. Nothing in this module - or anything reachable from it - sends,
executes, or dispatches an action. That is a later slice.

RULES_CONFIDENCE provenance
---------------------------
``RULES_CONFIDENCE`` (0.32) is the overall accuracy of the rules classifier
against blind human labels in Slice 6's stratified hand-label audit,
recorded in ``slice6_score.txt`` as matrix B (n = 100, per-class floor 15,
sampling seed 20260829, all 100 rows labelled, ``overall accuracy =
0.320``). That audit scored ``rules/error_code_map.json`` (``error_code`` +
``method``); the pipeline runs ``app.diagnosis`` (``error_code`` only),
which DECISIONS.md Slice 6 puts at the same ~0.32 ceiling by the same
structural argument (``BAD_REQUEST_ERROR`` is a four-way collision,
``GATEWAY_ERROR`` a coin toss). Two caveats, stated plainly: it is a
single corpus-wide figure used as a flat per-event, per-cause confidence -
NOT a calibrated per-cause probability - and it transfers from a sibling
classifier, not a direct score of ``app.diagnosis``. So: derived from a
real artifact, but an approximation pending per-cause calibration.
"""

from __future__ import annotations

import copy
import sqlite3

from app.arms import assign_arm  # Slice 5 - single implementation lives under app/
from app.decision import store
from app.decision.engine import Decision, SkipReason, decide
from app.diagnosis import diagnose
from app.ingest import Ingestor, Outcome

# Marker written into the normalized row's preserved ``raw`` payload (under
# ``notes``) so a reviewer can tell a live-bridged row from a synthetic one.
CID_SOURCE_KEY = "customer_id_source"

# See the module docstring ("RULES_CONFIDENCE provenance"): Slice 6 blind
# audit, slice6_score.txt matrix B, overall accuracy 0.320. A flat
# corpus-wide figure used as a per-event confidence - not per-cause
# calibrated. For an ``unknown`` cause the prior already equals
# ``population_incremental`` (0.10), so ``p_effective`` is 0.10 regardless of
# this value.
RULES_CONFIDENCE = 0.32


def _entity(row: dict) -> dict:
    try:
        return row["raw"]["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return {}


def _load_row(payment_id: str, db_path: str) -> dict | None:
    """The Slice 4 normalized row for this payment, or None. ``card_failure``
    is the only source keyed by payment id."""
    with Ingestor(db_path) as ing:
        for r in ing.rows():
            if r["source"] == "card_failure" and r["reference"] == payment_id:
                return r
    return None


def _derive_live_customer_id(payment_id: str) -> str:
    """A deterministic customer_id for a live payment that carries none.
    Prefixed ``live_`` so it can never collide with a datagen ``cust_NNNNN``
    id (or any real Razorpay ``cust_...`` id)."""
    return f"live_{payment_id}"


def ingest_live_failure(payload: dict, *, db_path: str) -> str | None:
    """Slice 8 live-ingest bridge. Feed a verified live ``payment.failed``
    webhook payload through the EXISTING Slice 4 ``card_failure`` adapter to
    produce a normalized row, filling only the two fields datagen supplies
    that a live test payload lacks:

      * ``customer_id`` - use ``notes.customer_id`` / ``entity.customer_id`` /
        ``payload.customer_id`` if present; otherwise derive
        ``f"live_{payment_id}"``. Either way the source ("notes" | "entity" |
        "payload" | "derived") is written into ``notes[CID_SOURCE_KEY]``, so
        it is preserved in the row's ``raw`` blob.
      * contact channels - passed straight through; a payload with neither
        email nor phone is kept (``allow_missing_contact=True``) rather than
        rejected, and the decision engine handles it.

    The amount conversion (paise -> the row's ``amount_paise``) and every
    other transform are the adapter's, unchanged.

    Returns ``None`` on success (the normalized row is now in
    ``normalized_events``; a redelivery de-dupes to the same row), or the
    adapter's ``reason_code`` string if the payload was rejected -- the
    caller logs it and still acks 200.
    """
    p = copy.deepcopy(payload)
    try:
        entity = p["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return "missing_required_field"  # no entity - the adapter would say the same

    notes = entity.get("notes")
    if not isinstance(notes, dict):
        notes = {}
        entity["notes"] = notes

    if notes.get("customer_id"):
        notes[CID_SOURCE_KEY] = "notes"
    elif entity.get("customer_id"):
        notes[CID_SOURCE_KEY] = "entity"
    elif p.get("customer_id"):
        notes[CID_SOURCE_KEY] = "payload"
    else:
        notes["customer_id"] = _derive_live_customer_id(str(entity.get("id")))
        notes[CID_SOURCE_KEY] = "derived"

    with Ingestor(db_path) as ing:
        res = ing.ingest("card_failure", p, allow_missing_contact=True)
    return res.reason_code if res.outcome is Outcome.REJECTED else None


def _existing_decision(db_path: str, payment_id: str) -> Decision | None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cols = ", ".join(Decision.FLAT_FIELDS)
        r = con.execute(
            f"SELECT {cols} FROM decisions WHERE payment_id = ? ORDER BY id LIMIT 1",
            (payment_id,),
        ).fetchone()
    finally:
        con.close()
    if r is None:
        return None
    kw = {k: r[k] for k in Decision.FLAT_FIELDS}
    kw["skip_reason"] = SkipReason[kw["skip_reason"]] if kw["skip_reason"] else None
    return Decision(**kw)


def _history(customer_id: str, db_path: str) -> dict:
    """From prior ``decisions`` rows for this customer: the most recent one
    that reached ACT is the cooldown anchor. There is no recovery-outcome
    feed in this slice, so ``prior_recoveries`` is always empty and the
    engine's control-arm self-recovery penalty stays dormant."""
    con = sqlite3.connect(db_path)
    try:
        refs = [
            row[0]
            for row in con.execute(
                "SELECT reference FROM normalized_events "
                "WHERE customer_id = ? AND source = 'card_failure'",
                (customer_id,),
            )
        ]
        last_contact_at = None
        if refs:
            placeholders = ",".join("?" * len(refs))
            hit = con.execute(
                f"SELECT MAX(created_at) FROM decisions "
                f"WHERE terminal = 'ACT' AND payment_id IN ({placeholders})",
                refs,
            ).fetchone()
            last_contact_at = hit[0] if hit else None
    finally:
        con.close()
    return {"last_contact_at": last_contact_at, "prior_recoveries": []}


def process_failure(
    payment_id: str,
    *,
    db_path: str,
    policy: dict,
    now_utc: str,
) -> Decision | None:
    """Run one failed payment: load normalized row -> rules diagnose ->
    assign arm -> build the event -> ``decide`` -> ``record_decision``.

    Returns the :class:`Decision`, or ``None`` when there is no normalized
    row for ``payment_id`` (nothing to decide on - and nothing is written).

    Idempotent: a second call for the same ``payment_id`` writes no second
    ``decisions`` row and returns the Decision already on record.

    This records a decision; it does NOT act on one. ``terminal == "ACT"``
    means the decision was recorded, not that a nudge was sent - this module
    cannot send anything.
    """
    row = _load_row(payment_id, db_path)
    if row is None:
        return None

    store.init_decision_store(db_path)  # idempotent; also ensures the audit table

    prior = _existing_decision(db_path, payment_id)
    if prior is not None:
        return prior

    cause = diagnose(row)  # Slice 6, rules path only
    entity = _entity(row)
    customer_id = row["customer_id"]
    arm = assign_arm(policy["experiment_seed"], customer_id)  # Slice 5, app.arms

    event = {
        "payment_id": payment_id,
        "cause": cause,
        "cause_confidence": RULES_CONFIDENCE,
        "ticket_inr": row["amount_paise"] / 100.0,
        # datagen emits no risk failures; this is the field the engine's risk
        # gate keys on, read straight off the ingested payload.
        "risk_blocked": entity.get("error_reason") == "risk_declined",
        "already_recovered": entity.get("status") in ("captured", "recovered"),
        "email": row["email"],
        "phone": row["phone"],
        "now_utc": now_utc,
    }
    history = _history(customer_id, db_path)

    decision = decide(event, policy, arm, history)  # Slice 7
    store.record_decision(  # Slice 7, append-only
        decision, event_id=row["event_id"], db_path=db_path, now=now_utc
    )
    return decision
