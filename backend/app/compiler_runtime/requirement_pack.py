from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from app.proof_schema import ProofSignature

from .models import (
    ProofNode,
    ProofPlan,
    RegisteredActionContract,
    RegisteredPredicateProgram,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
AURORA_AP_POLICY_PATH = PROJECT_ROOT / "policies" / "aurora_ap_policy_v1.json"
ODOO_ERP_ACTION_PLAN_POLICY_PATH = PROJECT_ROOT / "policies" / "odoo_erp_action_plan_v1.json"
SALES_ORDER_ACCEPTANCE_POLICY_PATH = (
    PROJECT_ROOT / "policies" / "erp_bench_sales_order_acceptance_v1.json"
)
VENDOR_BILL_POSTING_POLICY_PATH = (
    PROJECT_ROOT / "policies" / "odoo_vendor_bill_posting_v1.json"
)
_KINDS = {"document", "field", "cross_check", "visual", "risk_check"}
_OWNERS = {"evidence", "reviewer", "compiler"}
_HINT_ACTIVATIONS = {"explicit", "derived"}
_HINT_FIELDS = {"activation", "activation_requirement_groups", "capability", "target_predicate"}
_REQUIRED_CAPABILITY_FIELDS = {
    "action_kinds",
    "record_model",
    "record_fields",
    "predicate_program",
    "terminal_relations",
    "requirement_id",
    "facet_ref",
}
_CAPABILITY_FIELDS = _REQUIRED_CAPABILITY_FIELDS | {"optional_policy_values"}


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RequirementPack:
    pack_id: str
    version: str
    requirements: dict[str, dict[str, Any]]
    proof_signatures: tuple[ProofSignature, ...]
    policy: dict[str, Any]
    planning_hints: dict[str, dict[str, Any]]
    content_hash: str
    profiles: dict[str, list[dict[str, Any]]]
    unconfigured_policy_values: frozenset[str]
    raw: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> "RequirementPack":
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")), source=str(path))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source: str = "requirement pack",
    ) -> "RequirementPack":
        raw = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
        _validate_pack(raw, source=source)
        requirements = raw["requirements"]
        unconfigured = frozenset(str(item) for item in raw.get("unconfigured_policy_values") or [])
        policy_refs = {
            str(ref)
            for definition in requirements.values()
            for ref in definition.get("required_policy_values") or []
        }
        policy_refs.update(
            str(ref)
            for capability in (raw.get("capabilities") or {}).values()
            for ref in capability.get("optional_policy_values") or []
        )
        return cls(
            pack_id=str(raw["pack_id"]),
            version=str(raw["version"]),
            requirements=requirements,
            proof_signatures=tuple(
                ProofSignature.model_validate(item) for item in raw.get("proof_signatures") or []
            ),
            policy={
                "policy_version": str(raw.get("policy_version") or ""),
                "policy_basis": dict(raw.get("policy_basis") or {}),
                "values": {
                    ref: (
                        {"configured": False, "value": None}
                        if ref in unconfigured or ref not in raw
                        else {"configured": True, "value": raw[ref]}
                    )
                    for ref in sorted(policy_refs)
                },
            },
            planning_hints=dict(raw.get("planning_hints") or {}),
            content_hash=_stable_hash(raw),
            profiles=dict(raw["profiles"]),
            unconfigured_policy_values=unconfigured,
            raw=raw,
        )

    def definition(self, requirement_id: str) -> dict[str, Any]:
        return copy.deepcopy(
            self.requirements.get(str(requirement_id or "").strip()) or {}
        )

    def assert_integrity(self) -> None:
        if self.content_hash != _stable_hash(self.raw):
            raise ValueError(f"Requirement pack content changed: {self.pack_id!r}")
        if self != RequirementPack.from_mapping(self.raw):
            raise ValueError(f"Requirement pack derived state changed: {self.pack_id!r}")

    def default_required(self, requirement_id: str) -> bool:
        definition = self.definition(requirement_id)
        return bool(definition.get("default_required", True)) if definition else True

    def evidence_type(self, requirement_id: str) -> str:
        return str(self.definition(requirement_id).get("evidence_type") or "").strip()

    def kind(self, requirement_id: str) -> str:
        return str(self.definition(requirement_id).get("kind") or "field")

    def premises(self, requirement_id: str) -> tuple[str, ...]:
        return tuple(str(item) for item in self.definition(requirement_id).get("premise_requirements") or [])

    def signature_for(self, requirement_id: str) -> ProofSignature | None:
        normalized = str(requirement_id or "").strip()
        signature = next(
            (item for item in self.proof_signatures if item.requirement_id == normalized),
            None,
        )
        return (
            ProofSignature.model_validate(signature.model_dump(mode="json"))
            if signature is not None
            else None
        )

    def capability(self, capability_id: str) -> dict[str, Any]:
        normalized = str(capability_id or "").strip()
        self.assert_integrity()
        capability = (self.raw.get("capabilities") or {}).get(normalized)
        if capability is None:
            raise ValueError(f"Capability is not registered by {self.pack_id!r}: {normalized!r}")
        return copy.deepcopy(capability)

    def capability_requirement_ids(self) -> frozenset[str]:
        self.assert_integrity()
        return frozenset(
            str(item["requirement_id"])
            for item in (self.raw.get("capabilities") or {}).values()
        )

    def capability_predicate_program(
        self,
        capability_id: str,
        *,
        configured_policy_refs: set[str] | None = None,
    ) -> RegisteredPredicateProgram:
        capability = self.capability(capability_id)
        program = RegisteredPredicateProgram.model_validate(capability["predicate_program"])
        if configured_policy_refs is None:
            return program

        requirement_id = str(capability["requirement_id"])
        signature = self.signature_for(requirement_id)
        if signature is None:
            raise ValueError(f"Registered capability has no ProofSignature: {capability_id!r}")
        missing = sorted(set(signature.required_policy_refs) - configured_policy_refs)
        if missing:
            raise ValueError(f"Required capability policies are not configured: {missing}")

        disabled = set(capability.get("optional_policy_values") or []) - configured_policy_refs
        removed: set[str] = set()
        candidate_steps = []
        for step in program.steps:
            if any(
                (operand.kind == "POLICY" and operand.ref_id in disabled)
                or (operand.kind == "STEP" and operand.ref_id in removed)
                for operand in step.operands
            ):
                removed.add(step.step_id)
            else:
                candidate_steps.append(step)
        predicate_refs = [
            step_id
            for step_id in program.outcome.predicate_refs
            if step_id not in removed
        ]
        if not predicate_refs:
            raise ValueError("Configured capability has no outcome predicates")
        reachable = set(predicate_refs)
        for step in reversed(candidate_steps):
            if step.step_id in reachable:
                reachable.update(
                    operand.ref_id
                    for operand in step.operands
                    if operand.kind == "STEP"
                )
        return RegisteredPredicateProgram.model_validate(
            {
                "steps": [
                    step.model_dump(mode="json")
                    for step in candidate_steps
                    if step.step_id in reachable
                ],
                "outcome": {
                    **program.outcome.model_dump(mode="json"),
                    "predicate_refs": predicate_refs,
                },
            }
        )

    def lower_registered_action_plan(
        self,
        contracts: Sequence[RegisteredActionContract],
        *,
        configured_policy_refs: set[str] | None = None,
    ) -> ProofPlan:
        if not contracts:
            raise ValueError("Registered action plan requires at least one contract")
        capability_ids = {item.capability_id for item in contracts}
        proposal_hashes = {item.proposal_hash for item in contracts}
        if len(capability_ids) != 1 or len(proposal_hashes) != 1:
            raise ValueError("Registered action plan requires one capability and proposal")
        capability_id = next(iter(capability_ids))
        proposal_hash = next(iter(proposal_hashes))
        capability = self.capability(capability_id)
        requirement_id = str(capability["requirement_id"])
        signature = self.signature_for(requirement_id)
        if signature is None:
            raise ValueError(f"Registered capability has no ProofSignature: {capability_id!r}")
        predicate_program = self.capability_predicate_program(
            capability_id,
            configured_policy_refs=configured_policy_refs,
        )
        policy_refs = sorted(
            {
                operand.ref_id
                for step in predicate_program.steps
                for operand in step.operands
                if operand.kind == "POLICY"
            }
        )

        checks = []
        for contract in contracts:
            # ponytail: atomic action contracts are target-record-only until a
            # registered capability demonstrates a real cross-record dependency.
            if contract.source_refs != [contract.target_record_ref]:
                raise ValueError("Registered action contract must use its target source only")
            if contract.action_kind not in capability["action_kinds"]:
                raise ValueError(f"Action is not registered by {capability_id!r}")
            if contract.predicate_program != predicate_program:
                raise ValueError(
                    f"Predicate program does not match registered capability {capability_id!r}"
                )
            if contract.terminal_relations != capability["terminal_relations"]:
                raise ValueError(
                    f"Terminal relations do not match registered capability {capability_id!r}"
                )
            if contract.requirement_pack_hash != self.content_hash:
                raise ValueError("Registered action contract requirement pack changed")
            checks.append(
                ProofNode(
                    id=f"check:{contract.contract_id}",
                    kind="CHECK",
                    statement=(
                        f"The proposed {contract.action_kind} action for "
                        f"{contract.target_record_ref} matches the registered capability policy."
                    ),
                    requirement_refs=[requirement_id],
                    policy_refs=policy_refs,
                    facet_refs=[str(capability["facet_ref"])],
                    action_contract=contract,
                )
            )

        root_id = checks[0].id
        nodes = list(checks)
        if len(checks) > 1:
            root_id = f"all:{proposal_hash[:16]}"
            nodes.append(
                ProofNode(
                    id=root_id,
                    kind="ALL",
                    depends_on=[check.id for check in checks],
                )
            )
        return ProofPlan(
            plan_id=f"plan:{capability_id}:{proposal_hash[:16]}",
            objective=(
                f"Review each proposed action under the registered {capability_id} capability."
            ),
            active_requirement_ids=[requirement_id],
            policy_refs=policy_refs,
            roots={requirement_id: root_id},
            nodes=nodes,
        )

    def signature_hash_for(self, requirement_ids: Sequence[str]) -> str:
        active = {str(item or "").strip() for item in requirement_ids}
        return _stable_hash(
            [
                item.model_dump(mode="json")
                for item in sorted(self.proof_signatures, key=lambda value: value.requirement_id)
                if item.requirement_id in active
            ]
        )

    def expand(self, requirement_ids: Sequence[str]) -> list[str]:
        active = list(dict.fromkeys(str(item).strip() for item in requirement_ids if str(item).strip()))
        unknown = sorted(set(active) - set(self.requirements))
        if unknown:
            raise ValueError(f"Requirements are not declared by {self.pack_id!r}: {unknown}")
        active_set = set(active)
        changed = True
        while changed:
            changed = False
            for requirement_id in list(active):
                for premise_id in self.premises(requirement_id):
                    if premise_id not in active_set:
                        active.append(premise_id)
                        active_set.add(premise_id)
                        changed = True
            for requirement_id, hint in self.planning_hints.items():
                if hint.get("activation") != "derived" or requirement_id in active_set:
                    continue
                groups = hint.get("activation_requirement_groups") or []
                if groups and all(any(str(item) in active_set for item in group) for group in groups):
                    active.append(requirement_id)
                    active_set.add(requirement_id)
                    changed = True
        return active

    def required_policy_refs(self, requirement_ids: Sequence[str]) -> set[str]:
        return {
            str(ref)
            for requirement_id in requirement_ids
            for ref in self.definition(requirement_id).get("required_policy_values") or []
        }

    def policy_excerpt_for(self, requirement_ids: Sequence[str]) -> dict[str, Any]:
        refs = self.required_policy_refs(requirement_ids)
        refs.update(
            str(ref)
            for capability in (self.raw.get("capabilities") or {}).values()
            if str(capability["requirement_id"]) in requirement_ids
            for ref in capability.get("optional_policy_values") or []
        )
        return {
            "policy_version": self.policy["policy_version"],
            "policy_basis": dict(self.policy["policy_basis"]),
            "values": {ref: self.policy["values"][ref] for ref in sorted(refs)},
        }

    def requirement_context(self, requirement_ids: Sequence[str]) -> list[dict[str, Any]]:
        result = []
        for requirement_id in requirement_ids:
            definition = self.definition(requirement_id)
            hint = self.planning_hints.get(requirement_id) or {}
            label = str(definition.get("label") or requirement_id.replace("_", " "))
            result.append(
                {
                    "id": requirement_id,
                    "label": label,
                    "proof_target": {"requirement_id": requirement_id, "label": label},
                    "kind": self.kind(requirement_id),
                    "owner": str(definition.get("owner") or "evidence"),
                    "required": self.default_required(requirement_id),
                    "premise_requirements": list(definition.get("premise_requirements") or []),
                    "required_policy_values": list(definition.get("required_policy_values") or []),
                    "capability_hint": str(hint.get("capability") or ""),
                    "target_predicate_hint": str(hint.get("target_predicate") or ""),
                }
            )
        return result


