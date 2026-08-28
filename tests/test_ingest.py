import pytest

import datagen
from app.ingest import (
    ADAPTERS,
    FIELDS,
    Ingestor,
    Outcome,
    _clean_email,
    _clean_phone,
    normalize,
)
from eval.environment import Environment

# 2025-01-01T00:00:00Z, expressed three different ways by three sources.
_WHEN = "2025-01-01T00:00:00+00:00"
_EMAIL = "buyer@shop.io"
_PHONE = "+919876543210"

_SHAPE = frozenset(FIELDS + ("raw",))


def card(**over):
    entity = {
        "id": "pay_ABC123",
        "amount": 129900,
        "currency": "INR",
        "method": "card",
        "error_reason": "expired_card",
        "notes": {"customer_id": "cust_00042", "email": "Buyer@Shop.io", "contact": "+91 98765 43210"},
    }
    entity.update(over.pop("entity", {}))
    payload = {"id": "evt_card_1", "event": "payment.failed", "created_at": 1735689600,
               "payload": {"payment": {"entity": entity}}}
    payload.update(over)
    return payload


def cart(**over):
    payload = {
        "checkout_id": 998877,
        "cart_token": "tok_xyz",
        "customer": {"id": "cust_00042", "email": "buyer@shop.io", "phone": "9876543210"},
        "total_price": "1299.00",
        "currency": "INR",
        "abandoned_at": "2025-01-01T00:00:00Z",
        "line_items": [{"sku": "A", "qty": 1}],
    }
    payload["customer"] = {**payload["customer"], **over.pop("customer", {})}
    payload.update(over)
    return payload


def mandate(**over):
    payload = {
        "subscription_id": "sub_1",
        "mandate_id": "mnd_1",
        "invoice_id": "inv_777",
        "amount_due": 49900,
        "currency": "INR",
        "customer_ref": "cust_00042",
        "customer_email": "buyer@shop.io",
        "customer_phone": "098765 43210",
        "failure_reason": "insufficient_funds",
        "charge_attempted_at": "2025-01-01T00:00:00+00:00",
    }
    payload.update(over)
    return payload


CARD, CART, MANDATE = card(), cart(), mandate()


# --------------------------------------------------------------- shape / mapping


def test_registry_has_exactly_the_three_adapters():
    assert set(ADAPTERS) == {"card_failure", "abandoned_cart", "mandate_failure"}


def test_three_shapes_in_one_shape_out():
    rows = [
        normalize("card_failure", CARD),
        normalize("abandoned_cart", CART),
        normalize("mandate_failure", MANDATE),
    ]
    assert {frozenset(r) for r in rows} == {_SHAPE}
    for r in rows:
        assert r["customer_id"] == "cust_00042"
        assert r["occurred_at"] == _WHEN
        assert r["currency"] == "INR"
        assert r["email"] == _EMAIL
        assert r["phone"] == _PHONE
        assert isinstance(r["amount_paise"], int) and r["amount_paise"] > 0


def test_fields_shape_holds_for_all_three_sources():
    for src, payload in (("card_failure", CARD), ("abandoned_cart", CART), ("mandate_failure", MANDATE)):
        assert frozenset(normalize(src, payload)) == _SHAPE


def test_amounts_normalize_to_integer_paise():
    assert normalize("card_failure", CARD)["amount_paise"] == 129900
    assert normalize("abandoned_cart", CART)["amount_paise"] == 129900  # "1299.00"
    assert normalize("mandate_failure", MANDATE)["amount_paise"] == 49900


def test_source_specific_fields_map_through():
    c = normalize("card_failure", CARD)
    assert (c["source"], c["method"], c["reason"]) == ("card_failure", "card", "expired_card")
    assert c["event_id"] == "card_failure:pay_ABC123"

    k = normalize("abandoned_cart", CART)
    assert (k["method"], k["reason"]) == (None, None)
    assert k["event_id"] == "abandoned_cart:998877"

    m = normalize("mandate_failure", MANDATE)
    assert (m["method"], m["reason"]) == ("emandate", "insufficient_funds")
    assert m["event_id"] == "mandate_failure:inv_777"


def test_raw_payload_is_preserved_verbatim():
    assert normalize("abandoned_cart", CART)["raw"]["line_items"] == [{"sku": "A", "qty": 1}]


# ------------------------------------------------------------------- contact


