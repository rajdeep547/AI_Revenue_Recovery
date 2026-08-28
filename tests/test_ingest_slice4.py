"""Slice 4 · Break phase. Three shapes in, one shape out, dedupe holds.

Tests marked TARGET fail until contact fields + reject quarantine land.
"""
import sqlite3
from copy import deepcopy

import pytest

from app.ingest import ADAPTERS, FIELDS, Ingestor, normalize

WHEN_ISO = "2025-01-01T00:00:00+00:00"

# --- fixture builders: adjust to match your real payload shapes -------------

def card(ref="pay_ABC123", **over):
    # _adapt_card_failure reads: payload.payment.entity.{id,amount,currency,
    # method,error_reason,notes.customer_id,email,contact} and created_at at the
    # TOP level. **over updates the entity.
    entity = {
        "id": ref,
        "amount": 129900,
        "currency": "INR",
        "method": "card",
        "error_reason": "expired_card",
        "notes": {"customer_id": "cust_1"},
        "email": "A.Sharma@Example.COM ",
        "contact": "+91 98765 43210",
    }
    entity.update(over)
    return {
        "id": "evt_card_1",
        "event": "payment.failed",
        "created_at": 1735689600,
        "payload": {"payment": {"entity": entity}},
    }


def cart(ref="998877", **over):
    # _adapt_abandoned_cart reads: checkout_id, customer_id (fallback for
    # customer.id), total_price, currency, abandoned_at, email, phone.
    p = {
        "checkout_id": ref,
        "customer_id": "cust_1",
        "total_price": "1299.00",
        "currency": "INR",
        "abandoned_at": "2025-01-01T00:00:00Z",
        "email": "a.sharma@example.com",
        "phone": "9876543210",
    }
    p.update(over)
    return p


def mandate(ref="inv_777", **over):
    # _adapt_mandate_failure reads: invoice_id, customer_ref, amount_due,
    # currency, payment_method, failure_reason, charge_attempted_at,
    # customer_email, customer_phone.
    p = {
        "invoice_id": ref,
        "customer_ref": "cust_1",
        "amount_due": 49900,
        "currency": "INR",
        "payment_method": "emandate",
        "failure_reason": "insufficient_funds",
        "charge_attempted_at": 1735689600,
        "customer_email": "a.sharma@example.com",
        "customer_phone": "+919876543210",
    }
    p.update(over)
    return p


ALL_THREE = [("card_failure", card), ("abandoned_cart", cart),
             ("mandate_failure", mandate)]


@pytest.fixture
def ing():
    with Ingestor(":memory:") as i:
        yield i


def outcome(res):
    """Works before and after the INSERTED/DUPLICATE/REJECTED change."""
    return getattr(res, "outcome", None) or (
        "INSERTED" if res.is_new else "DUPLICATE")


# --- one shape out ---------------------------------------------------------

@pytest.mark.parametrize("source,build", ALL_THREE)
def test_every_source_yields_the_canonical_shape(source, build):
    row = normalize(source, build())
    assert set(row) == set(FIELDS) | {"raw"}


def test_all_three_agree_on_key_order():
    shapes = {tuple(normalize(s, b())) for s, b in ALL_THREE}
    assert len(shapes) == 1


def test_adapter_registry_matches_tested_sources():
    assert set(ADAPTERS) == {s for s, _ in ALL_THREE}


@pytest.mark.parametrize("source,build", ALL_THREE)
def test_amount_is_integer_paise(source, build):
    amt = normalize(source, build())["amount_paise"]
    assert isinstance(amt, int) and not isinstance(amt, bool)


def test_major_unit_string_converts_exactly():
    assert normalize("abandoned_cart", cart(total_price="1299.00"))["amount_paise"] == 129900
    assert normalize("abandoned_cart", cart(total_price="0.01"))["amount_paise"] == 1
    assert normalize("abandoned_cart", cart(total_price="4.995"))["amount_paise"] == 500


@pytest.mark.parametrize("source,build", ALL_THREE)
def test_timestamps_land_on_one_utc_string(source, build):
    assert normalize(source, build())["occurred_at"] == WHEN_ISO


@pytest.mark.parametrize("source,build", ALL_THREE)
def test_currency_defaults_to_inr(source, build):
    p = build()
    if source == "card_failure":
        p["payload"]["payment"]["entity"].pop("currency")
    else:
        p.pop("currency")
    assert normalize(source, p)["currency"] == "INR"


@pytest.mark.parametrize("source,build", ALL_THREE)
def test_normalize_is_pure(source, build):
    p = build()
    before = deepcopy(p)
    assert normalize(source, p) == normalize(source, p)
    assert p == before, "adapter mutated its input"


def test_cart_has_no_method_or_reason_but_keeps_the_keys():
    row = normalize("abandoned_cart", cart())
    assert row["method"] is None and row["reason"] is None


def test_raw_payload_survives_untouched():
    p = card()
    assert normalize("card_failure", p)["raw"] == p


# --- dedupe ----------------------------------------------------------------

@pytest.mark.parametrize("source,build", ALL_THREE)
def test_same_event_twice_writes_once(ing, source, build):
    first = ing.ingest(source, build())
    second = ing.ingest(source, build())
    assert outcome(first) == "INSERTED"
    assert outcome(second) == "DUPLICATE"
    assert first.row == second.row


