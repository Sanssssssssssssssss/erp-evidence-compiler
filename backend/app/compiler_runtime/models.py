from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.compiler_runtime.graph_walk import reachable_ids
from app.proof_schema import SemanticRole

NodeKind = Literal["CHECK", "ALL", "ANY"]
AssessmentStatus = Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]
CompileStatus = Literal["COMMITTED", "NON_CONVERGED", "INVALID", "CANCELLED"]
ExecutionStatus = Literal["COMPLETED", "PARTIAL", "FAILED"]
IntegrityStatus = Literal["VALID", "STALE", "INVALID"]
BusinessGapCode = Literal[
    "SOURCE_MISSING",
    "SOURCE_AMBIGUOUS",
    "BINDING_MISSING",
    "POLICY_UNCONFIGURED",
    "WITNESS_MISSING",
]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_text(value: str, *, field_name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _unique_strings(value: list[str], *, field_name: str) -> list[str]:
    result = [_require_text(item, field_name=field_name) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


class _CompilerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposedAction(_CompilerModel):
    action_id: str = ""
    stage: str = ""
    record_ref: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("record_ref", "action")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("action_id", "stage")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()


class ActionProposal(_CompilerModel):
    proposal_id: str
    actions: list[ProposedAction]
    target_record_refs: list[str]
    expected_preconditions: dict[str, dict[str, Any]]
    supersedes_proposal_id: str = ""
    proposal_hash: str = ""

    @field_validator("proposal_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_text(value, field_name="proposal_id")

    @field_validator("target_record_refs")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="target_record_refs")

    @model_validator(mode="after")
    def seal_and_validate(self) -> "ActionProposal":
        targets = set(self.target_record_refs)
        action_targets = [item.record_ref for item in self.actions]
        if len(action_targets) != len(set(action_targets)):
            raise ValueError("ActionProposal requires exactly one action per target")
        if len(action_targets) != len(self.target_record_refs) or set(action_targets) != targets:
            raise ValueError("ActionProposal actions must cover exactly target_record_refs")
        if set(self.expected_preconditions) != targets:
            raise ValueError("ActionProposal preconditions must cover exactly target_record_refs")
        expected_hash = self.content_hash()
        if self.proposal_hash and self.proposal_hash != expected_hash:
            raise ValueError("ActionProposal proposal_hash does not match its contents")
        self.proposal_hash = expected_hash
        return self

    def content_hash(self) -> str:
        return _stable_hash(self.model_dump(mode="json", exclude={"proposal_hash"}))


class ERPProposalRecord(_CompilerModel):
    """One immutable Manager-proposed record mutation, never an evidence Claim."""

    action_id: str
    stage: str = ""
    action_kind: str
    record_ref: str
    record_revision: str
    values: dict[str, Any]

    @field_validator("action_id", "action_kind", "record_ref", "record_revision")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("stage")
    @classmethod
    def normalize_stage(cls, value: str) -> str:
        return value.strip()


class ERPResolverEvidence(_CompilerModel):
    group_id: str
    source: Literal["MANAGER_PROPOSAL", "BOUND_SOURCE", "LIVE_ODOO", "UPSTREAM_CHECK"]
    method: Literal["STRUCTURED", "MODEL_QUOTE", "READ_AND_EXTRACT", "RUNTIME_TOOL", "UPSTREAM"]
    facts: list[str]
    source_role: str = ""
    upstream_checks: list[str] = Field(default_factory=list)

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: str) -> str:
        return _require_text(value, field_name="evidence group_id")

    @field_validator("facts")
    @classmethod
    def validate_facts(cls, value: list[str]) -> list[str]:
        result = _unique_strings(value, field_name="evidence facts")
        if not result:
            raise ValueError("evidence facts must not be empty")
        return result

    @model_validator(mode="after")
    def validate_acquisition(self) -> ERPResolverEvidence:
        allowed = {
            "MANAGER_PROPOSAL": {"STRUCTURED"},
            "BOUND_SOURCE": {"STRUCTURED", "MODEL_QUOTE", "READ_AND_EXTRACT"},
            "LIVE_ODOO": {"RUNTIME_TOOL"},
            "UPSTREAM_CHECK": {"UPSTREAM"},
        }
        if self.method not in allowed[self.source]:
            raise ValueError(f"{self.method} cannot acquire evidence from {self.source}")
        self.source_role = self.source_role.strip()
        if self.source == "BOUND_SOURCE" and not self.source_role:
            raise ValueError("BOUND_SOURCE evidence requires source_role")
        if self.source != "BOUND_SOURCE" and self.source_role:
            raise ValueError("source_role is only valid for BOUND_SOURCE evidence")
        if self.upstream_checks and self.source != "UPSTREAM_CHECK":
            raise ValueError("upstream_checks is only valid for UPSTREAM_CHECK evidence")
        return self


class RegisteredResolverProgram(_CompilerModel):
    resolver_id: str
    resolver_version: str = "1"
    tool_name: Literal["run_registered_check"] = "run_registered_check"
    evidence: list[ERPResolverEvidence]
    result_semantics: Literal["BOOLEAN_MUST_BE_TRUE"] = "BOOLEAN_MUST_BE_TRUE"

    @field_validator("resolver_id", "resolver_version")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[ERPResolverEvidence]) -> list[ERPResolverEvidence]:
        ids = [item.group_id for item in value]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("resolver evidence requires unique non-empty groups")
        return value