def test_each_source_with_email_only_phone_only_both_neither():
    cases = {
        "card_failure": {
            "both": card(entity={"id": "pay_b"}),
            "email": card(entity={"id": "pay_e", "notes": {"customer_id": "c", "email": "a@b.io"}}),
            "phone": card(entity={"id": "pay_p", "notes": {"customer_id": "c", "contact": "9876543210"}}),
            "neither": card(entity={"id": "pay_n", "notes": {"customer_id": "c"}}),
        },
        "abandoned_cart": {
            "both": cart(checkout_id=10),
            "email": cart(checkout_id=11, customer={"phone": None, "email": "a@b.io"}),
            "phone": cart(checkout_id=12, customer={"email": None, "phone": "9876543210"}),
            "neither": cart(checkout_id=13, customer={"email": None, "phone": None}),
        },
        "mandate_failure": {
            "both": mandate(invoice_id="inv_b"),
            "email": mandate(invoice_id="inv_e", customer_phone=None, customer_email="a@b.io"),
            "phone": mandate(invoice_id="inv_p", customer_email=None, customer_phone="9876543210"),
            "neither": mandate(invoice_id="inv_n", customer_email=None, customer_phone=None),
        },
    }
    with Ingestor() as ing:
        for source, variants in cases.items():
            for kind, payload in variants.items():
                res = ing.ingest(source, payload)
                if kind == "neither":
                    assert res.outcome is Outcome.REJECTED
                    assert res.reason_code == "no_contact_channel"
                else:
                    assert res.outcome is Outcome.INSERTED, (source, kind)
                    assert frozenset(res.row) == _SHAPE
                    if kind == "email":
                        assert res.row["email"] and res.row["phone"] is None
                    if kind == "phone":
                        assert res.row["phone"] and res.row["email"] is None
        assert ing.count() == 9
        assert ing.stats() == {
            "inserted": 9,
            "duplicate": 0,
            "rejected_by_reason": {"no_contact_channel": 3},
        }


def test_phone_and_email_normalization_is_idempotent():
    for raw in ["9876543210", "+91 98765 43210", "098765 43210", "0091-98765-43210", "+14155550123"]:
        once = _clean_phone(raw)
        assert once == _clean_phone(once)
    for raw in ["  Buyer@Shop.IO ", "buyer@shop.io", "USER@EXAMPLE.COM"]:
        once = _clean_email(raw)
        assert once == _clean_email(once)
    # normalizing a payload, then feeding its outputs back through, is stable
    row = normalize("mandate_failure", MANDATE)
    assert _clean_phone(row["phone"]) == row["phone"] == _PHONE
    assert _clean_email(row["email"]) == row["email"] == _EMAIL


# ------------------------------------------------------------------- dedupe


def test_feed_all_three_then_dedupe_holds():
    with Ingestor() as ing:
        outs = [
            ing.ingest("card_failure", CARD).outcome,
            ing.ingest("abandoned_cart", CART).outcome,
            ing.ingest("mandate_failure", MANDATE).outcome,
        ]
        assert outs == [Outcome.INSERTED, Outcome.INSERTED, Outcome.INSERTED]
        assert ing.count() == 3

        assert ing.ingest("mandate_failure", MANDATE).outcome is Outcome.DUPLICATE
        assert ing.ingest("card_failure", CARD).outcome is Outcome.DUPLICATE
        assert ing.ingest("abandoned_cart", dict(CART)).outcome is Outcome.DUPLICATE
        assert ing.count() == 3
        assert [r["source"] for r in ing.rows()] == ["card_failure", "abandoned_cart", "mandate_failure"]
        assert ing.stats() == {"inserted": 3, "duplicate": 3, "rejected_by_reason": {}}


def test_duplicate_returns_the_stored_row_without_overwriting():
    with Ingestor() as ing:
        first = ing.ingest("card_failure", CARD).row
        replay = dict(CARD, id="evt_card_1_redelivery")  # new envelope id, same reference
        again = ing.ingest("card_failure", replay)
        assert again.outcome is Outcome.DUPLICATE
        assert again.row == first
        assert ing.count() == 1


def test_dedupe_persists_across_reopen(tmp_path):
    db = str(tmp_path / "ingest.db")
    with Ingestor(db) as ing:
        assert ing.ingest("card_failure", CARD).outcome is Outcome.INSERTED
        assert ing.count() == 1
    with Ingestor(db) as ing:
        assert ing.ingest("card_failure", CARD).outcome is Outcome.DUPLICATE
        assert ing.count() == 1


# ------------------------------------------------------- missing optional field


