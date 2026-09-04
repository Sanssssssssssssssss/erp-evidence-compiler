from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.compiler_runtime.models import (
    ActionProposal,
    ProofPlan,
    RegisteredActionContract,
)
from app.compiler_runtime.policy import policy_hash
from app.compiler_runtime.requirement_pack import SALES_ORDER_ACCEPTANCE_PACK
from app.compiler_runtime.sandbox import SourceRecord
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .provenance import FactProvenance

FACT_VIEW_MODEL = "derived.order_acceptance_facts"
SALES_ORDER_ACCEPTANCE_CAPABILITY_ID = "erp_bench.sales_order_acceptance.v1"
_FACT_FIELDS = (
    "requested_quantity",
    "unit_list_price",
    "pretax_budget",
    "lead_days",
)
ODOO_READ_SCHEMA = {
    "sale.order": {
        "id": ("integer", ""),
        "client_order_ref": ("char", ""),
        "date_order": ("datetime", ""),
        "commitment_date": ("datetime", ""),
        "state": ("selection", ""),
        "order_line": ("one2many", "sale.order.line"),
        "write_date": ("datetime", ""),
    },
    "sale.order.line": {
        "id": ("integer", ""),
        "order_id": ("many2one", "sale.order"),
        "product_id": ("many2one", "product.product"),
        "product_uom_qty": ("float", ""),
        "write_date": ("datetime", ""),
    },
    "product.product": {
        "id": ("integer", ""),
        "default_code": ("char", ""),
        "list_price": ("float", ""),
        "write_date": ("datetime", ""),
    },
}
ODOO_READ_FIELDS = {
    model: frozenset(fields) for model, fields in ODOO_READ_SCHEMA.items()
}