class ERPReviewContract(_CompilerModel):
    """Typed ERP CHECK contract retained from Task Compiler through Kernel."""

    contract_kind: Literal["ERP_CHECK"] = "ERP_CHECK"
    execution_mode: Literal["registered_resolver", "evidence_review"] = "registered_resolver"
    requires_calculation: bool = False
    contract_id: str = ""
    logical_check_id: str
    execution_check_instance_id: str = ""
    compiler_revision: int = Field(ge=1)
    template_id: str
    local_check_id: str
    owner_action_id: str
    stage: str = ""
    check_kind: Literal["action", "shared", "post_action"]
    capability_id: Literal["erp_action_plan_review"] = "erp_action_plan_review"
    action_kind: str
    target_record_refs: list[str] = Field(default_factory=list)
    proposal_records: list[ERPProposalRecord] = Field(default_factory=list)
    proposal_hash: str
    source_snapshot_hash: str
    policy_source_hash: str
    requirement_pack_hash: str
    source_refs: list[str]
    upstream_logical_check_ids: list[str] = Field(default_factory=list)
    resolver_program: RegisteredResolverProgram
    terminal_relations: dict[str, AssessmentStatus] = Field(
        default_factory=lambda: {
            "CHECK_SATISFIED": "SUPPORTED",
            "CHECK_VIOLATED": "CONTRADICTED",
        }
    )
    immutable_contract_hash: str = ""

    @field_validator(
        "logical_check_id",
        "template_id",
        "local_check_id",
        "owner_action_id",
        "action_kind",
        "proposal_hash",
        "source_snapshot_hash",
        "policy_source_hash",
        "requirement_pack_hash",
    )
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("stage")
    @classmethod
    def normalize_stage(cls, value: str) -> str:
        return value.strip()

    @field_validator("target_record_refs", "source_refs", "upstream_logical_check_ids")
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        result = _unique_strings(value, field_name=info.field_name)
        if info.field_name == "source_refs" and not result:
            raise ValueError("source_refs must not be empty")
        return result

    @model_validator(mode="after")
    def seal_contract(self) -> "ERPReviewContract":
        if self.check_kind == "action" and not self.proposal_records:
            raise ValueError("ERP action CHECK requires proposal records")
        if self.check_kind == "action" and set(self.target_record_refs) != {
            item.record_ref for item in self.proposal_records
        }:
            raise ValueError("ERP action CHECK proposal records must cover its targets")
        if sorted(self.terminal_relations.values()) != ["CONTRADICTED", "SUPPORTED"]:
            raise ValueError("ERP CHECK terminal relations must cover both strong statuses")
        body = self.model_dump(
            mode="json",
            exclude={
                "contract_id",
                "execution_check_instance_id",
                "immutable_contract_hash",
            },
        )
        # Legacy strict checkpoints keep their original seal; opting into source
        # review produces a different contract and cannot reinterpret old proof.
        if self.execution_mode == "registered_resolver" and not self.requires_calculation:
            body.pop("execution_mode")
            body.pop("requires_calculation")
        for group in body["resolver_program"]["evidence"]:
            if not group.get("upstream_checks"):
                group.pop("upstream_checks", None)
        expected_hash = _stable_hash(body)
        expected_instance_id = (
            f"check:r{self.compiler_revision}:{self.logical_check_id}:{expected_hash[:12]}"
        )
        if self.immutable_contract_hash and self.immutable_contract_hash != expected_hash:
            raise ValueError("ERP CHECK immutable_contract_hash does not match its body")
        if self.execution_check_instance_id and (
            self.execution_check_instance_id != expected_instance_id
        ):
            raise ValueError("ERP CHECK execution id does not match revision and contract")
        if self.contract_id and self.contract_id != expected_instance_id:
            raise ValueError("ERP CHECK contract_id must equal its execution id")
        self.immutable_contract_hash = expected_hash
        self.execution_check_instance_id = expected_instance_id
        self.contract_id = expected_instance_id
        return self


class ProofFreshness(_CompilerModel):
    status: IntegrityStatus
    reasons: list[str] = Field(default_factory=list)