def _validate_pack(pack: dict[str, Any], *, source: str) -> None:
    requirements = pack.get("requirements")
    profiles = pack.get("profiles")
    if not isinstance(requirements, dict) or not isinstance(profiles, dict):
        raise ValueError(f"Invalid requirement pack: {source}")
    if not str(pack.get("pack_id") or "").strip() or not str(pack.get("version") or "").strip():
        raise ValueError("Invalid requirement pack identity")
    if not str(pack.get("requirement_pack_version") or "").strip():
        raise ValueError("Invalid requirement pack version")
    planning_hints = pack.get("planning_hints") or {}
    if not isinstance(planning_hints, dict):
        raise ValueError("Invalid planning hints")
    capabilities = pack.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise ValueError("Invalid capabilities")
    raw_unconfigured = pack.get("unconfigured_policy_values") or []
    if not isinstance(raw_unconfigured, list) or any(not str(item).strip() for item in raw_unconfigured):
        raise ValueError("Invalid unconfigured_policy_values")
    unconfigured = {str(item) for item in raw_unconfigured}
    if unconfigured.intersection(pack):
        raise ValueError("Policy values cannot be both configured and unconfigured")
    for requirement_id, definition in requirements.items():
        if not isinstance(definition, dict) or definition.get("kind") not in _KINDS or definition.get("owner") not in _OWNERS:
            raise ValueError(f"Invalid requirement definition: {requirement_id}")
        premises = definition.get("premise_requirements") or []
        if not isinstance(premises, list) or any(str(item) not in requirements for item in premises):
            raise ValueError(f"Invalid requirement premises: {requirement_id}")
        if requirement_id in premises:
            raise ValueError(f"Self-referencing requirement premise: {requirement_id}")
        refs = definition.get("required_policy_values") or []
        if (
            not isinstance(refs, list)
            or any(not str(item).strip() for item in refs)
            or any(str(item) not in pack and str(item) not in unconfigured for item in refs)
        ):
            raise ValueError(f"Invalid requirement policy values: {requirement_id}")
        if definition.get("owner") == "reviewer" and any(
            requirements[str(item)].get("owner") != "evidence" for item in premises
        ):
            raise ValueError(f"Reviewer premises must be evidence-owned: {requirement_id}")
    raw_signatures = pack.get("proof_signatures") or []
    if not isinstance(raw_signatures, list):
        raise ValueError("Invalid proof signatures")
    signatures = [ProofSignature.model_validate(item) for item in raw_signatures]
    if len({item.signature_id for item in signatures}) != len(signatures) or len(
        {item.requirement_id for item in signatures}
    ) != len(signatures):
        raise ValueError("Duplicate proof signature")
    for signature in signatures:
        if signature.requirement_id not in requirements:
            raise ValueError(f"Invalid proof signature requirement: {signature.requirement_id}")
        declared = set(requirements[signature.requirement_id].get("required_policy_values") or [])
        if not set(signature.required_policy_refs).issubset(declared):
            raise ValueError(f"Invalid proof signature policy refs: {signature.signature_id}")
        if any(
            facet.required_policy_refs is not None
            and not set(facet.required_policy_refs).issubset(declared)
            for facet in signature.facets
        ):
            raise ValueError(f"Invalid proof facet policy refs: {signature.signature_id}")
    signatures_by_requirement = {item.requirement_id: item for item in signatures}
    for capability_id, capability in capabilities.items():
        if (
            not str(capability_id).strip()
            or not isinstance(capability, dict)
            or set(capability) - _CAPABILITY_FIELDS
            or not _REQUIRED_CAPABILITY_FIELDS.issubset(capability)
        ):
            raise ValueError(f"Invalid capability: {capability_id!r}")
        requirement_id = str(capability.get("requirement_id") or "").strip()
        signature = signatures_by_requirement.get(requirement_id)
        if signature is None:
            raise ValueError(f"Invalid capability requirement: {capability_id!r}")
        if str(capability.get("facet_ref") or "").strip() not in {
            facet.id for facet in signature.facets
        }:
            raise ValueError(f"Invalid capability facet: {capability_id!r}")
        for field_name in ("action_kinds", "record_fields"):
            values = capability.get(field_name)
            if (
                not isinstance(values, list)
                or not values
                or any(not str(item).strip() for item in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"Invalid capability {field_name}: {capability_id!r}")
        optional_policies = capability.get("optional_policy_values") or []
        if (
            not isinstance(optional_policies, list)
            or any(not str(item).strip() for item in optional_policies)
            or len(set(optional_policies)) != len(optional_policies)
            or any(
                str(item) not in pack and str(item) not in unconfigured
                for item in optional_policies
            )
            or set(optional_policies).intersection(signature.required_policy_refs)
        ):
            raise ValueError(f"Invalid capability optional_policy_values: {capability_id!r}")
        try:
            predicate_program = RegisteredPredicateProgram.model_validate(
                capability.get("predicate_program")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid capability predicate_program: {capability_id!r}"
            ) from exc
        record_fields = set(capability["record_fields"])
        unknown_fields = sorted(
            operand.ref_id
            for step in predicate_program.steps
            for operand in step.operands
            if operand.kind == "RECORD_FIELD"
            and operand.ref_id.removeprefix("/") not in record_fields
        )
        unknown_policies = sorted(
            operand.ref_id
            for step in predicate_program.steps
            for operand in step.operands
            if operand.kind == "POLICY"
            and operand.ref_id
            not in set(signature.required_policy_refs).union(optional_policies)
        )
        unused_optional_policies = sorted(
            set(optional_policies)
            - {
                operand.ref_id
                for step in predicate_program.steps
                for operand in step.operands
                if operand.kind == "POLICY"
            }
        )
        if unknown_fields or unknown_policies or unused_optional_policies:
            raise ValueError(
                f"Invalid capability predicate operands: {capability_id!r}; "
                f"fields={unknown_fields}, policies={unknown_policies}, "
                f"unused_optional_policies={unused_optional_policies}"
            )
        if {
            predicate_program.outcome.true_action,
            predicate_program.outcome.false_action,
        } - set(capability["action_kinds"]):
            raise ValueError(f"Invalid capability predicate outcome: {capability_id!r}")
        terminal_relations = capability.get("terminal_relations")
        if (
            not isinstance(terminal_relations, dict)
            or len(terminal_relations) != 2
            or any(not str(item).strip() for item in terminal_relations)
            or sorted(terminal_relations.values()) != ["CONTRADICTED", "SUPPORTED"]
        ):
            raise ValueError(f"Invalid capability terminal_relations: {capability_id!r}")
        if not str(capability.get("record_model") or "").strip():
            raise ValueError(f"Invalid capability contract: {capability_id!r}")
    for profile_id, rows in profiles.items():
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("id") not in requirements for row in rows
        ):
            raise ValueError(f"Invalid requirement profile: {profile_id}")
    for requirement_id, hint in planning_hints.items():
        if requirement_id not in requirements or not isinstance(hint, dict) or set(hint) - _HINT_FIELDS:
            raise ValueError(f"Invalid planning hint: {requirement_id}")
        if hint.get("activation", "explicit") not in _HINT_ACTIVATIONS:
            raise ValueError(f"Invalid planning hint activation: {requirement_id}")
        expected = "derived" if requirements[requirement_id].get("owner") == "compiler" else "explicit"
        if hint.get("activation", "explicit") != expected:
            raise ValueError(f"Invalid planning hint authority: {requirement_id}")
        if not str(hint.get("capability") or "").strip() or not str(hint.get("target_predicate") or "").strip():
            raise ValueError(f"Invalid planning hint target: {requirement_id}")
        groups = hint.get("activation_requirement_groups") or []
        if not isinstance(groups, list) or any(
            not isinstance(group, list)
            or not group
            or any(str(item) not in requirements or str(item) == requirement_id for item in group)
            for group in groups
        ):
            raise ValueError(f"Invalid planning hint activation requirements: {requirement_id}")
        if expected == "derived" and not groups:
            raise ValueError(f"Derived planning hint has no activation requirements: {requirement_id}")
    missing = {
        requirement_id
        for requirement_id, definition in requirements.items()
        if definition.get("owner") != "evidence" and requirement_id not in planning_hints
    }
    if missing:
        raise ValueError(f"Missing planning hints: {', '.join(sorted(missing))}")
    try:
        graph = {
            key: set(value.get("premise_requirements") or []).union(
                item
                for group in planning_hints.get(key, {}).get("activation_requirement_groups") or []
                for item in group
            )
            for key, value in requirements.items()
        }
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("Cyclic requirement premises") from exc


