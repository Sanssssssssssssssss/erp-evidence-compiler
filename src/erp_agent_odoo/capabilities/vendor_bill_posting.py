from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Literal

from app.compiler_runtime.models import (
    ActionProposal,
    ProofPlan,
    RegisteredActionContract,
)
from app.compiler_runtime.policy import policy_hash
from app.compiler_runtime.requirement_pack import VENDOR_BILL_POSTING_PACK
from app.compiler_runtime.sandbox import SourceRecord
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .provenance import FactProvenance

FACT_VIEW_MODEL = "derived.vendor_bill_posting_facts"
CAPABILITY_ID = "odoo.vendor_bill_posting.v1"
_FACT_FIELDS = (
    "ordered_quantity",
    "received_quantity",
    "purchase_untaxed_total",
    "bill_untaxed_total",
    "document_untaxed_total",
    "vendor_identity_match",
    "document_reference_match",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VendorBillPostingFacts(_Model):
    ordered_quantity: Decimal
    received_quantity: Decimal
    purchase_untaxed_total: Decimal
    bill_untaxed_total: Decimal
    document_untaxed_total: Decimal
    vendor_identity_match: Decimal
    document_reference_match: Decimal
    fact_provenance: dict[str, tuple[FactProvenance, ...]]

    @field_validator(*_FACT_FIELDS, mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise TypeError("fact numbers must use decimal strings")
        return value

    @model_validator(mode="after")
    def validate_facts(self) -> VendorBillPostingFacts:
        values = [getattr(self, field) for field in _FACT_FIELDS]
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("vendor bill facts must be finite and non-negative")
        if self.vendor_identity_match not in {Decimal(0), Decimal(1)} or (
            self.document_reference_match not in {Decimal(0), Decimal(1)}
        ):
            raise ValueError("match facts must be canonical 0 or 1")
        if set(self.fact_provenance) != set(_FACT_FIELDS) or any(
            not sources for sources in self.fact_provenance.values()
        ):
            raise ValueError("fact_provenance must exactly cover every vendor bill fact")
        return self

    def structured_fields(self) -> dict[str, Any]:
        return {
            **{field: str(getattr(self, field)) for field in _FACT_FIELDS},
            "fact_provenance": {
                field: [item.model_dump(mode="json") for item in sources]
                for field, sources in sorted(self.fact_provenance.items())
            },
        }


class VendorBillPostingView(_Model):
    record_ref: str
    facts: VendorBillPostingFacts
    view_model: Literal["derived.vendor_bill_posting_facts"] = FACT_VIEW_MODEL

    @field_validator("record_ref")
    @classmethod
    def require_record_ref(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("vendor bill target reference must not be empty")
        return value

    @property
    def revision(self) -> str:
        return policy_hash(
            {
                field: [item.model_dump(mode="json") for item in sources]
                for field, sources in sorted(self.facts.fact_provenance.items())
            }
        )


class VendorBillPostingPolicy(_Model):
    policy_id: str
    required_match_flag: Decimal = Decimal(1)

    @field_validator("policy_id")
    @classmethod
    def require_policy_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("policy_id must not be empty")
        return value

    @model_validator(mode="after")
    def require_canonical_match_flag(self) -> VendorBillPostingPolicy:
        if self.required_match_flag != Decimal(1):
            raise ValueError("required_match_flag must be 1")
        return self

    def to_policy_excerpt(self) -> dict[str, Any]:
        return {
            "policy_version": "runtime-admitted",
            "policy_basis": {"policy_id": self.policy_id},
            "values": {
                "required_match_flag": {
                    "configured": True,
                    "value": str(self.required_match_flag),
                }
            },
        }

    def content_hash(self) -> str:
        return policy_hash(self.to_policy_excerpt())


class VendorBillPostingObligation(_Model):
    packet_id: str
    capability_id: str
    action_kind: str
    proposal_hash: str
    target_record_ref: str
    source_revision: str
    source_fingerprint: str
    policy_hash: str


def vendor_bill_posting_source(view: VendorBillPostingView) -> SourceRecord:
    return SourceRecord(
        source_id=view.record_ref,
        title=f"Vendor bill posting facts for {view.record_ref}",
        kind="record",
        content="",
        provenance={
            "view_model": view.view_model,
            "fact_provenance": view.facts.structured_fields()["fact_provenance"],
        },
        record_model=view.view_model,
        record_revision=view.revision,
        structured_fields=view.facts.structured_fields(),
    )


def compile_vendor_bill_posting(
    proposal: ActionProposal,
    view: VendorBillPostingView,
    policy: VendorBillPostingPolicy,
) -> VendorBillPostingObligation:
    schema = VENDOR_BILL_POSTING_PACK.capability(CAPABILITY_ID)
    if len(proposal.actions) != 1 or proposal.target_record_refs != [view.record_ref]:
        raise ValueError("vendor bill posting requires one action for the admitted bill")
    action = proposal.actions[0]
    if action.record_ref != view.record_ref or action.action not in schema["action_kinds"]:
        raise ValueError("vendor bill posting action is not registered for this bill")
    if action.arguments:
        raise ValueError("vendor bill posting actions do not accept arguments")
    expected_revision = proposal.expected_preconditions[view.record_ref].get(
        "upstream_revision"
    )
    if expected_revision != view.revision:
        raise ValueError("vendor bill posting revision does not match proposal precondition")
    source = vendor_bill_posting_source(view)
    return VendorBillPostingObligation(
        packet_id=f"{proposal.proposal_id}:{view.record_ref}",
        capability_id=CAPABILITY_ID,
        action_kind=action.action,
        proposal_hash=proposal.proposal_hash,
        target_record_ref=view.record_ref,
        source_revision=view.revision,
        source_fingerprint=hashlib.sha256(source.content.encode()).hexdigest(),
        policy_hash=policy.content_hash(),
    )


def compile_vendor_bill_posting_plan(
    obligation: VendorBillPostingObligation,
) -> ProofPlan:
    schema = VENDOR_BILL_POSTING_PACK.capability(obligation.capability_id)
    contract = RegisteredActionContract(
        contract_id=obligation.packet_id,
        capability_id=obligation.capability_id,
        action_kind=obligation.action_kind,
        target_record_ref=obligation.target_record_ref,
        proposal_hash=obligation.proposal_hash,
        policy_hash=obligation.policy_hash,
        requirement_pack_hash=VENDOR_BILL_POSTING_PACK.content_hash,
        source_refs=[obligation.target_record_ref],
        source_fingerprints={
            obligation.target_record_ref: obligation.source_fingerprint
        },
        source_revisions={obligation.target_record_ref: obligation.source_revision},
        predicate_program=VENDOR_BILL_POSTING_PACK.capability_predicate_program(
            obligation.capability_id,
            configured_policy_refs={"required_match_flag"},
        ),
        terminal_relations=dict(schema["terminal_relations"]),
    )
    return VENDOR_BILL_POSTING_PACK.lower_registered_action_plan(
        [contract], configured_policy_refs={"required_match_flag"}
    )


__all__ = [
    "CAPABILITY_ID",
    "FACT_VIEW_MODEL",
    "VendorBillPostingFacts",
    "VendorBillPostingObligation",
    "VendorBillPostingPolicy",
    "VendorBillPostingView",
    "compile_vendor_bill_posting",
    "compile_vendor_bill_posting_plan",
    "vendor_bill_posting_source",
]
