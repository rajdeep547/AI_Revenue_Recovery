"""Ingest · normalize heterogeneous recovery events into one row shape.

Three upstream sources, three payload shapes, one output:

* ``card_failure``    — a failed card / UPI payment (Razorpay-style webhook).
* ``abandoned_cart``  — a checkout a customer never completed (storefront).
* ``mandate_failure`` — a recurring e-mandate / UPI-Autopay charge that bounced.

``normalize(source, payload)`` is a pure function: it returns the row as a dict
or raises :class:`AdapterError` (a ``ValueError``). No network, no clock, no
randomness.

``Ingestor.ingest(source, payload)`` **never raises on bad input**. It
normalizes, de-duplicates by ``event_id``, and returns an :class:`IngestResult`
whose ``outcome`` is ``INSERTED`` / ``DUPLICATE`` / ``REJECTED``. A rejected
payload is quarantined in ``rejected_events`` with a short ``reason_code`` so a
batch loop keeps going and the offending input is kept for triage.

Row shape (:data:`FIELDS` + ``raw``)::

    event_id, source, customer_id, email, phone, amount_paise, currency,
    method, reason, occurred_at, reference

``email`` / ``phone`` / ``method`` / ``reason`` are individually nullable, but a
row with **neither** email nor phone is rejected (``no_contact_channel``): it is
unreachable by any nudge and useless to the decision engine.

De-dupe key is ``event_id = f"{source}:{reference}"`` where ``reference`` is the
source's own stable business id (payment id / checkout id / invoice id) — not a
delivery id, so a redelivery still collapses. Enforced by a ``UNIQUE`` column,
so it holds across restarts for a file-backed ``Ingestor``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, NamedTuple

DEFAULT_CURRENCY = "INR"
DEFAULT_PHONE_CC = "91"  # a bare 10-digit number is assumed an Indian mobile

FIELDS = (
    "event_id",
    "source",
    "customer_id",
    "email",
    "phone",
    "amount_paise",
    "currency",
    "method",
    "reason",
    "occurred_at",
    "reference",
)

# Fields an adapter may leave as None. (A row still needs at least one of
# email / phone — enforced separately, reason_code "no_contact_channel".)
OPTIONAL_FIELDS = ("email", "phone", "method", "reason")

REASON_CODES = (
    "missing_required_field",
    "unknown_source",
    "bad_amount",
    "bad_timestamp",
    "no_contact_channel",
)


class AdapterError(ValueError):
    """Raised by an adapter / :func:`normalize` when a payload can't become a
    row. Carries a ``reason_code`` drawn from :data:`REASON_CODES`."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
        self.reason_code = reason_code
        self.detail = detail