class RegisteredPredicateOperand(_CompilerModel):
    kind: Literal["RECORD_FIELD", "POLICY", "STEP"]
    ref_id: str

    @field_validator("ref_id")
    @classmethod
    def validate_ref_id(cls, value: str) -> str:
        return _require_text(value, field_name="predicate operand ref_id")

    @model_validator(mode="after")
    def validate_record_field_pointer(self) -> RegisteredPredicateOperand:
        if self.kind == "RECORD_FIELD" and (
            not self.ref_id.startswith("/") or re.search(r"~(?:[^01]|$)", self.ref_id)
        ):
            raise ValueError("predicate RECORD_FIELD refs must be RFC 6901 JSON Pointers")
        return self


class RegisteredPredicateStep(_CompilerModel):
    step_id: str
    operation: Literal["MULTIPLY", "EQUAL", "GREATER_THAN", "GTE", "LTE"]
    operands: list[RegisteredPredicateOperand]

    @field_validator("step_id")
    @classmethod
    def validate_step_id(cls, value: str) -> str:
        return _require_text(value, field_name="predicate step_id")

    @model_validator(mode="after")
    def validate_arity(self) -> RegisteredPredicateStep:
        if len(self.operands) != 2:
            raise ValueError(f"{self.operation} predicate steps require exactly two operands")
        return self

    @property
    def returns_boolean(self) -> bool:
        return self.operation != "MULTIPLY"


