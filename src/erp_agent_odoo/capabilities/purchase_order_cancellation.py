from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.compiler_runtime.sandbox import SourceRecord

FACT_VIEW_MODEL = "derived.purchase_order_cancellation_facts"
ODOO_READ_SCHEMA = {
    "purchase.order": {
        "id": ("integer", ""),
        "state": ("selection", ""),
        "locked": ("boolean", ""),
        "invoice_ids": ("many2many", "account.move"),
        "write_date": ("datetime", ""),
    },
    "account.move": {
        "id": ("integer", ""),
        "state": ("selection", ""),
        "write_date": ("datetime", ""),
    },
}
ODOO_READ_FIELDS = {
    model: frozenset(fields) for model, fields in ODOO_READ_SCHEMA.items()
}


def _odoo_id(value: Any, *, field: str) -> int:
    candidate = value[0] if isinstance(value, (list, tuple)) and value else value
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise TypeError(f"{field} must contain one Odoo integer id")
    return candidate


def _admit_record(model: str, record: Mapping[str, Any]) -> tuple[int, str, str]:
    expected = ODOO_READ_FIELDS[model]
    actual = set(record)
    if actual != expected:
        raise ValueError(
            f"{model} read fields must exactly match the registered closure; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    record_id = _odoo_id(record["id"], field=f"{model}.id")
    revision = str(record["write_date"] or "").strip()
    if not revision:
        raise ValueError(f"{model}.write_date is required for freshness")
    try:
        canonical = json.dumps(
            {"model": model, "fields": dict(sorted(record.items()))},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{model} read must be finite JSON data") from exc
    return record_id, revision, hashlib.sha256(canonical.encode()).hexdigest()


def admit_purchase_order_cancellation_reads(
    *,
    record_ref: str,
    schemas: Mapping[str, Mapping[str, Mapping[str, Any]]],
    order: Mapping[str, Any],
    vendor_bills: Sequence[Mapping[str, Any]],
) -> SourceRecord:
    """Seal the exact Odoo read closure needed before ``button_cancel``."""

    record_ref = record_ref.strip()
    if not record_ref.startswith("purchase.order:") or not record_ref.partition(":")[2]:
        raise ValueError("record_ref must identify one purchase.order")
    if set(schemas) != set(ODOO_READ_SCHEMA):
        raise ValueError("Odoo schema closure must contain only the registered models")
    for model, expected in ODOO_READ_SCHEMA.items():
        actual = schemas[model]
        if set(actual) != set(expected):
            raise ValueError(
                f"{model} schema fields must exactly match the registered closure; "
                f"missing={sorted(set(expected) - set(actual))}, "
                f"extra={sorted(set(actual) - set(expected))}"
            )
        for field, expected_shape in expected.items():
            metadata = actual[field]
            observed_shape = (
                str(metadata.get("type") or ""),
                str(metadata.get("relation") or ""),
            )
            if observed_shape != expected_shape:
                raise ValueError(
                    f"{model}.{field} schema type/relation changed: {observed_shape}"
                )

    order_id, order_revision, order_fingerprint = _admit_record(
        "purchase.order", order
    )
    state = order["state"]
    if not isinstance(state, str) or not state.strip():
        raise TypeError("purchase.order.state must be a non-empty string")
    if not isinstance(order["locked"], bool):
        raise TypeError("purchase.order.locked must be boolean")
    invoice_ids = order["invoice_ids"]
    if not isinstance(invoice_ids, list):
        raise TypeError("purchase.order.invoice_ids must be a list")
    expected_bill_ids = {
        _odoo_id(value, field="purchase.order.invoice_ids") for value in invoice_ids
    }
    if len(expected_bill_ids) != len(invoice_ids):
        raise ValueError("purchase.order.invoice_ids must not contain duplicates")

    bills: dict[int, tuple[str, str, str]] = {}
    for bill in vendor_bills:
        bill_id, revision, fingerprint = _admit_record("account.move", bill)
        bill_state = bill["state"]
        if not isinstance(bill_state, str) or not bill_state.strip():
            raise TypeError("account.move.state must be a non-empty string")
        if bill_id in bills:
            raise ValueError("account.move reads must be unique")
        bills[bill_id] = (bill_state.strip(), revision, fingerprint)
    if set(bills) != expected_bill_ids:
        raise ValueError("purchase.order.invoice_ids and admitted vendor bills differ")

    lineage = {
        f"odoo:purchase.order:{order_id}": {
            "revision": order_revision,
            "fingerprint": order_fingerprint,
        },
        **{
            f"odoo:account.move:{bill_id}": {
                "revision": revision,
                "fingerprint": fingerprint,
            }
            for bill_id, (_state, revision, fingerprint) in sorted(bills.items())
        },
    }
    revision = hashlib.sha256(
        json.dumps(lineage, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bill_states = {
        f"account.move:{bill_id}": bill_state
        for bill_id, (bill_state, _revision, _fingerprint) in sorted(bills.items())
    }
    return SourceRecord(
        source_id=record_ref,
        title=f"Purchase order cancellation facts for {record_ref}",
        kind="record",
        content="",
        provenance={"view_model": FACT_VIEW_MODEL, "source_records": lineage},
        record_model=FACT_VIEW_MODEL,
        record_revision=revision,
        structured_fields={
            "state": state.strip(),
            "locked": order["locked"],
            "vendor_bill_states": bill_states,
            "blocking_vendor_bill_refs": sorted(
                ref
                for ref, bill_state in bill_states.items()
                if bill_state not in {"cancel", "draft"}
            ),
        },
    )


__all__ = [
    "FACT_VIEW_MODEL",
    "ODOO_READ_FIELDS",
    "ODOO_READ_SCHEMA",
    "admit_purchase_order_cancellation_reads",
]