DEFAULT_REQUIREMENT_PACK = RequirementPack.from_path(AURORA_AP_POLICY_PATH)
ODOO_ERP_ACTION_PLAN_PACK = RequirementPack.from_path(ODOO_ERP_ACTION_PLAN_POLICY_PATH)
EVIDENCE_ACTION_REVIEW_PACK = RequirementPack.from_path(PROJECT_ROOT / "policies" / "evidence_action_review_v1.json")
SALES_ORDER_ACCEPTANCE_PACK = RequirementPack.from_path(
    SALES_ORDER_ACCEPTANCE_POLICY_PATH
)
VENDOR_BILL_POSTING_PACK = RequirementPack.from_path(
    VENDOR_BILL_POSTING_POLICY_PATH
)


def registered_requirement_pack(pack_id: str, version: str = "") -> RequirementPack:
    normalized_id = str(pack_id or "").strip()
    pack = {
        DEFAULT_REQUIREMENT_PACK.pack_id: DEFAULT_REQUIREMENT_PACK,
        ODOO_ERP_ACTION_PLAN_PACK.pack_id: ODOO_ERP_ACTION_PLAN_PACK,
        EVIDENCE_ACTION_REVIEW_PACK.pack_id: EVIDENCE_ACTION_REVIEW_PACK,
        SALES_ORDER_ACCEPTANCE_PACK.pack_id: SALES_ORDER_ACCEPTANCE_PACK,
        VENDOR_BILL_POSTING_PACK.pack_id: VENDOR_BILL_POSTING_PACK,
    }.get(normalized_id)
    if pack is None or (version and pack.version != str(version).strip()):
        raise ValueError(f"Unknown requirement pack: {pack_id!r} version {version!r}")
    return pack


__all__ = [
    "AURORA_AP_POLICY_PATH",
    "DEFAULT_REQUIREMENT_PACK",
    "ODOO_ERP_ACTION_PLAN_PACK",
    "ODOO_ERP_ACTION_PLAN_POLICY_PATH",
    "SALES_ORDER_ACCEPTANCE_PACK",
    "SALES_ORDER_ACCEPTANCE_POLICY_PATH",
    "VENDOR_BILL_POSTING_PACK",
    "VENDOR_BILL_POSTING_POLICY_PATH",
    "RequirementPack",
    "registered_requirement_pack",
]