def test_missing_optional_field_still_normalizes_to_one_shape():
    no_method = card(entity={
        "id": "pay_NOMETHOD", "amount": 50000,
        "notes": {"customer_id": "cust_9", "email": "a@b.io"},
    })
    # strip every optional field: no method / error_reason / currency
    for k in ("method", "error_reason", "currency"):
        no_method["payload"]["payment"]["entity"].pop(k, None)
    with Ingestor() as ing:
        res = ing.ingest("card_failure", no_method)
    assert res.outcome is Outcome.INSERTED
    assert frozenset(res.row) == _SHAPE
    assert res.row["method"] is None
    assert res.row["reason"] is None
    assert res.row["currency"] == "INR"

    m = mandate(invoice_id="inv_opt")
    m.pop("failure_reason")
    assert normalize("mandate_failure", m)["reason"] is None

    k = cart(checkout_id=99)
    k.pop("currency")
    assert normalize("abandoned_cart", k)["currency"] == "INR"


# --------------------------------------------------------- reject quarantine


def test_normalize_still_raises_as_a_pure_function():
    no_amount = card()
    no_amount["payload"]["payment"]["entity"].pop("amount")
    with pytest.raises(ValueError, match="amount"):
        normalize("card_failure", no_amount)
    with pytest.raises(ValueError, match="unknown_source"):
        normalize("paypal_dispute", {})


def test_missing_required_field_is_rejected_not_raised_and_loop_continues():
    bad = card(entity={"id": "pay_bad", "notes": {"customer_id": "c", "email": "a@b.io"}})
    bad["payload"]["payment"]["entity"].pop("amount")  # required field gone
    batch = [("card_failure", CARD), ("card_failure", bad), ("mandate_failure", MANDATE)]
    outs = []
    with Ingestor() as ing:
        for source, payload in batch:  # a raised exception would abort this loop
            outs.append(ing.ingest(source, payload).outcome)
        assert outs == [Outcome.INSERTED, Outcome.REJECTED, Outcome.INSERTED]
        assert ing.count() == 2
        assert ing.stats()["rejected_by_reason"] == {"missing_required_field": 1}
        assert ing.rejected()[0]["reason_code"] == "missing_required_field"


def test_unknown_source_is_rejected_not_raised():
    with Ingestor() as ing:
        res = ing.ingest("paypal_dispute", {"whatever": 1})
        assert res.outcome is Outcome.REJECTED
        assert res.reason_code == "unknown_source"
        assert res.row is None
        assert ing.count() == 0
        assert ing.stats()["rejected_by_reason"] == {"unknown_source": 1}


def test_bad_amount_and_bad_timestamp_are_rejected():
    with Ingestor() as ing:
        assert ing.ingest("mandate_failure", mandate(invoice_id="inv_a", amount_due="not-a-number")).reason_code == "bad_amount"
        assert ing.ingest("card_failure", card(entity={"id": "pay_t"}, created_at="whenever")).reason_code == "bad_timestamp"
        assert ing.count() == 0
        assert ing.stats()["rejected_by_reason"] == {"bad_amount": 1, "bad_timestamp": 1}


def test_batch_of_ten_with_two_bad_rows():
    batch = [
        ("card_failure", card(entity={"id": f"pay_{i}"})) for i in range(4)
    ] + [
        ("abandoned_cart", cart(checkout_id=1000 + i)) for i in range(4)
    ] + [
        ("card_failure", card(entity={"id": "pay_nc", "notes": {"customer_id": "c"}})),  # no contact
        ("weird_source", {"nope": True}),  # unknown source
    ]
    assert len(batch) == 10
    with Ingestor() as ing:
        results = [ing.ingest(s, p) for s, p in batch]
        inserted = sum(r.outcome is Outcome.INSERTED for r in results)
        rejected = sum(r.outcome is Outcome.REJECTED for r in results)
        assert (inserted, rejected) == (8, 2)
        assert ing.count() == 8
        assert ing.stats() == {
            "inserted": 8,
            "duplicate": 0,
            "rejected_by_reason": {"no_contact_channel": 1, "unknown_source": 1},
        }


# --------------------------------------------------- customer_id namespace gate


def test_customer_id_namespace_is_shared_across_adapters():
    _, gt = datagen.generate_dataset()
    cid = next(iter(gt["customers"]))
    via_card = normalize("card_failure", card(entity={
        "id": "pay_ns", "notes": {"customer_id": cid, "email": "a@b.io"}}))
    via_cart = normalize("abandoned_cart", cart(
        checkout_id="ns", customer={"id": cid, "email": None, "phone": "9876543210"}))
    assert via_card["customer_id"] == via_cart["customer_id"] == cid

    env = Environment(gt)
    assert env.resolve(via_card["customer_id"], "none") == env.resolve(via_cart["customer_id"], "none")
    assert env.resolve(via_card["customer_id"], "nudge") == env.resolve(via_cart["customer_id"], "nudge")