class Outcome(str, Enum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class NormalizedRow:
    event_id: str
    source: str
    customer_id: str
    email: str | None
    phone: str | None
    amount_paise: int
    currency: str
    method: str | None
    reason: str | None
    occurred_at: str
    reference: str
    raw: dict

    def as_dict(self) -> dict:
        return asdict(self)


class IngestResult(NamedTuple):
    outcome: Outcome
    row: dict | None  # the normalized row for INSERTED / DUPLICATE
    reason_code: str | None  # set for REJECTED


# --------------------------------------------------------------------- helpers


def _require(payload: dict, *path: str) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or node.get(key) is None:
            raise AdapterError("missing_required_field", ".".join(path))
        node = node[key]
    return node


def _clean_email(value: Any) -> str | None:
    if value is None:
        return None
    email = str(value).strip().lower()
    return email or None


def _clean_phone(value: Any) -> str | None:
    """E.164-ish: strip non-digits, drop an international/trunk prefix, assume a
    bare 10-digit number is +91. Idempotent on its own output."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("00"):
        digits = digits[2:]  # 00<cc> international prefix
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # national trunk 0
    if len(digits) == 10:
        digits = DEFAULT_PHONE_CC + digits
    return "+" + digits if len(digits) >= 11 else None


def _contact(email: Any, phone: Any) -> tuple[str | None, str | None]:
    email, phone = _clean_email(email), _clean_phone(phone)
    if not email and not phone:
        raise AdapterError("no_contact_channel", "row has neither email nor phone")
    return email, phone


def _paise_from_minor(value: Any) -> int:
    """Amounts already in the minor unit (paise): Razorpay, mandate invoices."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AdapterError("bad_amount", f"not a minor-unit amount: {value!r}") from None


def _paise_from_major(value: Any) -> int:
    """Storefront totals like ``"1299.00"`` — major units to paise, no float."""
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        raise AdapterError("bad_amount", f"not a major-unit amount: {value!r}") from None


def _iso_utc(value: Any) -> str:
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a timestamp")
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        raise AdapterError("bad_timestamp", f"unparseable timestamp: {value!r}") from None


# -------------------------------------------------------------------- adapters


def _adapt_card_failure(payload: dict) -> NormalizedRow:
    entity = _require(payload, "payload", "payment", "entity")
    reference = str(_require(entity, "id"))
    notes = entity.get("notes")
    notes = notes if isinstance(notes, dict) else {}
    customer_id = (
        notes.get("customer_id")
        or entity.get("customer_id")
        or payload.get("customer_id")
    )
    if not customer_id:
        raise AdapterError("missing_required_field", "customer_id")
    email, phone = _contact(
        notes.get("email") or entity.get("email"),
        notes.get("phone") or notes.get("contact") or entity.get("contact"),
    )
    return NormalizedRow(
        event_id=f"card_failure:{reference}",
        source="card_failure",
        customer_id=str(customer_id),
        email=email,
        phone=phone,
        amount_paise=_paise_from_minor(_require(entity, "amount")),
        currency=str(entity.get("currency") or DEFAULT_CURRENCY),
        method=entity.get("method"),
        reason=entity.get("error_reason") or entity.get("error_code"),
        occurred_at=_iso_utc(_require(payload, "created_at")),
        reference=reference,
        raw=payload,
    )


def _adapt_abandoned_cart(payload: dict) -> NormalizedRow:
    reference = str(_require(payload, "checkout_id"))
    customer = payload.get("customer")
    customer = customer if isinstance(customer, dict) else {}
    customer_id = customer.get("id") or payload.get("customer_id")
    if not customer_id:
        raise AdapterError("missing_required_field", "customer.id")
    email, phone = _contact(
        customer.get("email") or payload.get("email"),
        customer.get("phone") or payload.get("phone"),
    )
    return NormalizedRow(
        event_id=f"abandoned_cart:{reference}",
        source="abandoned_cart",
        customer_id=str(customer_id),
        email=email,
        phone=phone,
        amount_paise=_paise_from_major(_require(payload, "total_price")),
        currency=str(payload.get("currency") or DEFAULT_CURRENCY),
        method=None,  # a cart has no payment method yet
        reason=None,  # nor a failure reason
        occurred_at=_iso_utc(_require(payload, "abandoned_at")),
        reference=reference,
        raw=payload,
    )


def _adapt_mandate_failure(payload: dict) -> NormalizedRow:
    reference = str(_require(payload, "invoice_id"))
    customer_id = str(_require(payload, "customer_ref"))
    email, phone = _contact(
        payload.get("customer_email"),
        payload.get("customer_phone"),
    )
    return NormalizedRow(
        event_id=f"mandate_failure:{reference}",
        source="mandate_failure",
        customer_id=customer_id,
        email=email,
        phone=phone,
        amount_paise=_paise_from_minor(_require(payload, "amount_due")),
        currency=str(payload.get("currency") or DEFAULT_CURRENCY),
        method=payload.get("payment_method") or "emandate",
        reason=payload.get("failure_reason"),
        occurred_at=_iso_utc(_require(payload, "charge_attempted_at")),
        reference=reference,
        raw=payload,
    )


ADAPTERS: dict[str, Callable[[dict], NormalizedRow]] = {
    "card_failure": _adapt_card_failure,
    "abandoned_cart": _adapt_abandoned_cart,
    "mandate_failure": _adapt_mandate_failure,
}


def _adapter(source: str) -> Callable[[dict], NormalizedRow]:
    try:
        return ADAPTERS[source]
    except KeyError:
        raise AdapterError("unknown_source", str(source)) from None


def normalize(source: str, payload: dict) -> dict:
    """Run one adapter and return the normalized row as a plain dict. No
    storage, no de-duplication — the shape transform only. Raises
    :class:`AdapterError` on bad input; stays a pure function."""
    return _adapter(source)(payload).as_dict()


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------- storage


class Ingestor:
    """Normalizes events, de-duplicates by ``event_id``, quarantines rejects.

    ``db_path`` defaults to an in-memory database; pass a file path to make
    dedupe survive restarts.
    """

    _SCHEMA_NORMALIZED = """
        CREATE TABLE IF NOT EXISTS normalized_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT UNIQUE NOT NULL,
            source       TEXT NOT NULL,
            customer_id  TEXT NOT NULL,
            email        TEXT,
            phone        TEXT,
            amount_paise INTEGER NOT NULL,
            currency     TEXT NOT NULL,
            method       TEXT,
            reason       TEXT,
            occurred_at  TEXT NOT NULL,
            reference    TEXT NOT NULL,
            raw          TEXT NOT NULL,
            ingested_at  TEXT NOT NULL
        )
    """
    _SCHEMA_REJECTED = """
        CREATE TABLE IF NOT EXISTS rejected_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT NOT NULL,
            reason_code   TEXT NOT NULL,
            reason_detail TEXT,
            raw           TEXT NOT NULL,
            rejected_at   TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(self._SCHEMA_NORMALIZED)
        self._conn.execute(self._SCHEMA_REJECTED)
        self._conn.commit()
        self._inserted = 0
        self._duplicate = 0
        self._rejected: dict[str, int] = {}

    def ingest(self, source: str, payload: dict) -> IngestResult:
        try:
            row = _adapter(source)(payload)
        except AdapterError as exc:
            return self._reject(source, exc.reason_code, exc.detail, payload)
        except Exception as exc:  # noqa: BLE001 — ingest must never raise on input
            return self._reject(
                source, "missing_required_field", f"{type(exc).__name__}: {exc}", payload
            )

        params = {name: getattr(row, name) for name in FIELDS}
        params["raw"] = _dump(row.raw)
        params["ingested_at"] = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO normalized_events
                (event_id, source, customer_id, email, phone, amount_paise,
                 currency, method, reason, occurred_at, reference, raw, ingested_at)
            VALUES (:event_id, :source, :customer_id, :email, :phone, :amount_paise,
                    :currency, :method, :reason, :occurred_at, :reference, :raw,
                    :ingested_at)
            """,
            params,
        )
        self._conn.commit()
        stored = self._fetch(row.event_id)
        if cur.rowcount > 0:
            self._inserted += 1
            return IngestResult(Outcome.INSERTED, stored, None)
        self._duplicate += 1
        return IngestResult(Outcome.DUPLICATE, stored, None)

    def rows(self) -> list[dict]:
        """Every stored normalized row, in ingestion order."""
        return [
            self._to_row(r)
            for r in self._conn.execute("SELECT * FROM normalized_events ORDER BY id")
        ]

    def rejected(self) -> list[dict]:
        """Every quarantined payload, in arrival order."""
        return [
            {
                "source": r["source"],
                "reason_code": r["reason_code"],
                "reason_detail": r["reason_detail"],
                "raw": json.loads(r["raw"]),
            }
            for r in self._conn.execute("SELECT * FROM rejected_events ORDER BY id")
        ]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]

    def stats(self) -> dict:
        """What this ingestor has done since construction."""
        return {
            "inserted": self._inserted,
            "duplicate": self._duplicate,
            "rejected_by_reason": dict(sorted(self._rejected.items())),
        }

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ingestor":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- internals

    def _reject(
        self, source: Any, reason_code: str, detail: str, payload: Any
    ) -> IngestResult:
        self._conn.execute(
            """
            INSERT INTO rejected_events
                (source, reason_code, reason_detail, raw, rejected_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(source),
                reason_code,
                detail or None,
                _dump(payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        self._rejected[reason_code] = self._rejected.get(reason_code, 0) + 1
        return IngestResult(Outcome.REJECTED, None, reason_code)

    def _fetch(self, event_id: str) -> dict:
        return self._to_row(
            self._conn.execute(
                "SELECT * FROM normalized_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        )

    @staticmethod
    def _to_row(r: sqlite3.Row) -> dict:
        row = {name: r[name] for name in FIELDS}
        row["raw"] = json.loads(r["raw"])
        return row