class RegisteredPredicateOutcome(_CompilerModel):
    operator: Literal["ALL"]
    predicate_refs: list[str]
    true_action: str
    false_action: str

    @field_validator("predicate_refs")
    @classmethod
    def validate_predicate_refs(cls, value: list[str]) -> list[str]:
        result = _unique_strings(value, field_name="outcome predicate_refs")
        if not result:
            raise ValueError("outcome predicate_refs must not be empty")
        return result

    @field_validator("true_action", "false_action")
    @classmethod
    def validate_action(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_distinct_actions(self) -> RegisteredPredicateOutcome:
        if self.true_action == self.false_action:
            raise ValueError("predicate outcome actions must differ")
        return self


class RegisteredPredicateProgram(_CompilerModel):
    steps: list[RegisteredPredicateStep]
    outcome: RegisteredPredicateOutcome

    @model_validator(mode="after")
    def validate_graph(self) -> RegisteredPredicateProgram:
        step_ids = [step.step_id for step in self.steps]
        if not step_ids or len(step_ids) != len(set(step_ids)):
            raise ValueError("predicate program requires unique non-empty steps")
        seen: set[str] = set()
        referenced_steps: set[str] = set()
        for step in self.steps:
            for operand in step.operands:
                if operand.kind != "STEP":
                    continue
                if operand.ref_id not in seen:
                    raise ValueError("predicate STEP operands must reference an earlier step")
                referenced_steps.add(operand.ref_id)
            seen.add(step.step_id)
        by_id = {step.step_id: step for step in self.steps}
        unknown = sorted(set(self.outcome.predicate_refs) - set(by_id))
        if unknown:
            raise ValueError(f"predicate outcome references unknown steps: {unknown}")
        non_boolean = sorted(
            step_id
            for step_id in self.outcome.predicate_refs
            if not by_id[step_id].returns_boolean
        )
        if non_boolean:
            raise ValueError(f"predicate outcome references non-boolean steps: {non_boolean}")
        unused = sorted(
            set(by_id) - set(self.outcome.predicate_refs) - referenced_steps
        )
        if unused:
            raise ValueError(f"predicate program contains unused steps: {unused}")
        return self


class RegisteredActionContract(_CompilerModel):
    contract_id: str
    capability_id: str
    action_kind: str
    target_record_ref: str
    proposal_hash: str
    policy_hash: str
    requirement_pack_hash: str
    source_refs: list[str]
    source_fingerprints: dict[str, str]
    source_revisions: dict[str, str]
    predicate_program: RegisteredPredicateProgram
    terminal_relations: dict[str, AssessmentStatus]

    @field_validator(
        "contract_id",
        "capability_id",
        "action_kind",
        "target_record_ref",
        "proposal_hash",
        "policy_hash",
        "requirement_pack_hash",
    )
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("source_refs")
    @classmethod
    def validate_lists(cls, value: list[str], info: Any) -> list[str]:
        result = _unique_strings(value, field_name=info.field_name)
        if not result:
            raise ValueError(f"{info.field_name} must not be empty")
        return result

    @field_validator("terminal_relations")
    @classmethod
    def validate_terminal_relations(
        cls, value: dict[str, AssessmentStatus]
    ) -> dict[str, AssessmentStatus]:
        if not value:
            raise ValueError("terminal_relations must not be empty")
        normalized = {
            _require_text(relation, field_name="terminal relation"): status
            for relation, status in value.items()
        }
        if sorted(normalized.values()) != ["CONTRADICTED", "SUPPORTED"]:
            raise ValueError(
                "terminal_relations must define exactly one SUPPORTED and one CONTRADICTED relation"
            )
        return normalized

    @model_validator(mode="after")
    def validate_source_identity(self) -> RegisteredActionContract:
        source_refs = set(self.source_refs)
        if self.target_record_ref not in source_refs:
            raise ValueError("target_record_ref must be included in source_refs")
        if set(self.source_fingerprints) != source_refs:
            raise ValueError("source_fingerprints must exactly cover source_refs")
        if set(self.source_revisions) != source_refs:
            raise ValueError("source_revisions must exactly cover source_refs")
        self.source_fingerprints = {
            source_id: _require_text(value, field_name="source fingerprint")
            for source_id, value in self.source_fingerprints.items()
        }
        self.source_revisions = {
            source_id: _require_text(value, field_name="source revision")
            for source_id, value in self.source_revisions.items()
        }
        return self


class ProofNode(_CompilerModel):
    id: str
    kind: NodeKind
    statement: str = ""
    depends_on: list[str] = Field(default_factory=list)
    upstream_check_ids: list[str] = Field(default_factory=list)
    requirement_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    facet_refs: list[str] = Field(default_factory=list)
    semantic_role_refs: list[SemanticRole] = Field(default_factory=list)
    action_contract: RegisteredActionContract | ERPReviewContract | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_text(value, field_name="node id")

    @field_validator(
        "depends_on",
        "upstream_check_ids",
        "requirement_refs",
        "policy_refs",
        "facet_refs",
        "semantic_role_refs",
    )
    @classmethod
    def validate_references(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_shape(self) -> ProofNode:
        self.statement = self.statement.strip()
        if self.kind == "CHECK":
            if not self.statement:
                raise ValueError(f"CHECK node {self.id!r} requires a statement")
            if self.depends_on:
                raise ValueError(f"CHECK node {self.id!r} cannot have status dependencies")
            if not self.requirement_refs:
                raise ValueError(f"CHECK node {self.id!r} requires at least one requirement ref")
            return self

        if self.action_contract is not None:
            raise ValueError(f"{self.kind} node {self.id!r} cannot contain an action contract")
        if self.statement:
            raise ValueError(f"{self.kind} node {self.id!r} cannot contain a check statement")
        if self.upstream_check_ids:
            raise ValueError(f"{self.kind} node {self.id!r} cannot consume upstream CHECK outputs")
        if self.requirement_refs or self.policy_refs or self.facet_refs or self.semantic_role_refs:
            raise ValueError(
                f"{self.kind} node {self.id!r} cannot contain requirement, policy, facet, or semantic role refs"
            )
        if not self.depends_on:
            raise ValueError(f"{self.kind} node {self.id!r} requires at least one dependency")
        return self


class ProofPlan(_CompilerModel):
    plan_id: str
    version: str = "1"
    objective: str
    active_requirement_ids: list[str]
    policy_refs: list[str] = Field(default_factory=list)
    roots: dict[str, str]
    nodes: list[ProofNode]

    @field_validator("plan_id", "version", "objective")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("active_requirement_ids", "policy_refs")
    @classmethod
    def validate_declared_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_graph(self) -> ProofPlan:
        if not self.active_requirement_ids:
            raise ValueError("ProofPlan requires at least one active requirement")
        if not self.nodes:
            raise ValueError("ProofPlan requires at least one node")

        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("ProofPlan node ids must be unique")
        nodes = {node.id: node for node in self.nodes}

        declared_requirements = set(self.active_requirement_ids)
        root_requirements = set(self.roots)
        if root_requirements != declared_requirements:
            missing = sorted(declared_requirements - root_requirements)
            extra = sorted(root_requirements - declared_requirements)
            raise ValueError(f"ProofPlan roots must exactly cover active requirements; missing={missing}, extra={extra}")
        blank_root_keys = [key for key, value in self.roots.items() if not key.strip() or not value.strip()]
        if blank_root_keys:
            raise ValueError("ProofPlan root requirement ids and node ids must not be empty")
        unknown_roots = sorted(set(self.roots.values()) - set(nodes))
        if unknown_roots:
            raise ValueError(f"ProofPlan roots reference unknown nodes: {unknown_roots}")

        unknown_dependencies = sorted({item for node in self.nodes for item in node.depends_on if item not in nodes})
        if unknown_dependencies:
            raise ValueError(f"ProofPlan contains unknown dependencies: {unknown_dependencies}")
        unknown_upstream_checks = sorted(
            {
                upstream_id
                for node in self.nodes
                for upstream_id in node.upstream_check_ids
                if upstream_id not in nodes
            }
        )
        if unknown_upstream_checks:
            raise ValueError(
                f"ProofPlan references unknown upstream CHECKs: {unknown_upstream_checks}"
            )
        invalid_upstream_checks = sorted(
            (node.id, upstream_id)
            for node in self.nodes
            if node.kind == "CHECK"
            for upstream_id in node.upstream_check_ids
            if nodes[upstream_id].kind != "CHECK"
        )
        if invalid_upstream_checks:
            raise ValueError(
                "upstream_check_ids must reference CHECK nodes: "
                f"{invalid_upstream_checks}"
            )
        try:
            tuple(
                TopologicalSorter(
                    {
                        node.id: set(node.depends_on) | set(node.upstream_check_ids)
                        for node in self.nodes
                    }
                ).static_order()
            )
        except CycleError as exc:
            raise ValueError("ProofPlan must be acyclic") from exc

        referenced_policies = {
            policy for node in self.nodes if node.kind == "CHECK" for policy in node.policy_refs
        }
        if not self.policy_refs and referenced_policies:
            self.policy_refs = sorted(referenced_policies)
        declared_policies = set(self.policy_refs)
        for node in self.nodes:
            unknown_requirements = sorted(set(node.requirement_refs) - declared_requirements)
            if unknown_requirements:
                raise ValueError(f"CHECK node {node.id!r} references inactive requirements: {unknown_requirements}")
            unknown_policies = sorted(set(node.policy_refs) - declared_policies)
            if unknown_policies:
                raise ValueError(f"CHECK node {node.id!r} references undeclared policies: {unknown_policies}")

        reachable_by_requirement = {
            requirement_id: reachable_ids(root_id, lambda node_id: nodes[node_id].depends_on)
            for requirement_id, root_id in self.roots.items()
        }
        reachable = set().union(*reachable_by_requirement.values())
        disconnected = sorted(set(nodes) - reachable)
        if disconnected:
            raise ValueError(f"ProofPlan contains nodes that do not lead to a requirement root: {disconnected}")

        for requirement_id, requirement_node_ids in reachable_by_requirement.items():
            covered = any(
                requirement_id in nodes[node_id].requirement_refs
                for node_id in requirement_node_ids
                if nodes[node_id].kind == "CHECK"
            )
            if not covered:
                raise ValueError(f"Requirement {requirement_id!r} is not covered by a CHECK below its root")

        for node in self.nodes:
            if node.kind != "CHECK":
                continue
            for requirement_id in node.requirement_refs:
                if node.id not in reachable_by_requirement[requirement_id]:
                    raise ValueError(
                        f"CHECK node {node.id!r} references requirement {requirement_id!r} "
                        "but is not reachable from that requirement root"
                    )

        covered_policies = {policy for node in self.nodes for policy in node.policy_refs}
        missing_policies = sorted(declared_policies - covered_policies)
        if missing_policies:
            raise ValueError(f"Declared policy refs are not covered by a CHECK: {missing_policies}")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload["active_requirement_ids"] = sorted(payload["active_requirement_ids"])
        payload["policy_refs"] = sorted(payload["policy_refs"])
        payload["nodes"] = sorted(payload["nodes"], key=lambda item: item["id"])
        for node in payload["nodes"]:
            node["depends_on"] = sorted(node["depends_on"])
            node["upstream_check_ids"] = sorted(node["upstream_check_ids"])
            node["requirement_refs"] = sorted(node["requirement_refs"])
            node["policy_refs"] = sorted(node["policy_refs"])
            node["facet_refs"] = sorted(node["facet_refs"])
            node["semantic_role_refs"] = sorted(node["semantic_role_refs"])
        return _stable_hash(payload)


class RecordFieldLocator(_CompilerModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["record_field"] = "record_field"
    record_ref: str
    field_path: str = Field(description="RFC 6901 JSON Pointer rooted at read_source.record_fields, e.g. /amount or /lines/0/quantity; do not prefix /fields or /record_fields.")
    record_revision: str

    @field_validator("record_ref", "record_revision")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("field_path")
    @classmethod
    def validate_field_path(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/") or re.search(r"~(?:[^01]|$)", value):
            raise ValueError("field_path must be a non-empty RFC 6901 JSON Pointer")
        return value


class Claim(_CompilerModel):
    id: str
    subject: str
    predicate: str
    value: Any
    source_id: str
    quote: str = ""
    locator: str | RecordFieldLocator
    confidence: Literal["low", "medium", "high"] = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "subject", "predicate", "source_id")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("quote")
    @classmethod
    def normalize_quote(cls, value: str) -> str:
        return value.strip()

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str | RecordFieldLocator) -> str | RecordFieldLocator:
        if isinstance(value, str):
            return _require_text(value, field_name="locator")
        return value

    @model_validator(mode="after")
    def validate_locator_contract(self) -> Claim:
        if isinstance(self.locator, str):
            if not self.quote:
                raise ValueError("text-located Claims require an exact quote")
            return self
        if self.quote:
            raise ValueError("record_field Claims do not accept document quotes")
        if self.source_id != self.locator.record_ref or self.subject != self.locator.record_ref:
            raise ValueError("record_field Claim source_id and subject must match locator.record_ref")
        return self

    @field_validator("attributes")
    @classmethod
    def validate_observation_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Claim is the source-observation layer. Cross-claim meaning belongs in a
        # SemanticBindingProposal and arithmetic lineage belongs in a Witness.
        reserved = {
            "binding",
            "binding_group",
            "binding_id",
            "claim_ids",
            "operands",
            "related_claim_ids",
            "relation",
            "term_refs",
            "witness_ids",
        }
        forbidden = sorted(reserved.intersection(value))
        if forbidden:
            raise ValueError(
                "Claim attributes cannot encode semantic bindings or calculations: "
                f"{forbidden}"
            )
        return value


class EvidenceSourceDescriptor(_CompilerModel):
    source_type: Literal["document", "policy", "record", "derived_fact"]
    fingerprint: str
    revision: str = ""
    record_model: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("source descriptor fingerprint must be a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> EvidenceSourceDescriptor:
        self.revision = self.revision.strip()
        self.record_model = self.record_model.strip()
        if self.source_type in {"record", "derived_fact"}:
            if not self.revision or not self.record_model:
                raise ValueError("record source descriptors require model and revision")
            is_derived = self.record_model.startswith("derived.")
            if is_derived != (self.source_type == "derived_fact"):
                raise ValueError("derived source type must match its record model")
        elif self.revision or self.record_model:
            raise ValueError("document and policy descriptors cannot claim record identity")
        return self


class EvidenceIR(_CompilerModel):
    schema_version: str = "1"
    source_ids: list[str] = Field(default_factory=list)
    source_fingerprints: dict[str, str] = Field(default_factory=dict)
    source_revisions: dict[str, str] = Field(default_factory=dict)
    source_descriptors: dict[str, EvidenceSourceDescriptor] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        return _require_text(value, field_name="schema_version")

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="source_ids")

    @model_validator(mode="after")
    def validate_claims(self) -> EvidenceIR:
        normalized_fingerprints = {
            _require_text(source_id, field_name="source_fingerprint source id"):
            _require_text(fingerprint, field_name="source fingerprint")
            for source_id, fingerprint in self.source_fingerprints.items()
        }
        self.source_fingerprints = normalized_fingerprints
        if normalized_fingerprints and set(normalized_fingerprints) != set(self.source_ids):
            missing = sorted(set(self.source_ids) - set(normalized_fingerprints))
            extra = sorted(set(normalized_fingerprints) - set(self.source_ids))
            raise ValueError(
                "EvidenceIR source fingerprints must cover every source when supplied; "
                f"missing={missing}, extra={extra}"
            )
        self.source_revisions = {
            _require_text(source_id, field_name="source_revision source id"):
            _require_text(revision, field_name="source revision")
            for source_id, revision in self.source_revisions.items()
        }
        unknown_revisions = sorted(set(self.source_revisions) - set(self.source_ids))
        if unknown_revisions:
            raise ValueError(
                f"EvidenceIR source revisions reference unknown sources: {unknown_revisions}"
            )
        if self.source_ids:
            if set(self.source_descriptors) != set(self.source_ids):
                raise ValueError("EvidenceIR source descriptors must exactly cover source_ids")
            if not normalized_fingerprints:
                raise ValueError("EvidenceIR source descriptors require source fingerprints")
            mismatched = sorted(
                source_id
                for source_id, descriptor in self.source_descriptors.items()
                if descriptor.fingerprint != normalized_fingerprints[source_id]
                or descriptor.revision != self.source_revisions.get(source_id, "")
            )
            if mismatched:
                raise ValueError(
                    f"EvidenceIR source descriptors do not match admitted identity: {mismatched}"
                )
        claim_ids = [claim.id for claim in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("EvidenceIR claim ids must be unique")
        unknown_sources = sorted({claim.source_id for claim in self.claims} - set(self.source_ids))
        if unknown_sources:
            raise ValueError(f"EvidenceIR claims reference unknown sources: {unknown_sources}")
        if self.schema_version != "2" and any(
            isinstance(claim.locator, RecordFieldLocator) for claim in self.claims
        ):
            raise ValueError("record_field Claims require EvidenceIR schema_version 2")
        for claim in self.claims:
            if not isinstance(claim.locator, RecordFieldLocator):
                continue
            revision = self.source_revisions.get(claim.source_id)
            if revision is None:
                raise ValueError("record_field Claim source has no admitted revision")
            if revision != claim.locator.record_revision:
                raise ValueError("record_field Claim revision differs from admitted source")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload["source_ids"] = sorted(payload["source_ids"])
        payload["claims"] = sorted(payload["claims"], key=lambda item: item["id"])
        return _stable_hash(payload)

    def source_snapshot_hash(self) -> str:
        """Hash only the admitted, immutable source snapshot used by Witnesses."""
        return _stable_hash(
            {
                "kind": "compiler_runtime.evidence_source_snapshot",
                "schema_version": self.schema_version,
                "source_ids": sorted(self.source_ids),
                "source_fingerprints": self.source_fingerprints,
                "source_revisions": self.source_revisions,
                "source_descriptors": {
                    source_id: descriptor.model_dump(mode="json")
                    for source_id, descriptor in self.source_descriptors.items()
                },
            }
        )


class StrongStatusLink(_CompilerModel):
    """Verifier-owned polarity link to one replayable boolean Witness.

    The link intentionally carries no result, threshold, formula, or Policy
    value.  The Proof Kernel obtains the boolean only by replaying the named
    Witness and derives the false polarity as the opposite strong status.
    """

    witness_id: str
    true_status: Literal["SUPPORTED", "CONTRADICTED"]

    @field_validator("witness_id")
    @classmethod
    def validate_witness_id(cls, value: str) -> str:
        return _require_text(value, field_name="witness_id")


class CheckAssessment(_CompilerModel):
    check_id: str
    claim_ids: list[str] = Field(default_factory=list)
    accepted_binding_ids: list[str] = Field(default_factory=list)
    accepted_witness_ids: list[str] = Field(default_factory=list)
    strong_status_links: list["StrongStatusLink"] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    examined_source_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    missing_fact: str = ""
    gap_code: BusinessGapCode | None = None
    # Keep the verdict last in the structured output. Autoregressive models must
    # finish checking the evidence before committing to the classification.
    status: AssessmentStatus

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        return _require_text(value, field_name="check_id")

    @field_validator(
        "claim_ids",
        "accepted_binding_ids",
        "accepted_witness_ids",
        "source_ids",
        "examined_source_ids",
    )
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def normalize_explanation(self) -> CheckAssessment:
        self.reason = self.reason.strip()
        self.missing_fact = self.missing_fact.strip()
        witness_ids = [item.witness_id for item in self.strong_status_links]
        if len(set(witness_ids)) != len(witness_ids):
            raise ValueError("strong_status_links must not contain duplicate witness ids")
        return self


_EXPLICIT_FINAL_STATUS = re.compile(
    r"\b(?:final\s+(?:classification|status)|verdict)\s*(?:is|:|=|should\s+be|must\s+be)?\s*"
    r"`?(SUPPORTED|CONTRADICTED|NOT_FOUND)`?\b",
    flags=re.IGNORECASE,
)


def explicit_final_statuses(reason: str) -> set[str]:
    """Return only statuses the verifier explicitly presents as its conclusion."""
    return {match.upper() for match in _EXPLICIT_FINAL_STATUS.findall(reason)}


class ReviewArtifact(_CompilerModel):
    plan: ProofPlan
    plan_hash: str
    requirement_pack_id: str = ""
    requirement_pack_version: str = ""
    requirement_pack_hash: str = ""
    proof_signature_hash: str = ""
    evidence_ir: EvidenceIR
    source_snapshot_hash: str
    evidence_snapshot_hash: str
    proposal_hash: str = ""
    assessments: list[CheckAssessment] = Field(default_factory=list)
    binding_proposals: list["SemanticBindingProposal"] = Field(default_factory=list)
    calculation_witnesses: list["CalculationWitness"] = Field(default_factory=list)
    resolver_witnesses: list["ResolverWitness"] = Field(default_factory=list)
    submitted_claim_refs: dict[str, list[str]] = Field(default_factory=dict)
    submitted_binding_refs: dict[str, list[str]] = Field(default_factory=dict)
    submitted_witness_refs: dict[str, list[str]] = Field(default_factory=dict)
    policy_hash: str
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    resolved_policy_terms: dict[str, Any] = Field(default_factory=dict)
    unconfigured_policy_refs: list[str] = Field(default_factory=list)
    execution_status: ExecutionStatus = "COMPLETED"
    compiler_version: str
    model: str
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    artifact_hash: str = ""

    @field_validator(
        "plan_hash",
        "source_snapshot_hash",
        "evidence_snapshot_hash",
        "policy_hash",
        "compiler_version",
        "model",
    )
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("source_snapshot_hash")
    @classmethod
    def validate_source_snapshot_hash(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("source_snapshot_hash must be a SHA-256 digest")
        return value

    @field_validator("proof_signature_hash")
    @classmethod
    def normalize_proof_signature_hash(cls, value: str) -> str:
        return value.strip()

    @field_validator("unconfigured_policy_refs")
    @classmethod
    def validate_unconfigured_policy_refs(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="unconfigured_policy_refs")

    @field_validator("submitted_claim_refs", "submitted_binding_refs", "submitted_witness_refs")
    @classmethod
    def validate_submitted_refs(
        cls,
        value: dict[str, list[str]],
        info: Any,
    ) -> dict[str, list[str]]:
        return {
            _require_text(check_id, field_name=f"{info.field_name} check id"): _unique_strings(
                ref_ids,
                field_name=f"{info.field_name}[{check_id!r}]",
            )
            for check_id, ref_ids in value.items()
        }

    @field_validator("artifact_hash")
    @classmethod
    def normalize_artifact_hash(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_assessment_ids(self) -> ReviewArtifact:
        assessment_ids = [assessment.check_id for assessment in self.assessments]
        if len(set(assessment_ids)) != len(assessment_ids):
            raise ValueError("ReviewArtifact contains duplicate assessments for a check")
        binding_ids = [item.id for item in self.binding_proposals]
        witness_ids = [
            item.id for item in [*self.calculation_witnesses, *self.resolver_witnesses]
        ]
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("ReviewArtifact contains duplicate semantic binding ids")
        if len(set(witness_ids)) != len(witness_ids):
            raise ValueError("ReviewArtifact contains duplicate witness ids")
        # Cross-object truth (whether a ref exists, belongs to this CHECK/facet,
        # or was actually submitted) is deliberately not a Pydantic concern.
        # The Runtime rejects it on the normal path and the Proof Kernel must
        # diagnose hostile/stale artifacts fail-closed instead of losing that
        # attack surface during schema parsing.
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        payload["assessments"] = sorted(payload["assessments"], key=lambda item: item["check_id"])
        for assessment in payload["assessments"]:
            assessment["strong_status_links"] = sorted(
                assessment["strong_status_links"],
                key=lambda item: item["witness_id"],
            )
        payload["binding_proposals"] = sorted(
            payload["binding_proposals"], key=lambda item: item["id"]
        )
        payload["calculation_witnesses"] = sorted(
            payload["calculation_witnesses"], key=lambda item: item["id"]
        )
        payload["resolver_witnesses"] = sorted(
            payload["resolver_witnesses"], key=lambda item: item["id"]
        )
        for field_name in (
            "submitted_claim_refs",
            "submitted_binding_refs",
            "submitted_witness_refs",
        ):
            payload[field_name] = {
                check_id: sorted(ref_ids)
                for check_id, ref_ids in payload[field_name].items()
            }
        payload["unconfigured_policy_refs"] = sorted(payload["unconfigured_policy_refs"])
        return _stable_hash(payload)


class NodeResult(_CompilerModel):
    node_id: str
    kind: NodeKind
    status: AssessmentStatus
    reason: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    binding_ids: list[str] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    gap_code: BusinessGapCode | None = None

    @field_validator("claim_ids", "binding_ids", "witness_ids", "source_ids")
    @classmethod
    def validate_result_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)


class CompilationDiagnostic(_CompilerModel):
    code: str
    message: str
    node_id: str = ""
    requirement_id: str = ""
    blocking: bool = True


class ProofObligation(_CompilerModel):
    id: str
    requirement_id: str
    check_id: str
    missing_fact: str
    blocking: bool = True
    candidate_actions: list[str] = Field(
        default_factory=lambda: ["list_sources", "read_source", "bind_claim", "submit_check"]
    )


class DecisionProof(_CompilerModel):
    requirement_id: str
    root_node_id: str
    status: AssessmentStatus
    supporting_check_ids: list[str] = Field(default_factory=list)
    contradicting_check_ids: list[str] = Field(default_factory=list)
    unresolved_check_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    plan_hash: str
    requirement_pack_hash: str = ""
    source_snapshot_hash: str = ""
    evidence_snapshot_hash: str
    proposal_hash: str = ""
    policy_hash: str
    stop_reason: str


class CompiledProof(_CompilerModel):
    node_results: list[NodeResult] = Field(default_factory=list)
    decisions: list[DecisionProof] = Field(default_factory=list)
    obligations: list[ProofObligation] = Field(default_factory=list)
    diagnostics: list[CompilationDiagnostic] = Field(default_factory=list)

    def decision_for(self, requirement_id: str) -> DecisionProof | None:
        return next((item for item in self.decisions if item.requirement_id == requirement_id), None)


# Resolve typed proof-term fields without making proof_terms depend on a partially
# initialized ReviewArtifact module. proof_terms imports Claim, which is defined
# above before this late import runs.
from .proof_terms import (  # noqa: E402
    CalculationWitness,
    ResolverWitness,
    SemanticBindingProposal,
)

ReviewArtifact.model_rebuild(
    _types_namespace={
        "CalculationWitness": CalculationWitness,
        "ResolverWitness": ResolverWitness,
        "SemanticBindingProposal": SemanticBindingProposal,
    }
)