def test_all_three_then_all_three_again_leaves_three(ing):
    for s, b in ALL_THREE:
        ing.ingest(s, b())
    for s, b in reversed(ALL_THREE):
        ing.ingest(s, b())
    assert ing.count() == 3


def test_redelivery_with_fresh_envelope_id_still_collapses(ing):
    ing.ingest("card_failure", card())
    again = card()
    again["id"] = "evt_card_RETRY"
    assert outcome(ing.ingest("card_failure", again)) == "DUPLICATE"


def test_first_write_wins_on_mutated_redelivery(ing):
    stored = ing.ingest("card_failure", card()).row
    mutated = ing.ingest("card_failure", card(amount=999999)).row
    assert mutated["amount_paise"] == stored["amount_paise"]


def test_same_reference_different_sources_do_not_collide(ing):
    ing.ingest("abandoned_cart", cart(checkout_id="X1"))
    ing.ingest("mandate_failure", mandate(invoice_id="X1"))
    assert ing.count() == 2


def test_dedupe_survives_restart(tmp_path):
    db = str(tmp_path / "ingest.db")
    with Ingestor(db) as a:
        a.ingest("card_failure", card())
    with Ingestor(db) as b:
        assert outcome(b.ingest("card_failure", card())) == "DUPLICATE"
        assert b.count() == 1


def test_event_id_is_unique_at_the_db_level(tmp_path):
    db = str(tmp_path / "ingest.db")
    with Ingestor(db) as a:
        a.ingest("card_failure", card())
    cols = sqlite3.connect(db).execute(
        "SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
    assert any("UNIQUE" in (s[0] or "").upper() for s in cols)


# --- TARGET: contact channel ----------------------------------------------

@pytest.mark.parametrize("source,build", ALL_THREE)
def test_row_carries_contact_fields(source, build):
    row = normalize(source, build())
    assert "email" in row and "phone" in row


def test_email_is_normalized():
    assert normalize("card_failure", card())["email"] == "a.sharma@example.com"


def test_phone_is_normalized_to_e164():
    for s, b in ALL_THREE:
        assert normalize(s, b())["phone"] == "+919876543210"


def test_contact_normalization_is_idempotent():
    once = normalize("abandoned_cart", cart())
    twice = normalize("abandoned_cart", cart(email=once["email"], phone=once["phone"]))
    assert (once["email"], once["phone"]) == (twice["email"], twice["phone"])


def test_email_only_and_phone_only_are_accepted(ing):
    a = ing.ingest("abandoned_cart", cart(checkout_id="C_MAIL", phone=None))
    b = ing.ingest("abandoned_cart", cart(checkout_id="C_SMS", email=None))
    assert outcome(a) == outcome(b) == "INSERTED"


def test_no_contact_channel_is_rejected(ing):
    res = ing.ingest("abandoned_cart", cart(email=None, phone=""))
    assert outcome(res) == "REJECTED"
    assert res.reason_code == "no_contact_channel"
    assert ing.count() == 0


# --- TARGET: reject quarantine --------------------------------------------

def test_missing_required_field_is_rejected_not_raised(ing):
    res = ing.ingest("abandoned_cart", cart(customer_id=None))
    assert outcome(res) == "REJECTED"
    assert res.reason_code == "missing_required_field"


def test_unknown_source_is_rejected(ing):
    assert ing.ingest("telepathy", cart()).reason_code == "unknown_source"


def test_bad_amount_is_rejected(ing):
    assert ing.ingest("abandoned_cart", cart(total_price="not-money")).reason_code == "bad_amount"


def test_bad_timestamp_is_rejected(ing):
    assert ing.ingest("abandoned_cart", cart(abandoned_at="yesterday")).reason_code == "bad_timestamp"


def test_normalize_still_raises_on_bad_input():
    with pytest.raises(Exception):
        normalize("abandoned_cart", cart(customer_id=None))


def test_a_bad_row_does_not_kill_the_batch(ing):
    batch = [cart(checkout_id=f"C{i}") for i in range(10)]
    batch[3]["customer_id"] = None
    batch[7]["email"] = batch[7]["phone"] = None
    for p in batch:
        ing.ingest("abandoned_cart", p)
    s = ing.stats()
    assert (s["inserted"], s["rejected"]) == (8, 2)
    assert s["rejected_by_reason"] == {
        "missing_required_field": 1, "no_contact_channel": 1}
    assert ing.count() == 8


def test_rejects_are_persisted_with_their_payload(tmp_path):
    db = str(tmp_path / "ingest.db")
    with Ingestor(db) as i:
        i.ingest("abandoned_cart", cart(email=None, phone=None))
    rows = sqlite3.connect(db).execute(
        "SELECT source, reason_code, raw FROM rejected_events").fetchall()
    assert len(rows) == 1 and rows[0][1] == "no_contact_channel"
    assert "998877" in rows[0][2]


def test_stats_reconciles_with_input_count(ing):
    for s, b in ALL_THREE:
        ing.ingest(s, b())
        ing.ingest(s, b())
    ing.ingest("abandoned_cart", cart(checkout_id="BAD", email=None, phone=None))
    s = ing.stats()
    assert s["inserted"] + s["duplicate"] + s["rejected"] == 7