class _FrontendModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrderAcceptanceFacts(_FrontendModel):
    """A derived business view; none of these names claim to be sale.order fields."""

    requested_quantity: Decimal
    unit_list_price: Decimal
    pretax_budget: Decimal
    lead_days: Decimal
    fact_provenance: dict[str, tuple[FactProvenance, ...]]

    @field_validator(*_FACT_FIELDS, mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise TypeError("fact numbers must use decimal strings")
        return value

    @field_validator(*_FACT_FIELDS)
    @classmethod
    def finite_non_negative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("fact numbers must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def complete_lineage(self) -> OrderAcceptanceFacts:
        if self.requested_quantity == 0:
            raise ValueError("requested_quantity must be positive")
        if set(self.fact_provenance) != set(_FACT_FIELDS):
            raise ValueError("fact_provenance must exactly cover every acceptance fact")
        if any(not sources for sources in self.fact_provenance.values()):
            raise ValueError("every acceptance fact requires at least one provenance source")
        return self

    def structured_fields(self) -> dict[str, Any]:
        return {
            **{field: str(getattr(self, field)) for field in _FACT_FIELDS},
            "fact_provenance": {
                field: [item.model_dump(mode="json") for item in sources]
                for field, sources in sorted(self.fact_provenance.items())
            },
        }


class OrderAcceptanceView(_FrontendModel):
    record_ref: str
    facts: OrderAcceptanceFacts
    view_model: Literal["derived.order_acceptance_facts"] = FACT_VIEW_MODEL

    @field_validator("record_ref")
    @classmethod
    def non_empty_ref(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("order target reference must not be empty")
        return value

    @property
    def revision(self) -> str:
        return policy_hash(
            {
                field: [item.model_dump(mode="json") for item in sources]
                for field, sources in sorted(self.facts.fact_provenance.items())
            }
        )

    def content_hash(self) -> str:
        return hashlib.sha256(order_acceptance_source(self).content.encode("utf-8")).hexdigest()


class OrderAcceptanceBatch(_FrontendModel):
    records: tuple[OrderAcceptanceView, ...]

    @model_validator(mode="after")
    def unique_records(self) -> OrderAcceptanceBatch:
        refs = [record.record_ref for record in self.records]
        if len(refs) != len(set(refs)):
            raise ValueError("record_ref values must be unique")
        return self

    def record(self, record_ref: str) -> OrderAcceptanceView | None:
        return next(
            (record for record in self.records if record.record_ref == record_ref),
            None,
        )


class OrderAcceptancePolicy(_FrontendModel):
    policy_id: str
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    minimum_lead_days: Decimal | None = None

    @field_validator("policy_id")
    @classmethod
    def non_empty_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("policy_id must not be empty")
        return value

    @field_validator(
        "minimum_quantity", "maximum_quantity", "minimum_lead_days", mode="before"
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise TypeError("policy numbers must use decimal strings")
        return value

    @model_validator(mode="after")
    def valid_range(self) -> OrderAcceptancePolicy:
        values = (
            self.minimum_quantity,
            self.maximum_quantity,
            self.minimum_lead_days,
        )
        if any(
            not value.is_finite() or value < 0
            for value in values
            if value is not None
        ):
            raise ValueError("policy numbers must be finite and non-negative")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("minimum_quantity cannot exceed maximum_quantity")
        return self

    def content_hash(self) -> str:
        return policy_hash(self.to_policy_excerpt())

    def to_policy_excerpt(self) -> dict[str, Any]:
        return {
            "policy_version": "runtime-admitted",
            "policy_basis": {"policy_id": self.policy_id},
            "values": {
                "minimum_quantity": {
                    "configured": True,
                    "value": str(self.minimum_quantity),
                },
                "maximum_quantity": {
                    "configured": True,
                    "value": str(self.maximum_quantity),
                },
                "minimum_lead_days": {
                    "configured": self.minimum_lead_days is not None,
                    "value": (
                        str(self.minimum_lead_days)
                        if self.minimum_lead_days is not None
                        else None
                    ),
                },
            },
        }


def _relation_id(value: Any, *, field: str) -> int:
    candidate = value[0] if isinstance(value, (list, tuple)) and value else value
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise TypeError(f"{field} must contain one Odoo integer id")
    return candidate


def _admitted_record(
    model: str,
    record: Mapping[str, Any],
) -> tuple[str, str, str]:
    required = ODOO_READ_FIELDS[model]
    actual = set(record)
    if actual != required:
        raise ValueError(
            f"{model} read fields must exactly match the registered closure; "
            f"missing={sorted(required - actual)}, extra={sorted(actual - required)}"
        )
    record_id = _relation_id(record["id"], field=f"{model}.id")
    revision = str(record["write_date"] or "").strip()
    if not revision:
        raise ValueError(f"{model}.write_date is required for freshness")
    canonical = json.dumps(
        {"model": model, "fields": dict(sorted(record.items()))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"odoo:{model}:{record_id}",
        revision,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _odoo_datetime(value: Any, *, field: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc


def admit_order_acceptance_reads(
    *,
    schemas: Mapping[str, Mapping[str, Mapping[str, Any]]],
    order: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]],
    pretax_budget: str,
    budget_provenance: FactProvenance,
) -> OrderAcceptanceView:
    """Admit only the three read-only Odoo record shapes used by this capability."""

    if set(schemas) != set(ODOO_READ_FIELDS):
        raise ValueError("Odoo schema closure must contain only the registered models")
    for model, expected in ODOO_READ_SCHEMA.items():
        actual = schemas[model]
        if set(actual) != set(expected):
            raise ValueError(
                f"{model} schema fields must exactly match the registered closure; "
                f"missing={sorted(set(expected) - set(actual))}, "
                f"extra={sorted(set(actual) - set(expected))}"
            )
        for field, (expected_type, expected_relation) in expected.items():
            metadata = actual[field]
            observed = (
                str(metadata.get("type") or ""),
                str(metadata.get("relation") or ""),
            )
            if observed != (expected_type, expected_relation):
                raise ValueError(
                    f"{model}.{field} schema type/relation changed: {observed}"
                )
    if not lines or not products:
        raise ValueError("order acceptance requires at least one order line and product")
    if budget_provenance.source_kind not in {"document", "policy"}:
        raise ValueError("pretax budget must come from an admitted document or policy")

    order_ref, order_revision, order_fingerprint = _admitted_record(
        "sale.order", order
    )
    order_id = _relation_id(order["id"], field="sale.order.id")
    expected_line_ids = {
        _relation_id(value, field="sale.order.order_line")
        for value in order["order_line"]
    }
    line_rows: dict[int, Mapping[str, Any]] = {}
    line_lineage: dict[int, tuple[str, str, str]] = {}
    for line in lines:
        line_ref, revision, fingerprint = _admitted_record("sale.order.line", line)
        line_id = _relation_id(line["id"], field="sale.order.line.id")
        if line_id in line_rows:
            raise ValueError("sale.order.line reads must be unique")
        if _relation_id(line["order_id"], field="sale.order.line.order_id") != order_id:
            raise ValueError("sale.order.line belongs to another order")
        line_rows[line_id] = line
        line_lineage[line_id] = (line_ref, revision, fingerprint)
    if set(line_rows) != expected_line_ids:
        raise ValueError("sale.order.order_line and admitted line reads differ")

    product_rows: dict[int, Mapping[str, Any]] = {}
    product_lineage: dict[int, tuple[str, str, str]] = {}
    for product in products:
        product_ref, revision, fingerprint = _admitted_record(
            "product.product", product
        )
        product_id = _relation_id(product["id"], field="product.product.id")
        if product_id in product_rows:
            raise ValueError("product.product reads must be unique")
        product_rows[product_id] = product
        product_lineage[product_id] = (product_ref, revision, fingerprint)
    referenced_product_ids = {
        _relation_id(line["product_id"], field="sale.order.line.product_id")
        for line in line_rows.values()
    }
    if set(product_rows) != referenced_product_ids or len(product_rows) != 1:
        raise ValueError("this capability requires one fully admitted order-line product")

    quantities = [Decimal(str(line["product_uom_qty"])) for line in line_rows.values()]
    product_id, product = next(iter(product_rows.items()))
    start = _odoo_datetime(order["date_order"], field="sale.order.date_order")
    commitment = _odoo_datetime(
        order["commitment_date"], field="sale.order.commitment_date"
    )
    seconds = Decimal(str((commitment - start).total_seconds()))
    if seconds < 0:
        raise ValueError("sale.order.commitment_date cannot precede date_order")

    return OrderAcceptanceView(
        record_ref=order_ref,
        facts=OrderAcceptanceFacts(
            requested_quantity=sum(quantities, Decimal(0)),
            unit_list_price=Decimal(str(product["list_price"])),
            pretax_budget=pretax_budget,
            lead_days=seconds / Decimal(86400),
            fact_provenance={
                "requested_quantity": tuple(
                    FactProvenance(
                        source_ref=line_lineage[line_id][0],
                        field_path="/product_uom_qty",
                        revision=line_lineage[line_id][1],
                        fingerprint=line_lineage[line_id][2],
                        source_kind="odoo_record",
                    )
                    for line_id in sorted(line_rows)
                ),
                "unit_list_price": (
                    FactProvenance(
                        source_ref=product_lineage[product_id][0],
                        field_path="/list_price",
                        revision=product_lineage[product_id][1],
                        fingerprint=product_lineage[product_id][2],
                        source_kind="odoo_record",
                    ),
                ),
                "pretax_budget": (budget_provenance,),
                "lead_days": (
                    FactProvenance(
                        source_ref=order_ref,
                        field_path="/date_order",
                        revision=order_revision,
                        fingerprint=order_fingerprint,
                        source_kind="odoo_record",
                    ),
                    FactProvenance(
                        source_ref=order_ref,
                        field_path="/commitment_date",
                        revision=order_revision,
                        fingerprint=order_fingerprint,
                        source_kind="odoo_record",
                    ),
                ),
            },
        ),
    )


class OrderAcceptanceObligation(_FrontendModel):
    packet_id: str
    capability_id: str
    action_kind: str
    proposal_hash: str
    target_record_ref: str
    source_record_refs: tuple[str, ...]
    source_revision: str
    source_fingerprint: str
    facts: OrderAcceptanceFacts
    policy_id: str
    policy_hash: str
    configured_policy_refs: tuple[str, ...]


def order_acceptance_source(record: OrderAcceptanceView) -> SourceRecord:
    return SourceRecord(
        source_id=record.record_ref,
        content="",
        title=f"Order acceptance facts for {record.record_ref}",
        kind="record",
        provenance={
            "view_model": record.view_model,
            "fact_provenance": record.facts.structured_fields()["fact_provenance"],
        },
        record_model=record.view_model,
        record_revision=record.revision,
        structured_fields=record.facts.structured_fields(),
    )


def compile_order_acceptance(
    proposal: ActionProposal,
    records: OrderAcceptanceBatch,
    policy: OrderAcceptancePolicy,
) -> tuple[OrderAcceptanceObligation, ...]:
    schema = SALES_ORDER_ACCEPTANCE_PACK.capability(
        SALES_ORDER_ACCEPTANCE_CAPABILITY_ID
    )
    if not proposal.actions:
        raise ValueError("sales-order acceptance requires at least one action")
    if len(proposal.actions) != len(proposal.target_record_refs):
        raise ValueError("sales-order acceptance requires exactly one action per target")

    policy_excerpt = policy.to_policy_excerpt()
    configured_policy_refs = tuple(
        sorted(
            ref_id
            for ref_id, envelope in policy_excerpt["values"].items()
            if envelope["configured"] is True
        )
    )
    packets = []
    for action in proposal.actions:
        if action.action not in schema["action_kinds"]:
            raise ValueError(f"unsupported sales-order action: {action.action!r}")
        if action.arguments:
            raise ValueError(f"{action.action!r} does not accept arguments")
        record = records.record(action.record_ref)
        if record is None:
            raise ValueError(f"target is absent from acceptance facts: {action.record_ref!r}")
        if record.view_model != schema["record_model"]:
            raise ValueError(
                f"target {action.record_ref!r} must use view {schema['record_model']!r}"
            )
        expected_revision = proposal.expected_preconditions[action.record_ref].get(
            "upstream_revision"
        )
        if expected_revision != record.revision:
            raise ValueError(
                f"target {action.record_ref!r} revision does not match proposal precondition"
            )
        source = order_acceptance_source(record)
        packets.append(
            OrderAcceptanceObligation(
                packet_id=f"{proposal.proposal_id}:{action.record_ref}",
                capability_id=SALES_ORDER_ACCEPTANCE_CAPABILITY_ID,
                action_kind=action.action,
                proposal_hash=proposal.proposal_hash,
                target_record_ref=action.record_ref,
                source_record_refs=(action.record_ref,),
                source_revision=record.revision,
                source_fingerprint=hashlib.sha256(
                    source.content.encode("utf-8")
                ).hexdigest(),
                facts=record.facts,
                policy_id=policy.policy_id,
                policy_hash=policy.content_hash(),
                configured_policy_refs=configured_policy_refs,
            )
        )
    return tuple(packets)


def compile_order_acceptance_plan(
    packets: tuple[OrderAcceptanceObligation, ...],
) -> ProofPlan:
    if not packets:
        raise ValueError("sales-order acceptance requires at least one obligation")
    first = packets[0]
    shared_fields = (
        "capability_id",
        "proposal_hash",
        "policy_id",
        "policy_hash",
        "configured_policy_refs",
    )
    if any(
        getattr(packet, field) != getattr(first, field)
        for packet in packets[1:]
        for field in shared_fields
    ):
        raise ValueError("obligations do not belong to one registered review run")
    packet_ids = [packet.packet_id for packet in packets]
    target_refs = [packet.target_record_ref for packet in packets]
    if len(packet_ids) != len(set(packet_ids)) or len(target_refs) != len(
        set(target_refs)
    ):
        raise ValueError("obligation ids and targets must be unique")

    schema = SALES_ORDER_ACCEPTANCE_PACK.capability(first.capability_id)
    predicate_program = SALES_ORDER_ACCEPTANCE_PACK.capability_predicate_program(
        first.capability_id,
        configured_policy_refs=set(first.configured_policy_refs),
    )
    contracts = [
        RegisteredActionContract(
            contract_id=packet.packet_id,
            capability_id=packet.capability_id,
            action_kind=packet.action_kind,
            target_record_ref=packet.target_record_ref,
            proposal_hash=packet.proposal_hash,
            policy_hash=packet.policy_hash,
            requirement_pack_hash=SALES_ORDER_ACCEPTANCE_PACK.content_hash,
            source_refs=list(packet.source_record_refs),
            source_fingerprints={
                packet.target_record_ref: packet.source_fingerprint
            },
            source_revisions={packet.target_record_ref: packet.source_revision},
            predicate_program=predicate_program,
            terminal_relations=dict(schema["terminal_relations"]),
        )
        for packet in packets
    ]
    return SALES_ORDER_ACCEPTANCE_PACK.lower_registered_action_plan(
        contracts,
        configured_policy_refs=set(first.configured_policy_refs),
    )


__all__ = [
    "FACT_VIEW_MODEL",
    "ODOO_READ_FIELDS",
    "ODOO_READ_SCHEMA",
    "SALES_ORDER_ACCEPTANCE_CAPABILITY_ID",
    "FactProvenance",
    "OrderAcceptanceBatch",
    "OrderAcceptanceFacts",
    "OrderAcceptanceObligation",
    "OrderAcceptancePolicy",
    "OrderAcceptanceView",
    "admit_order_acceptance_reads",
    "compile_order_acceptance",
    "compile_order_acceptance_plan",
    "order_acceptance_source",
]
