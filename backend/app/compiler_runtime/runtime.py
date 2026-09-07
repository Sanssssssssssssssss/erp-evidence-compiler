from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from uuid import uuid4

from agents import Agent, FunctionTool, ModelSettings, ToolsToFinalOutputResult
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from agents.lifecycle import AgentHooks
from agents.memory import SQLiteSession
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents.thinking import (
    model_extra_body_for_thinking,
    role_thinking_type,
    temperature_for_thinking,
)
from app.config import Settings
from app.llm import LlmClient, ModelCallRecord
from app.runtime.agents_sdk import (
    FencedJsonOutputSchema,
    build_run_config,
    run_agent_sync,
)
from app.runtime.context_partition import usage_from_result
from app.runtime.reasoning_capture import extract_reasoning_from_result
from app.runtime.retry import error_chain, is_transient_llm_error

from .kernel import compile_review_artifact
from .models import (
    ActionProposal,
    AssessmentStatus,
    BusinessGapCode,
    CheckAssessment,
    CompiledProof,
    CompileStatus,
    EvidenceIR,
    EvidenceSourceDescriptor,
    ERPReviewContract,
    ExecutionStatus,
    ProofNode,
    ProofPlan,
    RecordFieldLocator,
    RegisteredActionContract,
    ReviewArtifact,
    StrongStatusLink,
)
from .policy import (
    canonical_policy_snapshot,
    expand_active_requirements,
    policy_excerpt_for,
    policy_hash,
    required_policy_refs,
    requirement_context,
)
from .policy import configured_policy_values as _configured_policy_values
from .proof_terms import (
    CalculationOperation,
    CalculationWitness,
    ProofTermRef,
    ResolverWitness,
    SemanticBindingProposal,
)
from .requirement_pack import DEFAULT_REQUIREMENT_PACK, RequirementPack
from .sandbox import EvidenceSandbox, SourceRecord
from .signatures import (
    PlanConformanceGate,
    proof_signature_for,
    proof_signature_hash_for,
)


class _PhaseResponseHooks(AgentHooks[Any]):
    """Retain SDK responses before structured parsing can fail."""

    def __init__(self) -> None:
        self.responses: list[Any] = []

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        self.responses.append(response)


COMPILER_VERSION = "typed_evidence_compiler_runtime_v18"
EXECUTOR_MAX_TURNS = 24
CHECK_FRONTIER_ATTEMPT_CAP = 2
CHECK_MODEL_CALL_BUDGET = 4
PROMPT_VERSIONS = {
    "task_compiler": "typed_task_compiler_v27",
    "executor": "typed_evidence_executor_v31",
    "registered_executor": "registered_action_executor_v2",
    "registered_verifier": "registered_action_verifier_v2_tool_submission",
    "evidence_executor": "bounded_evidence_executor_v10_planned_calculation",
    "evidence_verifier": "bounded_evidence_verifier_v12_observed_comparisons",
    "erp_task_compiler": "source_bound_erp_compiler_v3_atomic_routing",
    "verifier": "typed_fine_verifier_v31_tool_submission",
}
_PROMPT_ROOT = Path(__file__).with_name("prompts")
_TRACE_METADATA = {
    "prompt_version": COMPILER_VERSION,
    "prompt_file": "backend/app/compiler_runtime/prompts/",
    "output_model": "EvidenceReviewResult",
    "context_policy": [
        "task_objective",
        "active_requirement_ids",
        "source_catalog",
        "extraction_summary",
        "source_documents",
        "supervisor_task",
    ],
    "max_retries": 1,
    "allowed_tools": [
        "list_sources",
        "read_source",
        "bind_claim",
        "bind_record_field_claim",
        "bind_record_fields",
        "compute_witness",
        "compute_planned_witnesses",
        "submit_check",
    ],
    "side_effects": "none",
    "owner": "route_policy",
    "guard_policy": ["proof_plan_schema", "source_hook", "fine_verifier", "proof_kernel"],
    "fallback_policy": "fail_closed",
    "runtime": "evidence_compiler_runtime",
    "agent_as_tool": False,
}
_EVIDENCE_TYPES = {
    "invoice",
    "purchase_order",
    "goods_receipt",
    "vendor_record",
    "duplicate_payment_check",
    "process_log",
    "clear_invoice_event",
    "payment_terms",
    "policy_excerpt",
    "bpi_event_log",
    "user_statement",
    "odoo_record",
    "action_proposal",
}
_EXCLUDED_SOURCE_STATUSES = {"error", "excluded", "quarantined"}
_EXCLUDED_SOURCE_CLASSIFICATIONS = {
    "cross_case",
    "cross_case_sample",
    "excluded",
    "irrelevant",
    "mixed_case",
    "mixed_case_document",
    "out_of_scope_reference",
    "policy_guidance",
    "prompt_injection",
    "quarantined",
    "wrong_workflow",
}


def compiler_trace_metadata() -> dict[str, Any]:
    """Return the Runtime-owned trace contract without consulting RoleRegistry."""

    return {
        **_TRACE_METADATA,
        "context_policy": list(_TRACE_METADATA["context_policy"]),
        "allowed_tools": list(_TRACE_METADATA["allowed_tools"]),
        "guard_policy": list(_TRACE_METADATA["guard_policy"]),
    }


def attachment_source_admission(item: Mapping[str, Any]) -> tuple[bool, str]:
    """Admit a newly read attachment only when its runtime source boundary is explicit."""

    if str(item.get("status") or "").strip().lower() != "success":
        return False, "attachment_status_not_success"
    manifest_status = str(item.get("manifest_status") or "").strip().lower()
    if manifest_status in _EXCLUDED_SOURCE_STATUSES:
        return False, f"manifest_status_{manifest_status}"
    classification = _source_attribute(item, "classification").lower()
    if classification in _EXCLUDED_SOURCE_CLASSIFICATIONS:
        return False, f"classification_{classification}"
    if _optional_bool(_source_attribute(item, "should_accept")) is False:
        return False, "source_explicitly_not_accepted"
    if any(_true_flag(_source_attribute(item, key)) for key in ("excluded", "quarantined", "cross_case")):
        return False, "source_explicitly_excluded"
    if not any(
        isinstance(item.get(key), str) and bool(str(item.get(key)).strip())
        for key in ("source_content", "body_markdown", "content")
    ):
        return False, "source_content_unreadable"
    return True, "admitted"


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorSummary(_RuntimeModel):
    completed_check_ids: list[str] = Field(default_factory=list)
    unresolved_check_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    execution_status: ExecutionStatus = "COMPLETED"


class VerificationBatch(_RuntimeModel):
    assessments: list[CheckAssessment]


class EvidenceAssessment(_RuntimeModel):
    check_id: str
    accepted_binding_ids: list[str] = Field(default_factory=list)
    strong_status_links: list[StrongStatusLink] = Field(default_factory=list)
    source_scope_reviewed: bool = Field(strict=True, description="True only after independently examining every source in this CHECK's action_contract.source_refs, including contrary evidence and gaps. Delivery alone is not examination.")
    reason: str = ""
    missing_fact: str = ""
    gap_code: BusinessGapCode | None = None
    status: AssessmentStatus


class EvidenceVerificationBatch(_RuntimeModel):
    assessments: list[EvidenceAssessment]
    plan_issue: str = ""


class _VerifierSourceReview(_RuntimeModel):
    check_id: str
    material_status: Literal["SUFFICIENT", "MISSING", "AMBIGUOUS"]
    reason: str = Field(min_length=1)


class _RevealCandidateInput(_RuntimeModel):
    source_review: list[_VerifierSourceReview]


def _expand_verified_closures(batch: EvidenceVerificationBatch, checks: Sequence[Mapping[str, Any]]) -> VerificationBatch:
    """Expand only explicitly accepted bindings from this frozen verification request."""
    closures = {check["id"]: {c["binding_id"]: c for c in check["terminal_closures"]} for check in checks}
    scopes = {check["id"]: list((check.get("action_contract") or {}).get("source_refs", [])) for check in checks}
    assessments = []
    for item in batch.assessments:
        available = closures.get(item.check_id, {})
        if set(item.accepted_binding_ids) - set(available):
            raise ValueError("Verifier accepted a binding outside this CHECK's submitted closures")
        selected = [available[key] for key in item.accepted_binding_ids]
        assessments.append(CheckAssessment(
            **item.model_dump(exclude={"source_scope_reviewed"}),
            claim_ids=sorted({key for c in selected for key in c["claim_ids"]}),
            accepted_witness_ids=sorted({key for c in selected for key in c["witness_ids"]}),
            source_ids=sorted({key for c in selected for key in c["source_ids"]}),
            examined_source_ids=scopes.get(item.check_id, []) if item.source_scope_reviewed else [],
        ))
    return VerificationBatch(assessments=assessments)


class _ListSourcesInput(_RuntimeModel):
    pass


class _ReadSourceInput(_RuntimeModel):
    source_id: str


class _BindClaimInput(_RuntimeModel):
    subject: str
    predicate: str
    value: Any = Field(description="Document observation. Write decimal numbers as strings, for example '5000.00', never JSON floats.")
    source_id: str
    quote: str
    locator: str | int | None = Field(default=None, description="Omit for a unique exact quote: Runtime computes its locator. Provide a locator only to disambiguate repeated text; never guess line numbers.")
    confidence: str = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)
    claim_id: str = ""


class _BindRecordFieldClaimInput(_RuntimeModel):
    predicate: str
    locator: RecordFieldLocator
    confidence: str = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)
    claim_id: str = ""


class _RecordFieldSelection(_RuntimeModel):
    field_path: str
    predicate: str
    confidence: str = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)
    claim_id: str = ""


class _BindRecordFieldsInput(_RuntimeModel):
    record_ref: str
    record_revision: str
    fields: list[_RecordFieldSelection] = Field(min_length=1)


class _ComputeWitnessInput(_RuntimeModel):
    check_id: str
    facet_ref: str
    operation: CalculationOperation
    refs: list[ProofTermRef]


class _ComputeWitnessIdsInput(_ComputeWitnessInput):
    refs: list[str] = Field(description="Ordered existing Claim/Witness IDs or configured CHECK policy IDs. Runtime resolves their types; never invent constants or assign a reference type.")


class _RunRegisteredCheckInput(_RuntimeModel):
    check_id: str


class _SubmitCheckInput(_RuntimeModel):
    check_id: str
    claim_ids: list[str] = Field(default_factory=list)
    binding_proposals: list[SemanticBindingProposal] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
    note: str = ""
    submission_id: str = ""


class _SubmitEvidenceCheckInput(_RuntimeModel):
    check_id: str
    binding_proposals: list[SemanticBindingProposal] = Field(default_factory=list)
    upstream_check_ids: list[str] = Field(default_factory=list, description="Optional declared ancestors whose already submitted grounded facts this binding also consumes. Runtime expands facts, never inherits verdicts.")
    note: str = ""

    @model_validator(mode="after")
    def terminal_shape(self) -> _SubmitEvidenceCheckInput:
        if not self.binding_proposals:
            if self.upstream_check_ids or not self.note.strip():
                raise ValueError("A missing-evidence submission requires a precise note and no upstream proof selection")
        elif len(self.binding_proposals) != 1 or self.binding_proposals[0].relation not in {"CHECK_SATISFIED", "CHECK_VIOLATED"}:
            raise ValueError("Submit exactly one supported or violated candidate binding, or no binding with a gap note")
        return self


def _expand_evidence_submission(data: _SubmitEvidenceCheckInput, sandbox: EvidenceSandbox, review: Mapping[str, Any]) -> _SubmitCheckInput:
    """Project a model's explicit evidence selections into the existing sandbox contract."""
    if set(data.upstream_check_ids) - set(review.get("upstream_check_ids", [])):
        raise ValueError("Selected upstream CHECK is outside this CHECK's declared ancestry")
    bindings = [item.model_copy(deep=True) for item in data.binding_proposals]
    if data.upstream_check_ids:
        latest = {item.check_id: item for item in sandbox.latest_submissions()}
        if any(key not in latest or not latest[key].binding_ids for key in data.upstream_check_ids):
            raise ValueError("Selected upstream CHECK has no submitted terminal evidence; submit it first or report the gap")
        upstream = _submitted_proof_terms(sandbox, check_ids=set(data.upstream_check_ids))
        refs = [*bindings[0].term_refs,
                *(ProofTermRef(kind="CLAIM", ref_id=key) for key in upstream["claim_ids"]),
                *(ProofTermRef(kind="WITNESS", ref_id=key) for key in upstream["witness_ids"])]
        bindings[0].term_refs = list({(ref.kind, ref.ref_id): ref for ref in refs}.values())
    closure = _proof_terms_by_ids(sandbox, term_refs=(ref for binding in bindings for ref in binding.term_refs))
    return _SubmitCheckInput(check_id=data.check_id, binding_proposals=bindings,
        claim_ids=closure["claim_ids"], witness_ids=closure["witness_ids"], note=data.note)


@dataclass(frozen=True)
class PreparedSource:
    record: SourceRecord
    metadata: dict[str, Any]

    @property
    def source_id(self) -> str:
        return self.record.source_id

    @property
    def source_kind(self) -> str:
        return self.record.kind

    @property
    def canonical_content(self) -> str:
        return self.record.content

    @property
    def source_fingerprint(self) -> str:
        return str(self.metadata.get("source_fingerprint") or "")

    @property
    def observed_at(self) -> str:
        return str(self.metadata.get("observed_at") or "")

    @property
    def upstream_revision(self) -> str:
        return str(self.metadata.get("upstream_revision") or "")

    @property
    def provenance(self) -> dict[str, Any]:
        return dict(self.record.provenance)


@dataclass(frozen=True)
class CompilerRunResult:
    artifact: ReviewArtifact
    proof: CompiledProof
    review_result: dict[str, Any]
    retry_count: int
    compile_status: CompileStatus
    semantic_status: AssessmentStatus | None
    checkpoint: "CompilerRunCheckpoint | None" = None


class CompilerSupervisionPause(RuntimeError):
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(payload.get("public_reason") or "Compiler paused for supervision"))


class CompilerCorrection(_RuntimeModel):
    correction_id: str = Field(default_factory=lambda: f"correction_{uuid4().hex[:12]}")
    kind: Literal["RECHECK", "ADD_EVIDENCE", "CORRECT_SCOPE", "CANCEL"]
    target_check_id: str = ""
    message: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target(self) -> "CompilerCorrection":
        if self.kind in {"RECHECK", "ADD_EVIDENCE"} and not self.target_check_id.strip():
            raise ValueError(f"{self.kind} requires target_check_id")
        return self


class CompilerRunCheckpoint(_RuntimeModel):
    compiler_run_id: str
    requirement_pack_id: str = DEFAULT_REQUIREMENT_PACK.pack_id
    requirement_pack_version: str = DEFAULT_REQUIREMENT_PACK.version
    requirement_pack_hash: str = DEFAULT_REQUIREMENT_PACK.content_hash
    revision: int = 1
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    compile_status: CompileStatus | None = None
    semantic_status: AssessmentStatus | None = None
    active_check_id: str = ""
    completed_check_ids: list[str] = Field(default_factory=list)
    artifact: ReviewArtifact
    proof: CompiledProof
    retry_count: int = 0
    corrections: list[CompilerCorrection] = Field(default_factory=list)
    source_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    action_proposal: ActionProposal | None = None

    @model_validator(mode="before")
    @classmethod
    def admit_legacy_status(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "compile_status" in value:
            return value
        migrated = dict(value)
        migrated["compile_status"] = {
            "completed": "NON_CONVERGED",
            "failed": "INVALID",
            "cancelled": "CANCELLED",
        }.get(str(value.get("status") or "running"))
        migrated["semantic_status"] = None
        return migrated

    @model_validator(mode="after")
    def validate_statuses(self) -> "CompilerRunCheckpoint":
        if self.semantic_status is not None and self.compile_status != "COMMITTED":
            raise ValueError("semantic_status requires compile_status=COMMITTED")
        allowed_by_lifecycle = {
            "running": {None, "NON_CONVERGED"},
            "completed": {"COMMITTED", "NON_CONVERGED"},
            "failed": {"INVALID"},
            "cancelled": {"CANCELLED"},
        }
        if self.compile_status not in allowed_by_lifecycle[self.status]:
            raise ValueError(
                f"compile_status={self.compile_status} conflicts with checkpoint status={self.status}"
            )
        if self.compile_status == "COMMITTED":
            expected = _terminal_semantic_status(self.proof, self.compile_status)
            if expected is None or self.semantic_status != expected:
                raise ValueError("semantic_status does not match compiled proof")
        return self


def revise_compiler_checkpoint(
    checkpoint: CompilerRunCheckpoint,
    correction: CompilerCorrection,
    *,
    requirement_requiredness: Mapping[str, bool] | None = None,
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> CompilerRunCheckpoint:
    """Create a new revision without granting humans proof authority."""

    if correction.kind == "CANCEL":
        return checkpoint.model_copy(
            update={
                "revision": checkpoint.revision + 1,
                "status": "cancelled",
                "compile_status": "CANCELLED",
                "semantic_status": None,
                "active_check_id": "",
                "corrections": [*checkpoint.corrections, correction],
            }
        )
    if correction.kind != "RECHECK":
        raise ValueError(f"{correction.kind} requires a fresh compiler invocation")
    if checkpoint.status == "cancelled":
        raise ValueError("Cancelled compiler runs cannot be rechecked")

    plan = checkpoint.artifact.plan
    check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
    target = correction.target_check_id.strip()
    if target not in check_ids:
        raise ValueError(f"Correction target {target!r} is not a CHECK in this run")
    affected = _downstream_check_ids(plan, target)
    artifact = checkpoint.artifact
    revised_artifact = artifact.model_copy(
        update={
            "assessments": [item for item in artifact.assessments if item.check_id not in affected],
            "binding_proposals": [
                item for item in artifact.binding_proposals if item.check_id not in affected
            ],
            "calculation_witnesses": [
                item for item in artifact.calculation_witnesses if item.check_id not in affected
            ],
            "resolver_witnesses": [
                item for item in artifact.resolver_witnesses if item.check_id not in affected
            ],
            "submitted_claim_refs": {
                check_id: refs
                for check_id, refs in artifact.submitted_claim_refs.items()
                if check_id not in affected
            },
            "submitted_binding_refs": {
                check_id: refs
                for check_id, refs in artifact.submitted_binding_refs.items()
                if check_id not in affected
            },
            "submitted_witness_refs": {
                check_id: refs
                for check_id, refs in artifact.submitted_witness_refs.items()
                if check_id not in affected
            },
            "execution_status": "PARTIAL",
            "artifact_hash": "",
        }
    )
    retained = EvidenceSandbox.from_artifact(
        artifact=revised_artifact,
        sources=[item.record for item in prepared_sources_from_checkpoint(checkpoint)],
    )
    revised_artifact = _artifact(
        plan=plan, evidence_ir=retained.evidence_ir,
        assessments=list(revised_artifact.assessments),
        submitted_claim_refs=revised_artifact.submitted_claim_refs,
        submitted_binding_refs=revised_artifact.submitted_binding_refs,
        submitted_witness_refs=revised_artifact.submitted_witness_refs,
        policy_excerpt=revised_artifact.policy_snapshot, model=revised_artifact.model,
        sandbox=retained, execution_status="PARTIAL", requirement_pack=requirement_pack,
        proposal_hash=revised_artifact.proposal_hash,
    )
    revised_proof = compile_review_artifact(
        revised_artifact,
        requirement_requiredness=requirement_requiredness,
        requirement_pack=requirement_pack,
        source_records={
            item.record.source_id: item.record
            for item in prepared_sources_from_checkpoint(checkpoint)
        },
    )
    return CompilerRunCheckpoint(
        compiler_run_id=checkpoint.compiler_run_id,
        requirement_pack_id=checkpoint.requirement_pack_id,
        requirement_pack_version=checkpoint.requirement_pack_version,
        requirement_pack_hash=checkpoint.requirement_pack_hash,
        revision=checkpoint.revision + 1,
        status="running",
        compile_status=None,
        semantic_status=None,
        completed_check_ids=[
            check_id for check_id in checkpoint.completed_check_ids if check_id not in affected
        ],
        artifact=revised_artifact,
        proof=revised_proof,
        retry_count=checkpoint.retry_count,
        corrections=[*checkpoint.corrections, correction],
        source_snapshot=list(checkpoint.source_snapshot),
        action_proposal=checkpoint.action_proposal,
    )


def prepared_sources_from_checkpoint(checkpoint: CompilerRunCheckpoint) -> list[PreparedSource]:
    return [
        PreparedSource(
            record=SourceRecord(
                source_id=str(item["source_id"]),
                title=str(item.get("title") or ""),
                kind=str(item.get("kind") or "unknown"),
                content=str(item.get("content") or ""),
                provenance=dict(item.get("provenance") or {}),
                record_model=str(item.get("record_model") or ""),
                record_revision=str(item.get("record_revision") or ""),
            ),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in checkpoint.source_snapshot
    ]


def _validate_checkpoint_proof_closure(
    checkpoint: CompilerRunCheckpoint,
    *,
    requirement_requiredness: Mapping[str, bool],
    requirement_pack: RequirementPack,
) -> None:
    replayed = compile_review_artifact(
        checkpoint.artifact,
        requirement_requiredness=requirement_requiredness,
        requirement_pack=requirement_pack,
        source_records={
            item.record.source_id: item.record
            for item in prepared_sources_from_checkpoint(checkpoint)
        },
    )
    if checkpoint.proof != replayed:
        raise ValueError("Compiler checkpoint proof changed from Kernel replay")

    assessments = {item.check_id: item for item in checkpoint.artifact.assessments}
    node_results = {item.node_id: item for item in checkpoint.proof.node_results}
    submitted = (
        set(checkpoint.artifact.submitted_claim_refs)
        & set(checkpoint.artifact.submitted_binding_refs)
        & set(checkpoint.artifact.submitted_witness_refs)
    )
    blocking_nodes = {
        item.node_id
        for item in checkpoint.proof.diagnostics
        if item.blocking and item.node_id
    }
    for check_id in checkpoint.completed_check_ids:
        assessment = assessments.get(check_id)
        result = node_results.get(check_id)
        if (
            assessment is None
            or result is None
            or check_id not in submitted
            or check_id in blocking_nodes
            or result.kind != "CHECK"
            or result.status != assessment.status
        ):
            raise ValueError(
                f"Compiler checkpoint completed CHECK lacks Executor/Verifier/Kernel closure: {check_id!r}"
            )


def _source_snapshot(prepared_sources: Sequence[PreparedSource]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": item.record.source_id,
            "title": item.record.title,
            "kind": item.record.kind,
            "content": item.record.content,
            "provenance": dict(item.record.provenance),
            "record_model": item.record.record_model,
            "record_revision": item.record.record_revision,
            "metadata": dict(item.metadata),
        }
        for item in sorted(prepared_sources, key=lambda source: source.source_id)
    ]


def _validate_registered_proof_plan(
    plan: ProofPlan,
    *,
    action_proposal: ActionProposal | None,
    prepared_sources: Sequence[PreparedSource],
    policy_excerpt: Mapping[str, Any],
    requirement_pack: RequirementPack,
) -> set[str]:
    if action_proposal is None:
        raise ValueError("A registered ProofPlan requires its ActionProposal")
    erp_nodes = [
        node
        for node in plan.nodes
        if node.kind == "CHECK" and isinstance(node.action_contract, ERPReviewContract)
    ]
    if erp_nodes:
        check_nodes = [node for node in plan.nodes if node.kind == "CHECK"]
        if len(erp_nodes) != len(check_nodes):
            raise ValueError("ERP ProofPlan cannot mix registered contract kinds")
        fingerprints = {
            item.source_id: item.source_fingerprint for item in prepared_sources
        }
        policy_fingerprints = {
            item.source_fingerprint
            for item in prepared_sources
            if item.record.provenance.get("role") == "instruction"
        }
        by_logical = {
            node.action_contract.logical_check_id: node.id for node in erp_nodes
        }
        for node in erp_nodes:
            contract = node.action_contract
            if contract.execution_check_instance_id != node.id:
                raise ValueError("ERP CHECK execution id changed")
            if contract.proposal_hash != action_proposal.proposal_hash:
                raise ValueError("ERP CHECK proposal lineage changed")
            if contract.requirement_pack_hash != requirement_pack.content_hash:
                raise ValueError("ERP CHECK requirement pack changed")
            source_hash = _hash(
                {source_id: fingerprints[source_id] for source_id in contract.source_refs}
            )
            if source_hash != contract.source_snapshot_hash:
                raise ValueError("ERP CHECK source snapshot changed")
            if contract.policy_source_hash not in policy_fingerprints:
                raise ValueError("ERP CHECK policy source changed")
            expected_upstream = sorted(
                by_logical[item] for item in contract.upstream_logical_check_ids
            )
            if sorted(node.upstream_check_ids) != expected_upstream:
                raise ValueError("ERP CHECK dependency lineage changed")
        return set(fingerprints)
    expected_plan = lower_registered_action_proposal(
        active_requirement_ids=plan.active_requirement_ids,
        action_proposal=action_proposal,
        prepared_sources=prepared_sources,
        policy_excerpt=policy_excerpt,
        requirement_pack=requirement_pack,
    )
    actual_contracts = {
        node.id: node.action_contract
        for node in plan.nodes
        if node.kind == "CHECK" and node.action_contract is not None
    }
    expected_contracts = {
        node.id: node.action_contract
        for node in expected_plan.nodes
        if node.kind == "CHECK" and node.action_contract is not None
    }
    if actual_contracts.keys() == expected_contracts.keys():
        lineage_fields = {
            "proposal_hash": "proposal lineage changed",
            "policy_hash": "policy lineage changed",
            "requirement_pack_hash": "requirement pack changed",
            "source_fingerprints": "source fingerprint changed",
            "source_revisions": "source revision changed",
        }
        for check_id, actual in actual_contracts.items():
            expected = expected_contracts[check_id]
            for field, message in lineage_fields.items():
                if getattr(actual, field) != getattr(expected, field):
                    raise ValueError(f"Registered action contract {message}")
    if plan != expected_plan:
        raise ValueError("Registered ProofPlan differs from canonical lowering")
    return set(action_proposal.target_record_refs)


def lower_registered_action_proposal(
    *,
    active_requirement_ids: Sequence[str],
    action_proposal: ActionProposal,
    prepared_sources: Sequence[PreparedSource],
    policy_excerpt: Mapping[str, Any],
    requirement_pack: RequirementPack,
) -> ProofPlan:
    """Lower admitted refs into contracts; never create proof terms or assessments."""

    active = set(active_requirement_ids)
    capabilities = []
    for capability_id in requirement_pack.raw.get("capabilities") or {}:
        capability = requirement_pack.capability(capability_id)
        if str(capability["requirement_id"]) in active:
            capabilities.append((capability_id, capability))
    if len(capabilities) != 1:
        raise ValueError("Registered action lowering requires exactly one active capability")
    capability_id, capability = capabilities[0]
    requirement_id = str(capability["requirement_id"])
    if active != {requirement_id}:
        raise ValueError("Registered action lowering owns exactly its capability requirement")

    sources = {item.source_id: item for item in prepared_sources}
    targets = set(action_proposal.target_record_refs)
    if set(sources) != targets:
        raise ValueError("ActionProposal sources must exactly equal its target records")
    if {
        str(item.metadata.get("target_record_ref") or "").strip()
        for item in prepared_sources
    } != targets:
        raise ValueError("ActionProposal targets do not exactly cover admitted target records")

    configured_policy_refs = set(_configured_policy_values(policy_excerpt))
    predicate_program = requirement_pack.capability_predicate_program(
        capability_id,
        configured_policy_refs=configured_policy_refs,
    )
    contracts = []
    for action in action_proposal.actions:
        if action.arguments:
            raise ValueError("Registered action arguments require an explicit capability schema")
        if action.action not in capability["action_kinds"]:
            raise ValueError(f"Action is not registered by {capability_id!r}")
        source = sources[action.record_ref]
        if source.record.record_model != capability["record_model"]:
            raise ValueError(f"Registered source model changed: {source.source_id!r}")
        try:
            fields = json.loads(source.canonical_content)["fields"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Registered source is not canonical structured data: {source.source_id!r}"
            ) from exc
        if set(fields) != set(capability["record_fields"]):
            raise ValueError(f"Registered source fields changed: {source.source_id!r}")
        fingerprint = hashlib.sha256(source.canonical_content.encode()).hexdigest()
        if source.source_fingerprint != fingerprint:
            raise ValueError(f"Registered source fingerprint changed: {source.source_id!r}")
        revision = source.record.record_revision
        if not revision or source.upstream_revision != revision:
            raise ValueError(f"Registered source revision changed: {source.source_id!r}")
        if action_proposal.expected_preconditions[action.record_ref].get(
            "upstream_revision"
        ) != revision:
            raise ValueError("Registered target precondition changed")
        contracts.append(
            RegisteredActionContract(
                contract_id=f"{action_proposal.proposal_id}:{action.record_ref}",
                capability_id=capability_id,
                action_kind=action.action,
                target_record_ref=action.record_ref,
                proposal_hash=action_proposal.proposal_hash,
                policy_hash=policy_hash(policy_excerpt),
                requirement_pack_hash=requirement_pack.content_hash,
                source_refs=[action.record_ref],
                source_fingerprints={action.record_ref: fingerprint},
                source_revisions={action.record_ref: revision},
                predicate_program=predicate_program,
                terminal_relations=dict(capability["terminal_relations"]),
            )
        )
    return requirement_pack.lower_registered_action_plan(
        contracts,
        configured_policy_refs=configured_policy_refs,
    )


@dataclass
class _ExecutorConversation:
    checkpoint: EvidenceSandbox
    sandbox: EvidenceSandbox
    input_items: list[Any] | None = None
    session: Any | None = None
    last_runtime_rejection: dict[str, Any] | None = None
    provider_calls: int = 0


class _CheckBudgetExhausted(RuntimeError):
    pass


@dataclass
class _CheckModelBudget:
    remaining: int = CHECK_MODEL_CALL_BUDGET

    def consume(self) -> None:
        if self.remaining <= 0:
            raise _CheckBudgetExhausted("CHECK model call budget exhausted")
        self.remaining -= 1


class EvidenceCompilerRuntime:
    """A small plan -> act -> verify loop over an in-memory evidence sandbox."""

    def __init__(
        self,
        llm: LlmClient,
        *,
        hooks: Any | None = None,
        settings: Settings | None = None,
        progress_sink: Callable[[str, dict[str, Any], str], bool | None] | None = None,
        executor_session_db_path: str | Path | None = None,
        requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
    ) -> None:
        self.llm = llm
        self.settings = settings or llm.settings
        self.hooks = hooks
        self.progress_sink = progress_sink
        self.executor_session_db_path = Path(executor_session_db_path) if executor_session_db_path else None
        self.requirement_pack = requirement_pack
        self.current_compiler_run_id = ""
        self.current_revision = 1
        self.current_proposal_hash = ""

    def compile_review_route(
        self,
        *,
        payload: dict[str, Any],
        output_type: type[BaseModel],
    ) -> Any:
        """Run the source-bound ERP routing phase through the public Runtime API."""

        return self._run_phase(
            name="task_compiler",
            prompt_file="erp_task_compiler.md",
            prompt_version_key="erp_task_compiler",
            payload=payload,
            output_type=output_type,
            max_turns=None,
            max_output_tokens=None,
        )

    def compile_task(
        self,
        *,
        active_requirement_ids: Sequence[str],
        policy_excerpt: dict[str, Any],
        source_catalog: Sequence[dict[str, Any]],
        extraction_summary: Sequence[dict[str, Any]] = (),
        source_documents: Sequence[dict[str, Any]] = (),
        task_objective: str = "",
    ) -> ProofPlan:
        task_objective = task_objective.strip()
        requirement_ids = _unique(active_requirement_ids)
        self._progress(
            "model_started",
            stage="task_compiler",
            status="started",
            action="正在把审核目标编译成可核查的 Proof Plan",
            public_reason="先明确检查边界和完成条件，再让 Worker 阅读证据。",
            requirement_count=len(requirement_ids),
            source_count=len(source_catalog),
        )
        required_output: dict[str, Any] = {
            "active_requirement_ids": requirement_ids,
            "policy_refs": sorted(required_policy_refs(requirement_ids, self.requirement_pack)),
        }
        if task_objective:
            required_output["objective"] = task_objective
        payload = {
            "active_requirements": requirement_context(requirement_ids, self.requirement_pack),
            "proof_signatures": _active_proof_signatures(requirement_ids, self.requirement_pack),
            "policy": policy_excerpt,
            "source_catalog": _planning_source_catalog(source_catalog),
            "extraction_summary": _planning_extraction_summary(extraction_summary),
            "source_documents": _planning_source_documents(source_documents),
            "required_output": required_output,
        }
        plan: ProofPlan | None = None
        try:
            plan = self._run_phase(
                name="task_compiler",
                prompt_file="task_compiler.md",
                payload=payload,
                output_type=ProofPlan,
                max_turns=1,
            )
            plan = self._normalize_and_validate_task_plan(
                plan,
                requirement_ids=requirement_ids,
                task_objective=task_objective,
            )
        except (ModelBehaviorError, ValueError) as exc:
            failed_call = self.llm.calls[-1] if self.llm.calls else None
            plan = self._run_phase(
                name="task_compiler",
                prompt_file="task_compiler.md",
                payload=_task_compiler_repair_payload(payload, exc, plan),
                output_type=ProofPlan,
                max_turns=1,
            )
            if self.llm.calls:
                self.llm.calls[-1].retry_of = "task_compiler:validation_attempt_1"
            plan = self._normalize_and_validate_task_plan(
                plan,
                requirement_ids=requirement_ids,
                task_objective=task_objective,
            )
            if failed_call is not None:
                failed_call.recovered_by = "task_compiler_validation_retry_success"
        self._progress(
            "model_thinking",
            stage="task_compiler",
            status="completed",
            action="Proof Plan 已通过结构校验",
            public_reason="活动 Requirement、Policy 引用和无环结构均已覆盖。",
            requirement_count=len(requirement_ids),
            root_count=len(plan.roots),
            check_count=sum(1 for node in plan.nodes if node.kind == "CHECK"),
        )
        return plan

    def _normalize_and_validate_task_plan(
        self,
        plan: ProofPlan,
        *,
        requirement_ids: Sequence[str],
        task_objective: str = "",
    ) -> ProofPlan:
        if task_objective:
            plan = plan.model_copy(update={"objective": task_objective})
        if plan.active_requirement_ids != requirement_ids:
            self._progress_error("task_compiler", "Proof Plan 改变了活动 Requirement 范围。")
            raise ValueError("Task Compiler changed the ordered active requirement set")
        contracts = [
            node.action_contract
            for node in plan.nodes
            if node.kind == "CHECK" and node.action_contract is not None
        ]
        if contracts and all(isinstance(item, ERPReviewContract) for item in contracts):
            expected_policy_refs = []
        else:
            expected_policy_refs = sorted(
                {
                    operand.ref_id
                    for contract in contracts
                    for step in contract.predicate_program.steps
                    for operand in step.operands
                    if operand.kind == "POLICY"
                }
                if contracts
                else required_policy_refs(requirement_ids, self.requirement_pack)
            )
        if sorted(plan.policy_refs) != expected_policy_refs:
            self._progress_error("task_compiler", "Proof Plan 没有完整覆盖适用 Policy。")
            raise ValueError(
                f"Task Compiler policy coverage mismatch: expected={expected_policy_refs}, got={sorted(plan.policy_refs)}"
            )
        signatures = [
            signature
            for requirement_id in requirement_ids
            if (signature := proof_signature_for(requirement_id, self.requirement_pack)) is not None
        ]
        normalized_nodes = []
        for node in plan.nodes:
            required_local_policies = {
                policy_ref
                for signature in signatures
                if signature.requirement_id in node.requirement_refs
                for facet in signature.facets
                if facet.id in node.facet_refs
                and (
                    (path := facet.path_for_roles(node.semantic_role_refs)) is None
                    or "WITNESS" in path.minimum_proof_terms
                )
                for policy_ref in signature.required_policy_refs
            }
            policy_refs = sorted(set(node.policy_refs) | required_local_policies)
            normalized_nodes.append(
                node
                if policy_refs == node.policy_refs
                else node.model_copy(update={"policy_refs": policy_refs})
            )
        plan = plan.model_copy(update={"nodes": normalized_nodes})
        try:
            PlanConformanceGate(signatures).validate(plan)
        except ValueError:
            self._progress_error(
                "task_compiler",
                "Proof Plan 没有满足 Requirement 的最小 ProofSignature。",
            )
            raise
        return plan

    def execute_plan(
        self,
        *,
        plan: ProofPlan,
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any],
        sandbox: EvidenceSandbox,
        focus_check_id: str | Sequence[str],
        upstream_frontier_results: Sequence[Mapping[str, Any]] = (),
        runtime_observations: Sequence[dict[str, Any]] = (),
        conversation: _ExecutorConversation | None = None,
        model_budget: _CheckModelBudget | None = None,
    ) -> tuple[ExecutorSummary, EvidenceSandbox]:
        source_records = [item.record for item in prepared_sources]
        check_ids = [node.id for node in plan.nodes if node.kind == "CHECK"]
        requested_focus = _normalize_focus_check_ids(focus_check_id, check_ids)
        focused_nodes = [node for node in plan.nodes if node.id in requested_focus]
        action_contracts = [node.action_contract for node in focused_nodes]
        registered_lane = all(item is not None for item in action_contracts)
        erp_resolver_lane = registered_lane and all(
            isinstance(item, ERPReviewContract) and item.execution_mode == "registered_resolver"
            for item in action_contracts
        )
        evidence_lane = any(isinstance(item, ERPReviewContract) and item.execution_mode == "evidence_review" for item in action_contracts)
        if len(requested_focus) > 1 and not registered_lane:
            raise ValueError("Batch Executor focus requires registered action contracts")
        if registered_lane and len(
            {item.capability_id for item in action_contracts if item is not None}
        ) != 1:
            raise ValueError("Batch Executor focus requires one registered capability")
        allowed_source_ids = (
            frozenset(
                source_id
                for contract in action_contracts
                if contract is not None
                for source_id in contract.source_refs
            )
            if registered_lane
            else None
        )
        resolved_erp_inputs: dict[str, dict[str, Any]] = {}
        if erp_resolver_lane:
            from erp_agent_odoo.capabilities.erp_resolvers import (
                ERPResolverContractError,
                select_erp_inputs,
            )

            frozen_sources = {item.source_id: item for item in sandbox.source_records}
            try:
                for contract in action_contracts:
                    if not isinstance(contract, ERPReviewContract):
                        continue
                    bound_sources = {
                        source_id: frozen_sources[source_id]
                        for source_id in contract.source_refs
                        if source_id in frozen_sources
                    }
                    for source_id, record in bound_sources.items():
                        expected = sandbox.evidence_ir.source_fingerprints.get(source_id, "")
                        actual = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                        if actual != expected:
                            raise ValueError(
                                f"frozen source {source_id!r} differs from its admitted fingerprint"
                            )
                    resolved_erp_inputs[contract.execution_check_instance_id] = (
                        select_erp_inputs(contract, bound_sources)
                    )
            except ERPResolverContractError as exc:
                self._progress(
                    "resolver_contract",
                    stage="executor_preflight",
                    status="resolver_unavailable",
                    action="Executor 未启动：注册检查没有可重放实现",
                    public_reason=str(exc),
                    focused_check_ids=requested_focus,
                )
                raise ValueError(f"RESOLVER_NOT_REGISTERED: {exc}") from exc
            except (KeyError, TypeError, ValueError) as exc:
                self._progress(
                    "source_admission",
                    stage="executor_preflight",
                    status="evidence_incomplete",
                    action="Executor 未启动：冻结证据不完整",
                    public_reason=str(exc),
                    focused_check_ids=requested_focus,
                )
                raise ValueError(f"EVIDENCE_INCOMPLETE: {exc}") from exc
        rollback_sandbox = conversation.checkpoint if conversation is not None else sandbox
        # Every Executor call is speculative. The caller commits the candidate
        # only after focused verification and a full Kernel replay both succeed.
        sandbox = conversation.sandbox if conversation is not None else copy.deepcopy(sandbox)
        if conversation is not None:
            conversation.last_runtime_rejection = None
        current_check_terms = _submitted_proof_terms(
            sandbox,
            check_ids=set(requested_focus),
        )
        if erp_resolver_lane:
            payload = {
                "execution_program": _erp_execution_program(
                    focused_nodes,
                    resolved_inputs=resolved_erp_inputs,
                )
            }
            if runtime_observations:
                raise ValueError(
                    "ERP strict execution does not accept runtime_observations; "
                    "start a new compiler revision"
                )
        elif evidence_lane:
            by_id = {node.id: node for node in focused_nodes}
            focused_nodes = [by_id[check_id] for check_id in _ordered_check_ids(plan) if check_id in by_id]
            payload = _evidence_execution_payload(focused_nodes, sandbox, requested_focus)
            payload["review_objective"] = plan.objective
            upstream_ids = {item for check_id in requested_focus for item in _transitive_upstream_check_ids(plan, check_id)}
            payload["upstream_evidence"] = _submitted_proof_terms(sandbox, check_ids=upstream_ids)
        else:
            focused_plan = (
                {
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "objective": plan.objective,
                    "active_requirement_ids": sorted(
                        {
                            requirement_id
                            for node in focused_nodes
                            for requirement_id in node.requirement_refs
                        }
                    ),
                    "policy_refs": sorted(
                        {policy_ref for node in focused_nodes for policy_ref in node.policy_refs}
                    ),
                    **(
                        {"focused_check": focused_nodes[0].model_dump(mode="json")}
                        if len(focused_nodes) == 1
                        else {
                            "focused_checks": [
                                node.model_dump(mode="json") for node in focused_nodes
                            ]
                        }
                    ),
                }
                if registered_lane
                else plan.model_dump(mode="json")
            )
            payload = {
                "proof_plan": focused_plan,
                "proof_signatures": _active_proof_signatures(
                    plan.active_requirement_ids,
                    self.requirement_pack,
                ),
                "calculation_operation_protocol": _calculation_operation_protocol(),
                "policy": policy_excerpt,
                "source_catalog": [
                    {
                        "source_id": item.source_id,
                        "title": item.title,
                        "kind": item.kind,
                        "characters": len(item.content),
                    }
                    for item in sandbox.source_records
                    if allowed_source_ids is None or item.source_id in allowed_source_ids
                ],
                "focus_check_ids": requested_focus,
                "current_check_prior_terms": current_check_terms,
                "upstream_frontier_results": list(upstream_frontier_results),
            }
        model_input: str | list[Any] | None = None
        if runtime_observations:
            if len(requested_focus) != 1:
                raise ValueError("Batch Executor repair observations are not supported")
            focused_check_id = requested_focus[0]
            candidate_terms = _proof_terms_by_ids(
                sandbox,
                claim_ids=(
                    {item.id for item in sandbox.evidence_ir.claims}
                    - {item.id for item in rollback_sandbox.evidence_ir.claims}
                ),
                binding_ids=(
                    {item.id for item in sandbox.binding_proposals}
                    - {item.id for item in rollback_sandbox.binding_proposals}
                ),
                witness_ids=(
                    {
                        item.id
                        for item in [
                            *sandbox.calculation_witnesses,
                            *sandbox.resolver_witnesses,
                        ]
                    }
                    - {
                        item.id
                        for item in [
                            *rollback_sandbox.calculation_witnesses,
                            *rollback_sandbox.resolver_witnesses,
                        ]
                    }
                ),
            )
            observation = {
                "type": "runtime_observation",
                "focused_check_id": focused_check_id,
                "candidate_committed": False,
                "failure_signals": list(runtime_observations),
                "current_candidate_terms": candidate_terms,
                "current_submitted_terms": current_check_terms,
                "read_source_ids": list(sandbox.read_source_ids),
                "current_candidate_submissions": [
                    {
                        "submission_id": item.submission_id,
                        "claim_ids": list(item.claim_ids),
                        "binding_ids": list(item.binding_ids),
                        "witness_ids": list(item.witness_ids),
                        "note": item.note,
                    }
                        for item in sandbox.submissions
                    if item.check_id in requested_focus
                ],
                "instruction": (
                    "Continue this same CHECK from the observed state. Decide the next permitted "
                    "tool action yourself; diagnostics are environment observations, not evidence."
                ),
            }
            payload["runtime_observation"] = observation
            if (
                conversation is not None
                and conversation.session is not None
                and conversation.provider_calls
            ):
                model_input = json.dumps(observation, ensure_ascii=False, default=str)
            elif conversation is not None and conversation.input_items is not None:
                model_input = [
                    *conversation.input_items,
                    {
                        "role": "user",
                        "content": json.dumps(observation, ensure_ascii=False, default=str),
                    },
                ]
        target_check_ids = requested_focus
        self._progress(
            "model_started",
            stage="executor",
            status="started",
            action="Evidence Worker 正在按 Plan 读取来源并绑定 Claim",
            public_reason="Worker 只能通过证据沙箱读取来源、绑定事实和提交检查。",
            source_count=len(source_records),
            target_check_count=len(target_check_ids),
            existing_claim_count=len(sandbox.evidence_ir.claims),
        )
        submission_counts = {
            check_id: sum(1 for item in sandbox.submissions if item.check_id == check_id)
            for check_id in target_check_ids
        }
        submission_review_by_check = {
            node.id: {
                "check_id": node.id,
                "statement": node.statement,
                "facet_refs": list(node.facet_refs),
                "semantic_role_refs": list(node.semantic_role_refs),
                "upstream_check_ids": _transitive_upstream_check_ids(plan, node.id),
                "policy_refs": list(node.policy_refs),
                "terminal_relations": (
                    list(node.action_contract.terminal_relations)
                    if node.action_contract is not None
                    else []
                ),
                "contract_kind": getattr(
                    node.action_contract, "contract_kind", ""
                ),
            }
            for node in plan.nodes
            if node.kind == "CHECK" and node.id in target_check_ids
        }
        try:
            run_results: list[Any] = []
            with sandbox.focused_writes(requested_focus):
                summary = self._run_phase(
                    name="executor",
                    prompt_file=(
                        "evidence_executor.md" if evidence_lane else
                        "registered_executor.md"
                        if registered_lane
                        else "executor.md"
                    ),
                    prompt_version_key=(
                        "evidence_executor" if evidence_lane else
                        "registered_executor"
                        if registered_lane
                        else "executor"
                    ),
                    payload=payload,
                    output_type=ExecutorSummary,
                    tools=_sandbox_tools(
                        sandbox,
                        progress_sink=self._sandbox_progress,
                        submission_review_by_check=submission_review_by_check,
                        allowed_source_ids=allowed_source_ids,
                        record_fields_only=registered_lane and not evidence_lane,
                        reference_ids_only=evidence_lane,
                        numeric_checks=focused_nodes if evidence_lane else (),
                        resolver_only=erp_resolver_lane,
                        execution_program=(
                            payload.get("execution_program")
                            if erp_resolver_lane
                            else None
                        ),
                    ),
                    max_turns=None if evidence_lane else EXECUTOR_MAX_TURNS,
                    thinking_override="low" if evidence_lane else None,
                    max_output_tokens=None,
                    tool_use_behavior=_completion_hook(
                        sandbox,
                        target_check_ids,
                        prior_submission_counts=submission_counts,
                        require_final_review=not (evidence_lane or erp_resolver_lane),
                    ),
                    input_override=model_input,
                    result_sink=run_results.append if conversation is not None else None,
                    model_budget=model_budget,
                    session=conversation.session if conversation is not None else None,
                    parallel_tool_calls=(
                        False if erp_resolver_lane else len(requested_focus) > 1
                    ),
                )
            if conversation is not None and run_results:
                run_result = run_results[-1]
                if conversation.session is None:
                    conversation.input_items = run_result.to_input_list()
                conversation.provider_calls += len(getattr(run_result, "raw_responses", ()))
        except (ModelBehaviorError, UserError):
            submitted = {
                check_id
                for check_id in target_check_ids
                if sum(1 for item in sandbox.submissions if item.check_id == check_id)
                > submission_counts.get(check_id, 0)
            }
            if not submitted:
                raise
            summary = ExecutorSummary(
                completed_check_ids=sorted(submitted),
                unresolved_check_ids=sorted(set(target_check_ids) - submitted),
                summary=(
                    "Executor stopped on an SDK model/tool error after preserving newly accepted "
                    "CHECK submissions; unsubmitted checks remain unresolved."
                ),
                execution_status="PARTIAL",
            )
        except MaxTurnsExceeded:
            submitted = {
                check_id
                for check_id in target_check_ids
                if sum(1 for item in sandbox.submissions if item.check_id == check_id)
                > submission_counts.get(check_id, 0)
            }
            if not submitted:
                summary = ExecutorSummary(
                    completed_check_ids=[],
                    unresolved_check_ids=sorted(target_check_ids),
                    summary=(
                        "Executor exhausted its turn budget without a new CHECK submission; "
                        "the speculative candidate will not be committed."
                    ),
                    execution_status="PARTIAL",
                )
                self._progress(
                    "model_thinking",
                    stage="executor",
                    status="partial",
                    action="Evidence Worker 未在轮次预算内提交当前检查",
                    public_reason="候选事务保持私有，后续提交门会回滚本次 CHECK。",
                    unresolved_check_count=1,
                )
            else:
                summary = ExecutorSummary(
                    completed_check_ids=sorted(submitted),
                    unresolved_check_ids=sorted(set(target_check_ids) - submitted),
                    summary="Executor turn budget exhausted; admitted work was preserved and missing checks remain unresolved.",
                    execution_status=(
                        "COMPLETED" if set(target_check_ids) <= submitted else "PARTIAL"
                    ),
                )
        unknown = sorted(
            (set(summary.completed_check_ids) | set(summary.unresolved_check_ids)) - set(check_ids)
        )
        if unknown:
            self._progress_error("executor", "Worker 提交了 Proof Plan 之外的检查。")
            raise ValueError(f"Executor referenced checks outside the ProofPlan: {unknown}")
        outside_focus = sorted(
            (set(summary.completed_check_ids) | set(summary.unresolved_check_ids))
            - set(target_check_ids)
        )
        scope_violation = _focused_candidate_scope_violation(
            rollback_sandbox,
            sandbox,
            focused_check_ids=requested_focus,
        )
        if (
            not outside_focus
            and scope_violation is not None
            and scope_violation.get("scope_error") == "UNOWNED_FOCUSED_PROOF_MATERIAL"
            and len(sandbox.submissions) > len(rollback_sandbox.submissions)
        ):
            sandbox.discard_proof_material(
                claim_ids=scope_violation.get("orphan_claim_ids") or (),
                binding_ids=scope_violation.get("orphan_binding_ids") or (),
                witness_ids=scope_violation.get("orphan_witness_ids") or (),
            )
            scope_violation = _focused_candidate_scope_violation(
                rollback_sandbox,
                sandbox,
                focused_check_ids=requested_focus,
            )
        if outside_focus or scope_violation is not None:
            self._progress(
                "rejected_candidate", stage="executor", status="uncommitted_proof_material",
                action="未提交候选已保留，不进入活动证明",
                public_reason="不完整证明不是业务结论；原始事实和工具结果保留用于诊断。",
                evidence_ir=sandbox.evidence_ir.model_dump(mode="json"),
                bindings=[item.model_dump(mode="json") for item in sandbox.binding_proposals],
                witnesses=[item.model_dump(mode="json") for item in sandbox.calculation_witnesses],
            )
            details = dict(scope_violation or {})
            if outside_focus:
                details["summary_check_ids_outside_focus"] = outside_focus
            self._progress(
                "model_thinking",
                stage="executor",
                status="boundary_violation",
                action="Evidence Worker 聚焦修复越过了 CHECK 写入边界",
                public_reason=(
                    "候选沙箱已整体丢弃；原 Artifact、非聚焦 CHECK 与既有证明工件保持冻结。"
                ),
                violation_code="FOCUSED_CHECK_SCOPE_VIOLATION",
                focused_check_ids=requested_focus,
                **details,
            )
            if conversation is not None:
                read_source_ids = conversation.sandbox.read_source_ids
                conversation.sandbox = copy.deepcopy(rollback_sandbox)
                for source_id in read_source_ids:
                    conversation.sandbox.read_source(source_id)
                conversation.last_runtime_rejection = {
                    "check_ids": list(requested_focus),
                    "diagnostic_code": "FOCUSED_CHECK_SCOPE_VIOLATION",
                    "kernel_message": (
                        "Executor candidate crossed the focused CHECK ownership boundary."
                    ),
                    "details": details,
                }
            return (
                ExecutorSummary(
                    completed_check_ids=[],
                    unresolved_check_ids=sorted(target_check_ids),
                    summary=(
                        "Focused Executor candidate crossed its CHECK ownership boundary; "
                        "the candidate was discarded transactionally."
                    ),
                    execution_status="PARTIAL",
                ),
                rollback_sandbox,
            )
        self._progress(
            "model_thinking",
            stage="executor",
            status=("partial" if summary.execution_status == "PARTIAL" else "completed"),
            action=(
                "Evidence Worker 已保留本轮部分证据工作"
                if summary.execution_status == "PARTIAL"
                else "Evidence Worker 已完成本轮证据工作"
            ),
            public_reason=(
                "已采纳的合法工件将继续交给独立 Verifier；未提交 CHECK 保持 unresolved。"
                if summary.execution_status == "PARTIAL"
                else "已采纳的 Claim 与检查提交已冻结，等待独立 Verifier 核查。"
            ),
            source_count=len(sandbox.source_records),
            read_source_count=len(sandbox.read_source_ids),
            claim_count=len(sandbox.evidence_ir.claims),
            submitted_check_count=len({item.check_id for item in sandbox.submissions}),
            unresolved_check_count=len(summary.unresolved_check_ids),
            execution_status=summary.execution_status,
        )
        return summary, sandbox

    def verify(
        self,
        *,
        plan: ProofPlan,
        sandbox: EvidenceSandbox,
        policy_excerpt: dict[str, Any],
        focus_check_id: str | Sequence[str],
        upstream_frontier_results: Sequence[Mapping[str, Any]] = (),
        repair_feedback: Sequence[dict[str, Any]] = (),
        model_budget: _CheckModelBudget | None = None,
    ) -> list[CheckAssessment]:
        all_check_ids = [node.id for node in plan.nodes if node.kind == "CHECK"]
        requested_check_ids = _normalize_focus_check_ids(focus_check_id, all_check_ids)
        feedback_check_ids = {
            str(item.get("check_id") or "").strip()
            for item in repair_feedback
            if str(item.get("check_id") or "").strip()
        }
        if not feedback_check_ids.issubset(set(requested_check_ids)):
            raise ValueError(
                "Fine Verifier repair_feedback references checks outside focus_check_ids"
            )
        target_check_ids = set(requested_check_ids)
        focused_nodes = [node for node in plan.nodes if node.id in target_check_ids]
        action_contracts = [node.action_contract for node in focused_nodes]
        registered_lane = all(item is not None for item in action_contracts)
        erp_resolver_lane = registered_lane and all(
            isinstance(item, ERPReviewContract) and item.execution_mode == "registered_resolver"
            for item in action_contracts
        )
        evidence_lane = any(isinstance(item, ERPReviewContract) and item.execution_mode == "evidence_review" for item in action_contracts)
        if len(requested_check_ids) > 1 and not registered_lane:
            raise ValueError("Batch Fine Verifier focus requires registered action contracts")
        if registered_lane and len(
            {item.capability_id for item in action_contracts if item is not None}
        ) != 1:
            raise ValueError("Batch Fine Verifier focus requires one registered capability")
        self._progress(
            "model_started",
            stage="fine_verifier",
            status="started",
            action="Fine Verifier 正在逐项核查原子命题",
            public_reason="Verifier 只读取检查、Claim、原始引用与 Policy，不沿用 Worker 的最终判断。",
            check_count=len(target_check_ids),
            claim_count=len(sandbox.evidence_ir.claims),
            focused_repair=bool(repair_feedback),
        )
        claims = {claim.id: claim for claim in sandbox.evidence_ir.claims}
        submitted_refs = _submitted_claim_refs(sandbox)
        submitted_binding_refs = _submitted_binding_refs(sandbox)
        submitted_witness_refs = _submitted_witness_refs(sandbox)
        bindings = {item.id: item for item in sandbox.binding_proposals}
        witnesses = {
            item.id: item
            for item in [*sandbox.calculation_witnesses, *sandbox.resolver_witnesses]
        }
        runtime_resolver_inputs = []
        if erp_resolver_lane:
            from erp_agent_odoo.capabilities.erp_resolvers import resolve_erp_check

            source_records = {item.source_id: item for item in sandbox.source_records}
            if any(
                hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                != sandbox.evidence_ir.source_fingerprints.get(source_id, "")
                for source_id, record in source_records.items()
            ):
                raise ValueError("Frozen ERP source content differs from its admitted fingerprint")
            for node in focused_nodes:
                contract = node.action_contract
                if not isinstance(contract, ERPReviewContract):
                    continue
                resolved_inputs, _result, _diagnostics = resolve_erp_check(
                    contract,
                    {
                        source_id: source_records[source_id]
                        for source_id in contract.source_refs
                    },
                )
                runtime_resolver_inputs.append(
                    {
                        "check_id": node.id,
                        "contract_hash": contract.immutable_contract_hash,
                        "resolver_id": contract.resolver_program.resolver_id,
                        "resolver_version": contract.resolver_program.resolver_version,
                        "source_refs": list(contract.source_refs),
                        "source_fingerprints": {
                            source_id: sandbox.evidence_ir.source_fingerprints[source_id]
                            for source_id in contract.source_refs
                        },
                        "resolved_inputs": resolved_inputs,
                    }
                )
        expected_source_ids = (
            {
                source_id
                for contract in action_contracts
                if contract is not None
                for source_id in contract.source_refs
            }
            if registered_lane and not evidence_lane
            else set(sandbox.evidence_ir.source_ids)
        )
        sources = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "content": "" if erp_resolver_lane else source.content,
                "system_provenance": dict(source.provenance),
            }
            for source in sandbox.source_records
            if not registered_lane or source.source_id in expected_source_ids
        ]
        visible_source_ids = {item["source_id"] for item in sources}
        if visible_source_ids != expected_source_ids:
            self._progress_error("fine_verifier", "Verifier 的来源快照与 Evidence IR 不一致。")
            raise ValueError(
                "Fine Verifier source snapshot mismatch: "
                f"missing={sorted(expected_source_ids - visible_source_ids)}, "
                f"extra={sorted(visible_source_ids - expected_source_ids)}"
            )
        submission_notes = {item.check_id: item.note for item in sandbox.latest_submissions()}
        checks = []
        for node in plan.nodes:
            if node.kind != "CHECK" or node.id not in target_check_ids:
                continue
            candidate_ids = submitted_refs.get(node.id, [])
            checks.append(
                {
                    **node.model_dump(mode="json"),
                    **({"executor_note": submission_notes.get(node.id, "")} if evidence_lane else {}),
                    "submitted_claim_refs": candidate_ids,
                    "candidate_claims": [
                        claims[claim_id].model_dump(mode="json")
                        for claim_id in candidate_ids
                        if claim_id in claims
                    ],
                    "submitted_binding_refs": submitted_binding_refs.get(node.id, []),
                    "candidate_binding_proposals": [
                        bindings[binding_id].model_dump(mode="json")
                        for binding_id in submitted_binding_refs.get(node.id, [])
                        if binding_id in bindings
                    ],
                    "submitted_witness_refs": submitted_witness_refs.get(node.id, []),
                    "candidate_calculation_witnesses": [
                        witnesses[witness_id].model_dump(mode="json")
                        for witness_id in submitted_witness_refs.get(node.id, [])
                        if isinstance(witnesses.get(witness_id), CalculationWitness)
                    ],
                    "candidate_resolver_witnesses": [
                        witnesses[witness_id].model_dump(mode="json")
                        for witness_id in submitted_witness_refs.get(node.id, [])
                        if isinstance(witnesses.get(witness_id), ResolverWitness)
                    ],
                    "terminal_closures": [
                        {"binding_id": binding_id, "claim_ids": closure["claim_ids"],
                         "witness_ids": closure["witness_ids"],
                         "source_ids": sorted({claims[key].source_id for key in closure["claim_ids"] if key in claims})}
                        for binding_id in submitted_binding_refs.get(node.id, [])
                        for closure in [_proof_terms_by_ids(sandbox, binding_ids=[binding_id])]
                    ] if evidence_lane else [],
                }
            )
        payload = {
            "checks": checks,
            "proof_signatures": _active_proof_signatures(
                plan.active_requirement_ids,
                self.requirement_pack,
            ),
            "calculation_operation_protocol": _calculation_operation_protocol(),
            "strong_status_link_protocol": _strong_status_link_protocol(),
            "verification_contracts": _verifier_contracts(checks, self.requirement_pack),
            "focus_check_ids": requested_check_ids,
            "runtime_resolver_inputs": runtime_resolver_inputs,
            # Verifier judges the focused CHECK from committed proof terms, not
            # from another CHECK's classification.  Status topology remains a
            # Kernel concern and would only anchor the independent review.
            "upstream_frontier_results": [
                {key: value for key, value in item.items() if key != "status"}
                for item in upstream_frontier_results
            ],
            "repair_feedback": list(repair_feedback),
            "sources": sources,
            "policy": policy_excerpt,
        }
        source_review: list[dict[str, Any]] = []
        verifier_tools = []
        if evidence_lane:
            payload["review_objective"] = plan.objective
            payload["review_plan"] = {
                "roots": plan.roots,
                "nodes": [
                    {"id": node.id, "kind": node.kind, "statement": node.statement,
                     "depends_on": node.depends_on, "upstream_check_ids": node.upstream_check_ids,
                     "action_scope": ({"action_id": node.action_contract.owner_action_id,
                                       "action_kind": node.action_contract.action_kind,
                                       "target_record_refs": node.action_contract.target_record_refs,
                                       "source_refs": node.action_contract.source_refs}
                                      if isinstance(node.action_contract, ERPReviewContract) else None)}
                    for node in plan.nodes
                ],
            }
            payload["proposal_records"] = list({
                (record.action_id, record.record_ref): record.model_dump(mode="json")
                for node in plan.nodes if isinstance(node.action_contract, ERPReviewContract)
                for record in node.action_contract.proposal_records
            }.values())
            upstream_ids = {item for key in requested_check_ids for item in _transitive_upstream_check_ids(plan, key)}
            payload["upstream_evidence"] = _submitted_proof_terms(sandbox, check_ids=upstream_ids)
            candidate = {key: payload.pop(key) for key in (
                "upstream_evidence", "upstream_frontier_results", "repair_feedback",
            )}
            candidate["checks"] = [
                {key: value for key, value in check.items() if key == "id" or key not in ProofNode.model_fields}
                for check in checks
            ]
            candidate["proof_terms"] = {}
            for kind, fields in (
                ("claims", ("candidate_claims",)),
                ("bindings", ("candidate_binding_proposals",)),
                ("witnesses", ("candidate_calculation_witnesses", "candidate_resolver_witnesses")),
            ):
                terms = candidate["upstream_evidence"].pop(kind)
                for check in candidate["checks"]:
                    for field in fields:
                        terms.extend(check.pop(field))
                candidate["proof_terms"][kind] = list({term["id"]: term for term in terms}.values())
            payload["checks"] = [node.model_dump(mode="json") for node in focused_nodes]
            if len(focused_nodes) > 1:
                contracts = [check["action_contract"] for check in payload["checks"]]
                shared = {key: value for key, value in contracts[0].items()
                          if all(key in contract and contract[key] == value for contract in contracts)}
                payload["shared_action_contract"] = shared
                for check in payload["checks"]:
                    check["action_contract"] = {key: value for key, value in check["action_contract"].items()
                                                if key not in shared}

            async def reveal_candidate(_context: Any, raw: str) -> str:
                request = _RevealCandidateInput.model_validate_json(raw)
                ids = [item.check_id for item in request.source_review]
                if (len(ids) != len(set(ids)) or set(ids) != target_check_ids
                        or any(not item.reason.strip() for item in request.source_review)):
                    return _tool_json({"ok": False, "error": "Review every focused CHECK exactly once with a nonempty source-based reason before revealing the candidate."})
                if not source_review:
                    source_review.extend(item.model_dump(mode="json") for item in request.source_review)
                    self._progress(
                        "verifier_source_review", stage="fine_verifier", status="candidate_revealed",
                        action="Verifier 已记录原文判断，开始核查候选证明",
                        public_reason="初步材料判断先于候选揭示冻结；它不是最终证明或标准答案。",
                        source_review=copy.deepcopy(source_review),
                    )
                # A transport retry may repeat the reveal; retain the first review.
                return _tool_json({"ok": True, "source_review": source_review, "candidate": candidate})

            verifier_tools = [_function_tool(
                "reveal_candidate", "Record a source-only material review for each focused CHECK, then reveal the Executor candidate. The first review is retained on repeated calls.",
                _RevealCandidateInput, reveal_candidate,
            )]
        batch = self._run_phase(
            name="fine_verifier",
            prompt_file=("evidence_verifier.md" if evidence_lane else "registered_verifier.md" if registered_lane else "verifier.md"),
            prompt_version_key=("evidence_verifier" if evidence_lane else "registered_verifier" if registered_lane else "verifier"),
            payload=payload,
            output_type=EvidenceVerificationBatch if evidence_lane else VerificationBatch,
            tools=verifier_tools,
            max_turns=None if evidence_lane else 1,
            max_output_tokens=None,
            model_budget=model_budget,
        )
        if evidence_lane:
            if batch.plan_issue.strip():
                raise CompilerSupervisionPause({
                    "status": "plan_review_required", "message": batch.plan_issue,
                })
            if not source_review:
                raise ModelBehaviorError("Verifier ended without a source review and candidate inspection")
            batch = _expand_verified_closures(batch, checks)
        expected = {item["id"] for item in checks}
        actual = [item.check_id for item in batch.assessments]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            self._progress_error("fine_verifier", "Verifier 没有对每个 CHECK 恰好核查一次。")
            raise ValueError(
                f"Fine Verifier must assess every CHECK exactly once: expected={sorted(expected)}, got={sorted(actual)}"
            )
        for assessment in batch.assessments:
            unknown_accepted_bindings = sorted(
                set(assessment.accepted_binding_ids)
                - set(submitted_binding_refs.get(assessment.check_id, []))
            )
            unknown_accepted_witnesses = sorted(
                set(assessment.accepted_witness_ids)
                - set(submitted_witness_refs.get(assessment.check_id, []))
            )
            if unknown_accepted_bindings or unknown_accepted_witnesses:
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="boundary_violation",
                    action="Verifier 返回了越权的 Proof Term 引用",
                    public_reason=(
                        "越权引用将保留在 Artifact 中，由 Proof Kernel 显式降级为 NOT_FOUND。"
                    ),
                    check_id=assessment.check_id,
                    unknown_binding_ids=unknown_accepted_bindings,
                    unknown_witness_ids=unknown_accepted_witnesses,
                )
            polarity_violations = _strong_status_link_boundary_violations(
                assessment,
                witnesses,
            )
            if polarity_violations:
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="boundary_violation",
                    action="Verifier 的终端 Witness 极性协议不一致",
                    public_reason=(
                        "错误链接将原样保留，由 Proof Kernel 显式降级；Runtime 不会从当前结果或说明文字反推极性。"
                    ),
                    check_id=assessment.check_id,
                    violation_code="STRONG_STATUS_LINK_POLARITY_CONFLICT",
                    polarity_violations=polarity_violations,
                )
        counts = {
            status.lower(): sum(1 for item in batch.assessments if item.status == status)
            for status in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND")
        }
        self._progress(
            "model_thinking",
            stage="fine_verifier",
            status="completed",
            action="Fine Verifier 已完成全部原子检查",
            public_reason="三态结果已经引用完整性校验，可交给 Proof Kernel 聚合。",
            check_count=len(batch.assessments),
            supported_count=counts["supported"],
            contradicted_count=counts["contradicted"],
            not_found_count=counts["not_found"],
        )
        return batch.assessments

    def run(
        self,
        *,
        active_requirement_ids: Sequence[str],
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any] | None = None,
        extraction_summary: Sequence[dict[str, Any]] = (),
        requirement_requiredness: Mapping[str, bool] | None = None,
        task_objective: str = "",
        compiler_run_id: str = "",
        checkpoint: CompilerRunCheckpoint | None = None,
        checkpoint_sink: Callable[[CompilerRunCheckpoint], None] | None = None,
        action_proposal: ActionProposal | None = None,
        proof_plan: ProofPlan | None = None,
    ) -> CompilerRunResult:
        self.requirement_pack.assert_integrity()
        prepared_sources = list(prepared_sources)
        if checkpoint is not None:
            checkpoint = CompilerRunCheckpoint.model_validate(
                checkpoint.model_dump(mode="json")
            )
            if checkpoint.artifact.plan_hash != checkpoint.artifact.plan.content_hash():
                raise ValueError("Compiler checkpoint ProofPlan hash changed")
            if checkpoint.artifact.artifact_hash != checkpoint.artifact.content_hash():
                raise ValueError("Compiler checkpoint artifact hash changed")
        if action_proposal is not None:
            action_proposal = ActionProposal.model_validate(
                action_proposal.model_dump(mode="json")
            )
        if proof_plan is not None:
            proof_plan = ProofPlan.model_validate(proof_plan.model_dump(mode="json"))
            if task_objective.strip():
                raise ValueError("A registered ProofPlan owns its canonical objective")
        if checkpoint is not None and proof_plan is not None:
            raise ValueError("Compiler checkpoint already owns its immutable ProofPlan")
        if checkpoint is not None:
            if action_proposal is None:
                action_proposal = checkpoint.action_proposal
            elif (
                checkpoint.action_proposal is None
                or checkpoint.action_proposal.proposal_hash != action_proposal.proposal_hash
            ):
                raise ValueError("Compiler checkpoint action proposal changed")
        if checkpoint is not None and not prepared_sources:
            prepared_sources = prepared_sources_from_checkpoint(checkpoint)
        if checkpoint is None and action_proposal is not None and proof_plan is None:
            raise ValueError("ActionProposal execution requires a registered ProofPlan")
        active_ids = expand_active_requirements(active_requirement_ids, self.requirement_pack)
        requires_registered_plan = bool(
            set(active_ids).intersection(
                self.requirement_pack.capability_requirement_ids()
            )
        )
        if requires_registered_plan and action_proposal is None:
            raise ValueError("Capability-backed requirements require an ActionProposal")
        if requires_registered_plan and checkpoint is None and proof_plan is None:
            raise ValueError("Capability-backed requirements require a registered ProofPlan")
        requiredness = {
            requirement_id: bool(
                (requirement_requiredness or {}).get(
                    requirement_id,
                    self.requirement_pack.default_required(requirement_id),
                )
            )
            for requirement_id in active_ids
        }
        policy_excerpt = policy_excerpt or policy_excerpt_for(active_ids, self.requirement_pack)
        run_id = compiler_run_id.strip() or f"compiler_{uuid4().hex[:12]}"
        self.current_compiler_run_id = run_id
        self.current_revision = (checkpoint.revision + (checkpoint.compile_status == "NON_CONVERGED")) if checkpoint is not None else 1
        self.current_proposal_hash = action_proposal.proposal_hash if action_proposal else ""
        completed_check_ids: list[str] = []
        if checkpoint is None:
            if proof_plan is not None:
                _validate_registered_proof_plan(
                    proof_plan,
                    action_proposal=action_proposal,
                    prepared_sources=prepared_sources,
                    policy_excerpt=policy_excerpt,
                    requirement_pack=self.requirement_pack,
                )
            plan = (
                self._normalize_and_validate_task_plan(
                    proof_plan,
                    requirement_ids=active_ids,
                    task_objective=task_objective,
                )
                if proof_plan is not None
                else self.compile_task(
                    active_requirement_ids=active_ids,
                    policy_excerpt=policy_excerpt,
                    source_catalog=[
                        {
                            "source_id": item.record.source_id,
                            "title": item.record.title,
                            "kind": item.record.kind,
                            "characters": len(item.record.content),
                        }
                        for item in prepared_sources
                    ],
                    extraction_summary=extraction_summary,
                    source_documents=[
                        {"kind": item.record.kind, "content": item.record.content}
                        for item in prepared_sources
                    ],
                    task_objective=task_objective,
                )
            )
            sandbox = _initial_sandbox(
                plan=plan,
                prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt,
            )
            assessments: list[CheckAssessment] = []
            artifact = _artifact(
                plan=plan,
                evidence_ir=sandbox.evidence_ir,
                assessments=assessments,
                submitted_claim_refs={},
                submitted_binding_refs={},
                submitted_witness_refs={},
                policy_excerpt=policy_excerpt,
                model=self.settings.llm_model,
                sandbox=sandbox,
                execution_status=_derived_execution_status(plan, sandbox, assessments),
                requirement_pack=self.requirement_pack,
                proposal_hash=(action_proposal.proposal_hash if action_proposal else ""),
            )
            proof = compile_review_artifact(
                artifact,
                requirement_requiredness=requiredness,
                requirement_pack=self.requirement_pack,
                source_records={item.source_id: item for item in sandbox.source_records},
            )
            retry_count = 0
        else:
            if action_proposal is not None:
                expected_source_ids = _validate_registered_proof_plan(
                    checkpoint.artifact.plan,
                    action_proposal=action_proposal,
                    prepared_sources=prepared_sources,
                    policy_excerpt=policy_excerpt,
                    requirement_pack=self.requirement_pack,
                )
                checkpoint_source_ids = {
                    str(item.get("source_id") or "").strip()
                    for item in checkpoint.source_snapshot
                }
                artifact_source_ids = set(checkpoint.artifact.evidence_ir.source_ids)
                if (
                    checkpoint_source_ids != expected_source_ids
                    or artifact_source_ids != expected_source_ids
                ):
                    raise ValueError(
                        "Compiler checkpoint sources must exactly equal its proposal targets"
                    )
                if checkpoint.source_snapshot != _source_snapshot(prepared_sources):
                    raise ValueError("Compiler checkpoint source snapshot changed")
            if checkpoint.compiler_run_id != run_id:
                raise ValueError("Compiler checkpoint run id does not match requested run id")
            if (
                checkpoint.requirement_pack_id != self.requirement_pack.pack_id
                or checkpoint.requirement_pack_version != self.requirement_pack.version
                or checkpoint.requirement_pack_hash != self.requirement_pack.content_hash
            ):
                raise ValueError("Compiler checkpoint requirement pack changed")
            if checkpoint.artifact.plan.active_requirement_ids != active_ids:
                raise ValueError("Compiler checkpoint requirement scope does not match current run")
            if checkpoint.artifact.policy_hash != policy_hash(policy_excerpt):
                raise ValueError("Compiler checkpoint policy snapshot changed")
            if checkpoint.artifact.proposal_hash != self.current_proposal_hash:
                raise ValueError("Compiler checkpoint proposal lineage changed")
            plan = checkpoint.artifact.plan
            ordered_check_ids = _ordered_check_ids(plan)
            completed_set = set(checkpoint.completed_check_ids)
            if (
                len(completed_set) != len(checkpoint.completed_check_ids)
                or completed_set - set(ordered_check_ids)
            ):
                raise ValueError("Compiler checkpoint completed CHECKs contain duplicate or unknown IDs")
            nodes = {node.id: node for node in plan.nodes}
            if any(
                not set(nodes[check_id].upstream_check_ids).issubset(completed_set)
                for check_id in completed_set
            ):
                raise ValueError("Compiler checkpoint completed CHECKs break dataflow closure")
            current_sources = _initial_sandbox(
                plan=plan,
                prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt,
            )
            if (
                current_sources.evidence_ir.source_snapshot_hash()
                != checkpoint.artifact.evidence_ir.source_snapshot_hash()
            ):
                raise ValueError("Compiler checkpoint source snapshot changed")
            sandbox = EvidenceSandbox.from_artifact(
                artifact=checkpoint.artifact,
                sources=[item.record for item in prepared_sources],
            )
            if sandbox.evidence_ir.content_hash() != checkpoint.artifact.evidence_snapshot_hash:
                raise ValueError("Compiler checkpoint evidence snapshot changed")
            assessments = list(checkpoint.artifact.assessments)
            artifact = checkpoint.artifact
            proof = checkpoint.proof
            retry_count = checkpoint.retry_count
            # Older partial rechecks persisted completion order. Membership and
            # proof closure remain validated; execution uses the canonical DAG order.
            completed_check_ids = [key for key in ordered_check_ids if key in completed_set]
            _validate_checkpoint_proof_closure(
                checkpoint,
                requirement_requiredness=requiredness,
                requirement_pack=self.requirement_pack,
            )

        latest_checkpoint = CompilerRunCheckpoint(
            compiler_run_id=run_id,
            requirement_pack_id=self.requirement_pack.pack_id,
            requirement_pack_version=self.requirement_pack.version,
            requirement_pack_hash=self.requirement_pack.content_hash,
            revision=self.current_revision,
            status="running",
            active_check_id="",
            completed_check_ids=completed_check_ids,
            artifact=artifact,
            proof=proof,
            retry_count=retry_count,
            corrections=list(checkpoint.corrections) if checkpoint is not None else [],
            source_snapshot=_source_snapshot(prepared_sources),
            action_proposal=action_proposal,
        )
        if checkpoint_sink is not None:
            checkpoint_sink(latest_checkpoint)
        if checkpoint is None:
            self._progress(
                "model_thinking",
                stage="task_compiler",
                status="plan_ready",
                action="Proof Plan 已保存，等待 Supervisor 核对执行边界",
                public_reason="Supervisor 可检查 CHECK 拆分和依赖；已保存的 checkpoint 可原地继续。",
                check_count=len(_ordered_check_ids(plan)),
            )
        ordered_check_ids = _ordered_check_ids(plan)
        remaining_check_ids = [
            check_id for check_id in ordered_check_ids if check_id not in completed_check_ids
        ]
        batch_check_ids = _registered_batch_check_ids(plan, remaining_check_ids)
        batch_failed = False
        if batch_check_ids:
            latest_checkpoint = latest_checkpoint.model_copy(
                update={"active_check_id": batch_check_ids[0]}
            )
            if checkpoint_sink is not None:
                checkpoint_sink(latest_checkpoint)
            executor_session = (
                SQLiteSession(
                    f"{run_id}:batch:{len(batch_check_ids)}:r{latest_checkpoint.revision}:executor",
                    self.executor_session_db_path,
                )
                if self.executor_session_db_path is not None
                else None
            )
            try:
                (
                    sandbox,
                    assessments,
                    artifact,
                    proof,
                    frontier_retries,
                    frontier_committed,
                ) = self._run_registered_batch_frontier(
                    plan=plan,
                    check_ids=batch_check_ids,
                    prepared_sources=prepared_sources,
                    policy_excerpt=policy_excerpt,
                    requirement_requiredness=requiredness,
                    sandbox=sandbox,
                    assessments=assessments,
                    artifact=artifact,
                    proof=proof,
                    executor_session=executor_session,
                    initial_feedback=[item for key in batch_check_ids for item in _correction_feedback(
                        plan, key, latest_checkpoint.corrections,
                    )],
                )
            except Exception:
                latest_checkpoint = latest_checkpoint.model_copy(
                    update={
                        "status": "failed",
                        "compile_status": "INVALID",
                        "semantic_status": None,
                    }
                )
                if checkpoint_sink is not None:
                    checkpoint_sink(latest_checkpoint)
                raise
            finally:
                if executor_session is not None:
                    executor_session.close()
            retry_count += frontier_retries
            committed_ids = {item.check_id for item in assessments}
            completed_check_ids.extend(key for key in batch_check_ids if key in committed_ids)
            completed_check_ids.sort(key=ordered_check_ids.index)
            batch_failed = not frontier_committed
            latest_checkpoint = CompilerRunCheckpoint(
                compiler_run_id=run_id,
                requirement_pack_id=self.requirement_pack.pack_id,
                requirement_pack_version=self.requirement_pack.version,
                requirement_pack_hash=self.requirement_pack.content_hash,
                revision=latest_checkpoint.revision,
                status="running",
                active_check_id="",
                completed_check_ids=completed_check_ids,
                artifact=artifact,
                proof=proof,
                retry_count=retry_count,
                corrections=list(latest_checkpoint.corrections),
                source_snapshot=list(latest_checkpoint.source_snapshot),
                action_proposal=action_proposal,
            )
            if checkpoint_sink is not None:
                checkpoint_sink(latest_checkpoint)
        for check_id in ordered_check_ids:
            if batch_failed:
                break
            if check_id in completed_check_ids:
                continue
            latest_checkpoint = latest_checkpoint.model_copy(update={"active_check_id": check_id})
            if checkpoint_sink is not None:
                checkpoint_sink(latest_checkpoint)
            executor_session = (
                SQLiteSession(
                    f"{run_id}:{check_id}:r{latest_checkpoint.revision}:executor",
                    self.executor_session_db_path,
                )
                if self.executor_session_db_path is not None
                else None
            )
            try:
                (
                    sandbox,
                    assessments,
                    artifact,
                    proof,
                    frontier_retries,
                    frontier_committed,
                ) = self._run_check_frontier(
                    plan=plan,
                    check_id=check_id,
                    prepared_sources=prepared_sources,
                    policy_excerpt=policy_excerpt,
                    requirement_requiredness=requiredness,
                    sandbox=sandbox,
                    assessments=assessments,
                    artifact=artifact,
                    proof=proof,
                    executor_session=executor_session,
                    initial_feedback=_correction_feedback(
                        plan,
                        check_id,
                        latest_checkpoint.corrections,
                    ),
                )
            except CompilerSupervisionPause as pause:
                if str(pause.payload.get("status") or "") == "frontier_rolled_back":
                    latest_checkpoint = latest_checkpoint.model_copy(
                        update={
                            "compile_status": "NON_CONVERGED",
                            "semantic_status": None,
                        }
                    )
                    if checkpoint_sink is not None:
                        checkpoint_sink(latest_checkpoint)
                raise
            except Exception:
                latest_checkpoint = latest_checkpoint.model_copy(
                    update={
                        "status": "failed",
                        "compile_status": "INVALID",
                        "semantic_status": None,
                    }
                )
                if checkpoint_sink is not None:
                    checkpoint_sink(latest_checkpoint)
                raise
            finally:
                if executor_session is not None:
                    executor_session.close()
            retry_count += frontier_retries
            if frontier_committed:
                completed_check_ids.append(check_id)
                completed_check_ids.sort(key=ordered_check_ids.index)
            latest_checkpoint = CompilerRunCheckpoint(
                compiler_run_id=run_id,
                requirement_pack_id=self.requirement_pack.pack_id,
                requirement_pack_version=self.requirement_pack.version,
                requirement_pack_hash=self.requirement_pack.content_hash,
                revision=latest_checkpoint.revision,
                status="running",
                active_check_id="",
                completed_check_ids=completed_check_ids,
                artifact=artifact,
                proof=proof,
                retry_count=retry_count,
                corrections=list(latest_checkpoint.corrections),
                source_snapshot=list(latest_checkpoint.source_snapshot),
                action_proposal=action_proposal,
            )
            if checkpoint_sink is not None:
                checkpoint_sink(latest_checkpoint)

        compile_status: CompileStatus = (
            "COMMITTED"
            if completed_check_ids == _ordered_check_ids(plan)
            else "NON_CONVERGED"
        )
        semantic_status = _terminal_semantic_status(proof, compile_status)
        latest_checkpoint = latest_checkpoint.model_copy(
            update={
                "status": "completed" if compile_status == "COMMITTED" else "running",
                "active_check_id": "",
                "compile_status": compile_status,
                "semantic_status": semantic_status,
            }
        )
        if checkpoint_sink is not None:
            checkpoint_sink(latest_checkpoint)

        return CompilerRunResult(
            artifact=artifact,
            proof=proof,
            review_result=_review_result(
                prepared_sources=prepared_sources,
                sandbox=sandbox,
                artifact=artifact,
                proof=proof,
                requirement_pack=self.requirement_pack,
                compile_status=compile_status,
                semantic_status=semantic_status,
            ),
            retry_count=retry_count,
            compile_status=compile_status,
            semantic_status=semantic_status,
            checkpoint=latest_checkpoint,
        )

    def _run_check_frontier(
        self,
        *,
        plan: ProofPlan,
        check_id: str,
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any],
        requirement_requiredness: Mapping[str, bool],
        sandbox: EvidenceSandbox,
        assessments: Sequence[CheckAssessment],
        artifact: ReviewArtifact,
        proof: CompiledProof,
        executor_session: Any | None = None,
        initial_feedback: Sequence[dict[str, Any]] = (),
    ) -> tuple[
        EvidenceSandbox,
        list[CheckAssessment],
        ReviewArtifact,
        CompiledProof,
        int,
        bool,
    ]:
        """Run one CHECK transaction and commit only an Executor/Verifier/Kernel-green chain."""

        committed_assessments = list(assessments)
        upstream_frontier_results = _upstream_frontier_results(
            plan=plan,
            check_id=check_id,
            sandbox=sandbox,
            assessments=committed_assessments,
            proof=proof,
        )
        candidate_sandbox: EvidenceSandbox | None = None
        feedback: list[dict[str, Any]] = list(initial_feedback)
        repair_owner = "executor"
        attempts_used = 0
        executor_conversation: _ExecutorConversation | None = None
        model_budget = _CheckModelBudget()

        for attempt in range(1, CHECK_FRONTIER_ATTEMPT_CAP + 1):
            attempts_used = attempt
            self._progress(
                "model_thinking",
                stage="executor",
                status="frontier_started",
                action="Evidence Worker 正在执行单项证明前沿",
                public_reason="当前 CHECK 在私有候选沙箱中执行，三道门全部通过后才提交。",
                focused_check_ids=[check_id],
                frontier_attempt=attempt,
            )

            if attempt == 1 or repair_owner == "executor":
                executor_base = candidate_sandbox or sandbox
                before_submission_count = _check_submission_count(executor_base, check_id)
                if executor_conversation is None:
                    executor_conversation = _ExecutorConversation(
                        checkpoint=sandbox,
                        sandbox=copy.deepcopy(executor_base),
                        session=executor_session,
                    )
                try:
                    _summary, executed_sandbox = self.execute_plan(
                        plan=plan,
                        prepared_sources=prepared_sources,
                        policy_excerpt=policy_excerpt,
                        sandbox=executor_base,
                        focus_check_id=check_id,
                        upstream_frontier_results=upstream_frontier_results,
                        runtime_observations=feedback,
                        conversation=executor_conversation,
                        model_budget=model_budget,
                    )
                except _CheckBudgetExhausted:
                    break
                except (ModelBehaviorError, UserError) as exc:
                    feedback = [
                        _frontier_feedback(
                            check_id,
                            code="EXECUTOR_ATTEMPT_FAILED",
                            message=f"{type(exc).__name__}: {exc}",
                        )
                    ]
                    repair_owner = "executor"
                    self._progress(
                        "model_thinking",
                        stage="executor",
                        status="frontier_attempt_failed",
                        action="当前 CHECK 的 Executor 候选失败",
                        public_reason="已提交 checkpoint 未改变；失败仅消耗当前 CHECK 的固定尝试预算。",
                        focused_check_ids=[check_id],
                        frontier_attempt=attempt,
                    )
                    continue
                except Exception as exc:
                    self._progress(
                        "model_thinking",
                        stage="executor",
                        status="fatal",
                        action="Executor Runtime 失败，停止当前运行",
                        public_reason=f"{type(exc).__name__}: 该异常不属于模型可修复协议错误。",
                        focused_check_ids=[check_id],
                        frontier_attempt=attempt,
                    )
                    raise

                candidate_sandbox = executed_sandbox
                if _check_submission_count(executed_sandbox, check_id) <= before_submission_count:
                    feedback = [
                        executor_conversation.last_runtime_rejection
                        or _frontier_feedback(
                            check_id,
                            code="CHECK_SUBMISSION_REQUIRED",
                            message=(
                                "Executor did not create a new submission for the focused CHECK."
                            ),
                        )
                    ]
                    repair_owner = "executor"
                    self._progress(
                        "model_thinking",
                        stage="executor",
                        status="frontier_submission_missing",
                        action="当前 CHECK 没有新的提交",
                        public_reason="note-only NOT_FOUND 也必须经 submit_check；没有新提交的候选不会进入提交态。",
                        focused_check_ids=[check_id],
                        frontier_attempt=attempt,
                    )
                    continue

            if candidate_sandbox is None:
                continue

            try:
                focused_assessments = self.verify(
                    plan=plan,
                    sandbox=candidate_sandbox,
                    policy_excerpt=policy_excerpt,
                    focus_check_id=check_id,
                    upstream_frontier_results=upstream_frontier_results,
                    repair_feedback=feedback if repair_owner == "verifier" else (),
                    model_budget=model_budget,
                )
            except _CheckBudgetExhausted:
                break
            except (MaxTurnsExceeded, ModelBehaviorError, UserError) as exc:
                feedback = [
                    _frontier_feedback(
                        check_id,
                        code="VERIFIER_ATTEMPT_FAILED",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                ]
                repair_owner = "verifier"
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="frontier_attempt_failed",
                    action="当前 CHECK 的 focused Verifier 候选失败",
                    public_reason="Executor 候选仍保持私有；Verifier 未通过时不会提交任何证明工件。",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                    model_calls_used=CHECK_MODEL_CALL_BUDGET - model_budget.remaining,
                )
                continue
            except Exception as exc:
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="fatal",
                    action="Verifier Runtime 失败，停止当前运行",
                    public_reason=f"{type(exc).__name__}: 该异常不属于模型可修复协议错误。",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                    model_calls_used=CHECK_MODEL_CALL_BUDGET - model_budget.remaining,
                )
                raise

            rejected_terms = _verifier_rejected_latest_submission(
                candidate_sandbox,
                focused_assessments[0],
            )
            if rejected_terms is not None:
                feedback = [rejected_terms]
                repair_owner = "executor"
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="frontier_rejected",
                    action="Verifier 拒绝了当前 CHECK 的提交项",
                    public_reason="候选仍未提交；Executor 可在同一会话和固定预算内更正或明确证据缺口。",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                    diagnostic_codes=[rejected_terms["diagnostic_code"]],
                )
                continue

            candidate_assessments = _upsert_focused_assessment(
                plan,
                committed_assessments,
                focused_assessments,
                check_id=check_id,
            )
            try:
                candidate_artifact = _artifact(
                    plan=plan,
                    evidence_ir=candidate_sandbox.evidence_ir,
                    assessments=candidate_assessments,
                    submitted_claim_refs=_submitted_claim_refs(candidate_sandbox),
                    submitted_binding_refs=_submitted_binding_refs(candidate_sandbox),
                    submitted_witness_refs=_submitted_witness_refs(candidate_sandbox),
                    policy_excerpt=policy_excerpt,
                    model=self.settings.llm_model,
                    sandbox=candidate_sandbox,
                    execution_status=_derived_execution_status(
                        plan,
                        candidate_sandbox,
                        candidate_assessments,
                    ),
                    requirement_pack=self.requirement_pack,
                    proposal_hash=self.current_proposal_hash,
                )
                self._progress(
                    "model_started",
                    stage="proof_kernel",
                    status="started",
                    action="Proof Kernel 正在核验单项候选事务",
                    public_reason="Kernel 全量重放 Artifact，但只允许当前 CHECK 改变已提交前沿。",
                    assessment_count=len(candidate_assessments),
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                )
                candidate_proof = compile_review_artifact(
                    candidate_artifact,
                    requirement_requiredness=requirement_requiredness,
                    requirement_pack=self.requirement_pack,
                    source_records={
                        item.source_id: item for item in candidate_sandbox.source_records
                    },
                )
                self._emit_kernel_completed(candidate_proof)
            except Exception as exc:
                self._progress(
                    "model_thinking",
                    stage="proof_kernel",
                    status="fatal",
                    action="Proof Kernel Runtime 失败，停止当前运行",
                    public_reason=f"{type(exc).__name__}: Kernel 异常不能由模型修复。",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                )
                raise

            failures = _frontier_kernel_failures(
                check_id=check_id,
                committed_assessments=committed_assessments,
                committed_proof=proof,
                focused_assessment=focused_assessments[0],
                candidate_proof=candidate_proof,
            )
            if not failures:
                self._progress(
                    "model_thinking",
                    stage="proof_kernel",
                    status="frontier_committed",
                    action="当前 CHECK 已通过三道门并提交",
                    public_reason="Executor、focused Verifier 与 Kernel 结果一致；内存 checkpoint 已前移。",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                    model_calls_used=CHECK_MODEL_CALL_BUDGET - model_budget.remaining,
                )
                return (
                    candidate_sandbox,
                    candidate_assessments,
                    candidate_artifact,
                    candidate_proof,
                    attempt - 1,
                    True,
                )

            global_failures = [item for item in failures if not item.get("node_id")]
            if global_failures:
                codes = sorted(
                    {str(item.get("diagnostic_code") or "") for item in global_failures}
                )
                self._progress(
                    "model_thinking",
                    stage="proof_kernel",
                    status="fatal",
                    action="Artifact 全局完整性失败，停止当前运行",
                    public_reason=f"全局 Kernel diagnostics 不能由单个 CHECK 修复：{codes}",
                    focused_check_ids=[check_id],
                    frontier_attempt=attempt,
                    diagnostic_codes=codes,
                )
                raise RuntimeError(f"Fatal artifact integrity diagnostics: {codes}")

            feedback = failures
            repair_owner = (
                "verifier"
                if all(
                    item.get("diagnostic_code") == "TERMINAL_WITNESS_STATUS_MISMATCH"
                    for item in failures
                )
                else "executor"
            )
            self._progress(
                "model_thinking",
                stage="proof_kernel",
                status="frontier_rejected",
                action="当前 CHECK 的候选未通过 Kernel",
                public_reason="候选仍未提交；只在固定预算内重试当前 CHECK。",
                focused_check_ids=[check_id],
                frontier_attempt=attempt,
                repair_owner=repair_owner,
                diagnostic_codes=[
                    str(item.get("diagnostic_code") or "") for item in failures
                ],
            )

        self._progress(
            "model_thinking",
            stage="proof_kernel",
            status="frontier_rolled_back",
            action="当前 CHECK 已回滚，继续完整审核",
            public_reason=(
                "CHECK 共享模型调用预算已耗尽；私有候选被丢弃。"
                if model_budget.remaining == 0
                else "固定尝试预算已耗尽；仅当前 CHECK 的私有候选被丢弃。"
            ),
            focused_check_ids=[check_id],
            frontier_attempt=attempts_used,
            model_calls_used=CHECK_MODEL_CALL_BUDGET - model_budget.remaining,
            model_budget_exhausted=model_budget.remaining == 0,
        )
        return (
            sandbox,
            committed_assessments,
            artifact,
            proof,
            max(0, attempts_used - 1),
            False,
        )

    def _run_registered_batch_frontier(
        self,
        *,
        plan: ProofPlan,
        check_ids: Sequence[str],
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any],
        requirement_requiredness: Mapping[str, bool],
        sandbox: EvidenceSandbox,
        assessments: Sequence[CheckAssessment],
        artifact: ReviewArtifact,
        proof: CompiledProof,
        executor_session: Any | None = None,
        initial_feedback: Sequence[dict[str, Any]] = (),
        allow_repair: bool = True,
    ) -> tuple[
        EvidenceSandbox,
        list[CheckAssessment],
        ReviewArtifact,
        CompiledProof,
        int,
        bool,
    ]:
        """Commit valid closure, with at most one focused evidence proof repair."""

        focused = list(check_ids)
        committed_assessments = list(assessments)
        before_submissions = {
            check_id: _check_submission_count(sandbox, check_id)
            for check_id in focused
        }
        model_budget = _CheckModelBudget(remaining=2)
        conversation = _ExecutorConversation(
            checkpoint=sandbox,
            sandbox=copy.deepcopy(sandbox),
            session=executor_session,
        )
        self._progress(
            "model_thinking",
            stage="executor",
            status="batch_started",
            action="Evidence Worker 正在批量执行依赖闭合的证明 DAG",
            public_reason="同一 capability 的 CHECK 共享一次模型工具循环；依赖由 Kernel 重放。",
            focused_check_ids=focused,
        )
        active_phase = "executor"
        try:
            summary, candidate_sandbox = self.execute_plan(
                plan=plan,
                prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt,
                sandbox=sandbox,
                focus_check_id=focused,
                runtime_observations=initial_feedback,
                conversation=conversation,
                model_budget=model_budget,
            )
            submitted = {
                check_id
                for check_id in focused
                if _check_submission_count(candidate_sandbox, check_id)
                > before_submissions[check_id]
            }
            # A note-only missing-evidence submission is complete protocol work.
            # The Verifier decides NOT_FOUND; the summary's unresolved list is not a gate.
            if submitted != set(focused):
                raise ModelBehaviorError(
                    "Batch Executor did not submit every focused CHECK"
                )
            active_phase = "fine_verifier"
            focused_assessments = self.verify(
                plan=plan,
                sandbox=candidate_sandbox,
                policy_excerpt=policy_excerpt,
                focus_check_id=focused,
                model_budget=model_budget,
            )
        except (
            _CheckBudgetExhausted,
            MaxTurnsExceeded,
            ModelBehaviorError,
            UserError,
            CompilerSupervisionPause,
        ) as exc:
            pause = exc.payload if isinstance(exc, CompilerSupervisionPause) else {}
            self._progress(
                "rejected_candidate", stage=active_phase,
                status=pause.get("status", "protocol_failure"),
                action="计划需要重新审查" if pause else "候选协议未完成，原始证据保留",
                public_reason="不自动重跑语义阶段；本 revision 停在已提交边界。",
                error_type=type(exc).__name__, error=pause.get("message", str(exc)),
                evidence_ir=conversation.sandbox.evidence_ir.model_dump(mode="json"),
                bindings=[item.model_dump(mode="json") for item in conversation.sandbox.binding_proposals],
                witnesses=[item.model_dump(mode="json") for item in conversation.sandbox.calculation_witnesses],
            )
            self._progress(
                "model_thinking",
                stage="proof_kernel",
                status="batch_rolled_back",
                action="批量证明未完成，候选已整体回滚",
                public_reason=f"{type(exc).__name__}: 没有 CHECK 被部分提交。",
                focused_check_ids=focused,
                model_calls_used=2 - model_budget.remaining,
            )
            return (
                sandbox,
                committed_assessments,
                artifact,
                proof,
                0,
                False,
            )

        assessments_by_id = {
            item.check_id: item for item in [*committed_assessments, *focused_assessments]
        }
        candidate_assessments = [
            assessments_by_id[check_id]
            for check_id in _ordered_check_ids(plan)
            if check_id in assessments_by_id
        ]
        candidate_artifact = _artifact(
            plan=plan,
            evidence_ir=candidate_sandbox.evidence_ir,
            assessments=candidate_assessments,
            submitted_claim_refs=_submitted_claim_refs(candidate_sandbox),
            submitted_binding_refs=_submitted_binding_refs(candidate_sandbox),
            submitted_witness_refs=_submitted_witness_refs(candidate_sandbox),
            policy_excerpt=policy_excerpt,
            model=self.settings.llm_model,
            sandbox=candidate_sandbox,
            execution_status=_derived_execution_status(
                plan,
                candidate_sandbox,
                candidate_assessments,
            ),
            requirement_pack=self.requirement_pack,
            proposal_hash=self.current_proposal_hash,
        )
        candidate_proof = compile_review_artifact(
            candidate_artifact,
            requirement_requiredness=requirement_requiredness,
            requirement_pack=self.requirement_pack,
            source_records={
                item.source_id: item for item in candidate_sandbox.source_records
            },
        )
        focused_by_id = {item.check_id: item for item in focused_assessments}
        failures_by_check = {
            check_id: _frontier_kernel_failures(
                check_id=check_id,
                committed_assessments=committed_assessments,
                committed_proof=proof,
                focused_assessment=focused_by_id[check_id],
                candidate_proof=candidate_proof,
            )
            for check_id in focused
        }
        for assessment in focused_assessments:
            rejected = _verifier_rejected_latest_submission(candidate_sandbox, assessment)
            if rejected is not None:
                failures_by_check[assessment.check_id].append({**rejected, "node_id": assessment.check_id})
        failures = [item for items in failures_by_check.values() for item in items]
        global_codes = sorted(
            {
                str(item.get("diagnostic_code") or "")
                for item in failures
                if not item.get("node_id")
            }
        )
        if global_codes:
            raise RuntimeError(f"Fatal artifact integrity diagnostics: {global_codes}")
        ready = {item.check_id for item in committed_assessments}
        nodes = {node.id: node for node in plan.nodes}
        for check_id in _ordered_check_ids(plan):
            if (check_id in failures_by_check and not failures_by_check[check_id]
                    and set(nodes[check_id].upstream_check_ids) <= ready):
                ready.add(check_id)
        rejected_ids = set(focused) - ready
        if rejected_ids:
            self._progress(
                "rejected_candidate", stage="proof_kernel", status="proof_repair_required",
                action="未通过的候选已封存，仅保留有效证明",
                public_reason="仅提交通过三道门且依赖闭合的 CHECK；失败候选不进入活动证明。",
                rejected_check_ids=sorted(rejected_ids),
                candidate_artifact=candidate_artifact.model_dump(mode="json"),
                candidate_proof=candidate_proof.model_dump(mode="json"),
                diagnostic_codes=sorted(
                    {str(item.get("diagnostic_code") or "") for item in failures}
                ),
            )
            candidate_assessments = [item for item in candidate_assessments if item.check_id in ready]
            candidate_artifact = _artifact(
                plan=plan, evidence_ir=candidate_sandbox.evidence_ir,
                assessments=candidate_assessments,
                submitted_claim_refs={key: value for key, value in _submitted_claim_refs(candidate_sandbox).items() if key in ready},
                submitted_binding_refs={key: value for key, value in _submitted_binding_refs(candidate_sandbox).items() if key in ready},
                submitted_witness_refs={key: value for key, value in _submitted_witness_refs(candidate_sandbox).items() if key in ready},
                policy_excerpt=policy_excerpt, model=self.settings.llm_model,
                sandbox=candidate_sandbox, execution_status="PARTIAL",
                requirement_pack=self.requirement_pack, proposal_hash=self.current_proposal_hash,
            )
            candidate_proof = compile_review_artifact(
                candidate_artifact, requirement_requiredness=requirement_requiredness,
                requirement_pack=self.requirement_pack,
                source_records={item.source_id: item for item in candidate_sandbox.source_records},
            )
        # Restore only the committed projection; rejected/raw history remains in receipts.
        candidate_sandbox = EvidenceSandbox.from_artifact(
            artifact=candidate_artifact, sources=candidate_sandbox.source_records,
        )

        self._emit_kernel_completed(candidate_proof)
        self._progress(
            "model_thinking",
            stage="proof_kernel",
            status="batch_partially_committed" if rejected_ids else "batch_committed",
            action="批量 CHECK 已通过三道门并提交",
            public_reason="一次 Executor、一次 Fine Verifier；Kernel 仍逐 CHECK 重放一致。",
            focused_check_ids=focused,
            committed_check_ids=[key for key in focused if key in ready],
            model_calls_used=2 - model_budget.remaining,
        )
        if rejected_ids and allow_repair and all(
            isinstance(nodes[key].action_contract, ERPReviewContract)
            and nodes[key].action_contract.execution_mode == "evidence_review"
            for key in focused
        ):
            repair_ids = [key for key in focused if key in rejected_ids]
            self._progress(
                "model_thinking", stage="executor", status="proof_repair_started",
                action="Executor 根据复核诊断进行一次定向返工",
                public_reason="保留已提交证明，用新上下文重做失败及受阻 CHECK；再次复核后停止。",
                focused_check_ids=repair_ids, repair_attempt=1,
            )
            repaired = self._run_registered_batch_frontier(
                plan=plan, check_ids=repair_ids, prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt, requirement_requiredness=requirement_requiredness,
                sandbox=candidate_sandbox, assessments=candidate_assessments,
                artifact=candidate_artifact, proof=candidate_proof,
                initial_feedback=failures, allow_repair=False,
            )
            return (*repaired[:4], repaired[4] + 1, repaired[5])
        return (
            candidate_sandbox,
            candidate_assessments,
            candidate_artifact,
            candidate_proof,
            0,
            not rejected_ids,
        )

    def _run_phase(
        self,
        *,
        name: str,
        prompt_file: str,
        prompt_version_key: str | None = None,
        payload: dict[str, Any],
        output_type: type[BaseModel],
        tools: Sequence[FunctionTool] = (),
        max_turns: int | None,
        tool_use_behavior: Any = "run_llm_again",
        input_override: str | list[Any] | None = None,
        result_sink: Callable[[Any], None] | None = None,
        model_budget: _CheckModelBudget | None = None,
        thinking_override: str | None = None,
        session: Any | None = None,
        parallel_tool_calls: bool = False,
        max_output_tokens: int | None = None,
    ) -> Any:
        if not self.llm.available:
            raise RuntimeError("LLM_API_KEY is required for Evidence Compiler execution")
        prompt = (_PROMPT_ROOT / prompt_file).read_text(encoding="utf-8")
        model = self.settings.llm_model
        thinking_type = thinking_override or role_thinking_type(
            name,
            payload,
            self.settings.llm_thinking_type,
        )
        response_hooks = _PhaseResponseHooks()
        tool_only = name == "executor" and prompt_version_key == "evidence_executor"
        verifier_submission = name == "fine_verifier" and output_type in (VerificationBatch, EvidenceVerificationBatch)
        if verifier_submission:
            submitted = None
            submission_attempts = 0
            needs_candidate = any(tool.name == "reveal_candidate" for tool in tools)
            candidate_revealed = not needs_candidate

            async def submit_verification(_context: Any, raw: str) -> str:
                nonlocal submitted, submission_attempts
                submission_attempts += 1
                try:
                    validated = output_type.model_validate_json(raw)
                except ValidationError as exc:
                    if submission_attempts >= 2:
                        raise ModelBehaviorError("Verifier exhausted its one submission correction") from exc
                    raise
                if not candidate_revealed and not getattr(validated, "plan_issue", "").strip():
                    raise ModelBehaviorError("Verifier must inspect the revealed candidate in a prior turn before submitting")
                submitted = validated
                return _tool_json({"ok": True})

            def finish_verification(_context: Any, results: Any) -> ToolsToFinalOutputResult:
                nonlocal candidate_revealed
                if any(item.tool.name == "reveal_candidate" and json.loads(item.output).get("ok") for item in results):
                    candidate_revealed = True
                return ToolsToFinalOutputResult(is_final_output=submitted is not None, final_output=submitted)

            tools = [*tools, _function_tool(
                "submit_verification", "Submit the final verification batch. Every assessment requires status. Schema errors allow one correction by the Verifier; no verdict is supplied by the tool.",
                output_type, submit_verification,
            )]
            tool_use_behavior = finish_verification
            tool_only = True
            max_turns = 4 if max_turns is None else max(2, max_turns)
            prompt += (
                "\nSubmit your final result through submit_verification, never as final text. "
                "If it returns schema errors, correct your submission using your own review. "
                "You have one correction; error messages supply no business verdict."
            )
        agent = Agent(
            name=name,
            instructions=prompt,
            model=model,
            model_settings=ModelSettings(
                max_tokens=max_output_tokens,
                temperature=temperature_for_thinking(model, self.settings.llm_temperature, thinking_type),
                parallel_tool_calls=parallel_tool_calls if tools else None,
                extra_body=model_extra_body_for_thinking(
                    model,
                    thinking_type,
                    self.settings.llm_base_url,
                ),
            ),
            tools=list(tools),
            output_type=None if tool_only else FencedJsonOutputSchema(
                output_type,
                strict_json_schema=output_type in (VerificationBatch, EvidenceVerificationBatch),
            ),
            tool_use_behavior=tool_use_behavior,
            hooks=response_hooks,
        )
        model_input = (
            input_override
            if input_override is not None
            else json.dumps(payload, ensure_ascii=False, default=str)
        )
        input_text = (
            model_input
            if isinstance(model_input, str)
            else json.dumps(model_input, ensure_ascii=False, default=str)
        )
        prompt_version = PROMPT_VERSIONS[
            prompt_version_key or (name if name != "fine_verifier" else "verifier")
        ]
        logical_invocation_id = (
            f"{name}:revision-{self.current_revision}:{uuid4().hex[:12]}"
        )
        failed_record: ModelCallRecord | None = None
        if model_budget is not None:
            model_budget.consume()
        for attempt in range(2):
            # A failed SDK run closes its client. Build a fresh config so the one
            # visible transport retry cannot reuse a broken connection pool.
            run_config = build_run_config(
                self.settings,
                workflow_name=f"invoice_agent.compiler.{name}",
                replay_streamed_reasoning=bool(tools),
                trace_metadata={
                    "role": name,
                    "prompt_version": prompt_version,
                    "compiler_version": COMPILER_VERSION,
                    "transport_attempt": attempt + 1,
                },
                disable_timeout=True,
            )
            attempt_started = time.perf_counter()
            result = None
            response_hooks.responses.clear()
            if verifier_submission:
                submitted = None
                candidate_revealed = not needs_candidate
            try:
                attempt_input = model_input
                if attempt and session is not None:
                    attempt_input = json.dumps(
                        {
                            "type": "runtime_observation",
                            "failure_signals": [
                                {
                                    "diagnostic_code": "TRANSIENT_STREAM_INTERRUPTION",
                                    "message": "The provider stream ended before its terminal chunk.",
                                }
                            ],
                            "instruction": (
                                "Continue from the persisted conversation and accepted tool state. "
                                "Do not repeat successful tool calls; finish the current phase."
                            ),
                        },
                        ensure_ascii=False,
                    )
                result = run_agent_sync(
                    agent,
                    attempt_input,
                    max_turns=max_turns,
                    hooks=self.hooks,
                    run_config=run_config,
                    stream_response=True,
                    session=session,
                )
                if tool_only:
                    completion = tool_use_behavior(None, [])
                    if not completion.is_final_output:
                        raise ModelBehaviorError(f"{name} ended without completing required submissions through real tools")
                    # The SDK stringifies final_output when output_type is None.
                    # Read the typed result from accepted submissions, never parse that text.
                    parsed = completion.final_output
                else:
                    parsed = result.final_output
                if not isinstance(parsed, output_type):
                    parsed = output_type.model_validate(parsed)
                raw = parsed.model_dump_json()
                usage = usage_from_result(result)
                reasoning = extract_reasoning_from_result(result, final_output=raw)
                if failed_record is not None:
                    failed_record.recovered_by = "compiler_transport_retry_success"
                self.llm.calls.append(
                    ModelCallRecord(
                        role=name,
                        model=model,
                        prompt_version=prompt_version,
                        input_preview=input_text[:1400],
                        output_preview=raw[:1400],
                        system_prompt=prompt,
                        payload=payload,
                        raw_response=raw,
                        usage=usage,
                        latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                        content_chars=len(raw),
                        retry_of=f"{name}:transport_attempt_1" if attempt else "",
                        runtime="evidence_compiler_runtime",
                        reasoning_excerpt=reasoning.text if reasoning else "",
                        reasoning_full=reasoning.full_text if reasoning else "",
                        reasoning_chars=reasoning.chars if reasoning else 0,
                        reasoning_chunks=reasoning.chunks if reasoning else 0,
                        thinking_type=thinking_type,
                        reasoning_source=reasoning.source if reasoning else "",
                        logical_invocation_id=logical_invocation_id,
                        transport_attempt=attempt + 1,
                        provider_turn_count=len(
                            getattr(result, "raw_responses", []) or []
                        ),
                    )
                )
                if result_sink is not None:
                    result_sink(result)
                return parsed
            except Exception as exc:
                partial = getattr(exc, "run_data", None) or result
                if response_hooks.responses:
                    if partial is None:
                        partial = SimpleNamespace(raw_responses=[], new_items=[])
                    for response in response_hooks.responses:
                        if not any(response is received for received in partial.raw_responses):
                            partial.raw_responses.append(response)
                reasoning = extract_reasoning_from_result(partial, final_output="") if partial is not None else None
                if partial is not None and result_sink is not None:
                    result_sink(partial)
                if self.hooks is not None and hasattr(self.hooks, "record_error"):
                    self.hooks.record_error(exc)
                failed_record = ModelCallRecord(
                    role=name,
                    model=model,
                    prompt_version=prompt_version,
                    input_preview=input_text[:1400],
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    error_details=error_chain(exc),
                    usage=usage_from_result(partial) if partial is not None else None,
                    provider_turn_count=len(partial.raw_responses) if partial is not None else None,
                    reasoning_full=reasoning.full_text if reasoning else "",
                    reasoning_excerpt=reasoning.text if reasoning else "",
                    reasoning_chars=reasoning.chars if reasoning else 0,
                    reasoning_chunks=reasoning.chunks if reasoning else 0,
                    reasoning_source=reasoning.source if reasoning else "",
                    system_prompt=prompt,
                    payload=payload,
                    latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                    runtime="evidence_compiler_runtime",
                    thinking_type=thinking_type,
                    logical_invocation_id=logical_invocation_id,
                    transport_attempt=attempt + 1,
                )
                self.llm.calls.append(failed_record)
                if attempt or not is_transient_llm_error(exc):
                    self._progress(
                        "model_thinking",
                        stage=name,
                        status="error",
                        action=f"{name} 阶段失败",
                        public_reason=f"{type(exc).__name__}: 结构化模型调用没有产出可接受结果。",
                    )
                    raise
                time.sleep(0.25)

        raise RuntimeError(f"Compiler phase {name} exhausted its transport attempts")

    def _emit_kernel_completed(self, proof: CompiledProof) -> None:
        self._progress(
            "model_thinking",
            stage="proof_kernel",
            status="completed",
            action="DecisionProof 已生成",
            public_reason="每个 Requirement 已得到 SUPPORTED、CONTRADICTED 或 NOT_FOUND 结果。",
            supported_count=sum(1 for item in proof.decisions if item.status == "SUPPORTED"),
            contradicted_count=sum(1 for item in proof.decisions if item.status == "CONTRADICTED"),
            not_found_count=sum(1 for item in proof.decisions if item.status == "NOT_FOUND"),
            blocking_obligation_count=sum(1 for item in proof.obligations if item.blocking),
        )

    def _sandbox_progress(self, tool: str, result: dict[str, Any] | None) -> None:
        if result is None:
            self._progress(
                "tool_started",
                stage="executor",
                status="started",
                action=f"Evidence Worker 调用 {tool}",
                public_reason="沙箱正在校验这一步是否满足来源与引用边界。",
                tool=tool,
            )
            return
        accepted = result.get("ok") is True
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or "")
        self._progress(
            "tool_finished",
            stage="executor",
            status="completed" if accepted else "rejected",
            action=(f"{tool} 已完成" if accepted else f"证据 Hook 拒绝了 {tool}"),
            public_reason=str(
                error.get("message")
                or ("沙箱已接受这一步。" if accepted else "这一步没有进入 Evidence IR。")
            ),
            tool=tool,
            hook_code=code,
        )

    def _progress(
        self,
        kind: str,
        *,
        stage: str,
        status: str,
        action: str,
        public_reason: str,
        **counts: Any,
    ) -> None:
        if self.progress_sink is None:
            return
        payload = {
            "role": stage,
            "stage": stage,
            "status": status,
            "action": action,
            "public_reason": public_reason,
            "compiler_run_id": self.current_compiler_run_id,
            "compiler_revision": self.current_revision,
            **counts,
        }
        try:
            should_pause = self.progress_sink(kind, payload, action) is True
        except Exception:
            return
        if should_pause:
            raise CompilerSupervisionPause(payload)

    def _progress_error(self, stage: str, public_reason: str) -> None:
        self._progress(
            "model_thinking",
            stage=stage,
            status="error",
            action=f"{stage} 未通过 Runtime 校验",
            public_reason=public_reason,
        )


def prepare_sources(items: Iterable[Mapping[str, Any]]) -> list[PreparedSource]:
    """Lower attachment extraction records into readable, run-local text sources."""

    result: list[PreparedSource] = []
    for index, raw in enumerate(items):
        item = dict(raw)
        already_persisted = bool(item.get("already_persisted"))
        supplied_source_id = str(item.get("source_id") or "").strip()
        supplied_content = item.get("source_content")
        supplied_fingerprint = str(item.get("source_fingerprint") or "").strip()
        if already_persisted:
            missing = [
                name
                for name, present in (
                    ("source_id", bool(supplied_source_id)),
                    ("source_content", isinstance(supplied_content, str) and bool(supplied_content.strip())),
                    ("source_fingerprint", bool(supplied_fingerprint)),
                )
                if not present
            ]
            if missing:
                raise ValueError(
                    f"Persisted source at index {index} is missing stable fields: {missing}"
                )
        identity = str(
            item.get("attachment_id")
            or item.get("original_ref")
            or item.get("name")
            or f"source_{index + 1}"
        )
        content = str(supplied_content) if already_persisted else str(supplied_content or "") or _source_text(item)
        if not content.strip():
            continue
        actual_fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if already_persisted and supplied_fingerprint != actual_fingerprint:
            raise ValueError(
                f"Persisted source at index {index} does not match its source_fingerprint"
            )
        source_id = supplied_source_id or f"evc_{_hash({'identity': identity, 'content': content})[:16]}"
        title = str(item.get("name") or identity)
        kind = str(item.get("evidence_type") or item.get("type") or item.get("content_kind") or "unknown")
        classification = _source_attribute(item, "classification") or "unclear"
        credibility = _normalize_credibility(_source_attribute(item, "credibility"))
        raw_provenance = item.get("provenance") or {}
        if not isinstance(raw_provenance, Mapping):
            raise ValueError(f"Source provenance at index {index} must be a mapping")
        observed_at = str(item.get("observed_at") or "")
        upstream_revision = str(item.get("upstream_revision") or item.get("write_date") or "")
        record_model = str(item.get("record_model") or "").strip()
        explicit_record_revision = str(item.get("record_revision") or "").strip()
        record_revision = (
            explicit_record_revision or upstream_revision
            if record_model
            else explicit_record_revision
        )
        upstream_revision = upstream_revision or record_revision
        if bool(record_model) != bool(record_revision):
            raise ValueError("Structured records require model and revision together")
        if (record_model or item.get("record_revision")) and not already_persisted:
            raise ValueError("Structured records must be admitted before prepare_sources")
        provenance = {
            **dict(raw_provenance),
            "runtime_admission": "admitted",
            "attachment_id": str(item.get("attachment_id") or ""),
            "original_ref": str(item.get("original_ref") or ""),
            "source_sha256": str(item.get("sha256") or ""),
            "content_sha256": actual_fingerprint,
            "extraction_ref": str(item.get("extraction_ref") or ""),
            "extraction_sha256": str(item.get("extraction_sha256") or ""),
            "preview_paths": list(item.get("preview_paths") or []),
            "scope": "system_chain_of_custody_only_not_real_world_authenticity",
            "observed_at": observed_at,
            "upstream_revision": upstream_revision,
        }
        provenance = {key: value for key, value in provenance.items() if value not in ("", [], None)}
        result.append(
            PreparedSource(
                record=SourceRecord(
                    source_id=source_id,
                    title=title,
                    kind="record" if record_model else kind,
                    content=content,
                    provenance=provenance,
                    record_model=record_model,
                    record_revision=record_revision if record_model else "",
                ),
                metadata={
                    "attachment_id": str(item.get("attachment_id") or ""),
                    "original_ref": str(item.get("original_ref") or ""),
                    "source_filename": title,
                    "preview_paths": list(item.get("preview_paths") or []),
                    "extraction_ref": str(item.get("extraction_ref") or ""),
                    "source_doc_id": source_id,
                    "classification": classification,
                    "credibility": credibility,
                    "should_accept": _optional_bool(_source_attribute(item, "should_accept")),
                    "manifest_status": str(item.get("manifest_status") or ""),
                    "source_status": str(item.get("status") or ""),
                    "source": str(item.get("source") or ("attachment" if not already_persisted else "")),
                    "already_persisted": already_persisted,
                    "source_fingerprint": supplied_fingerprint or actual_fingerprint,
                    "observed_at": observed_at,
                    "upstream_revision": upstream_revision,
                    "target_record_ref": str(item.get("target_record_ref") or ""),
                },
            )
        )
    by_id: dict[str, PreparedSource] = {}
    for item in result:
        source_id = item.record.source_id
        existing = by_id.get(source_id)
        if existing is None:
            by_id[source_id] = item
            continue
        if existing.record.content != item.record.content:
            raise ValueError(f"Source id {source_id!r} identifies conflicting content")
        if item.metadata.get("already_persisted") and not existing.metadata.get("already_persisted"):
            by_id[source_id] = item
    return [by_id[key] for key in sorted(by_id)]


def _initial_sandbox(
    *,
    plan: ProofPlan,
    prepared_sources: Sequence[PreparedSource],
    policy_excerpt: dict[str, Any],
) -> EvidenceSandbox:
    source_records = [item.record for item in prepared_sources]
    check_nodes = [node for node in plan.nodes if node.kind == "CHECK"]
    return EvidenceSandbox(
        sources=source_records,
        allowed_check_ids=[node.id for node in check_nodes],
        allowed_check_facets={node.id: node.facet_refs for node in check_nodes},
        allowed_check_policy_refs={node.id: node.policy_refs for node in check_nodes},
        policy_values=_configured_policy_values(policy_excerpt),
        policy_snapshot_hash=policy_hash(policy_excerpt),
        evidence_ir=EvidenceIR(
            schema_version=("2" if any(item.record_model for item in source_records) else "1"),
            source_ids=sorted(item.source_id for item in source_records),
            source_fingerprints={
                item.record.source_id: str(item.metadata["source_fingerprint"])
                for item in prepared_sources
            },
            source_revisions={
                item.source_id: item.record_revision
                for item in source_records
                if item.record_revision
            },
            source_descriptors={
                item.record.source_id: _source_descriptor(
                    item.record,
                    str(item.metadata["source_fingerprint"]),
                )
                for item in prepared_sources
            },
        ),
        erp_check_contracts={
            node.id: node.action_contract
            for node in check_nodes
            if isinstance(node.action_contract, ERPReviewContract)
        },
    )


def _source_descriptor(
    source: SourceRecord,
    fingerprint: str,
) -> EvidenceSourceDescriptor:
    source_type: Literal["document", "policy", "record", "derived_fact"]
    if source.record_model.startswith("derived."):
        source_type = "derived_fact"
    elif source.record_model:
        source_type = "record"
    elif source.kind in {"policy", "policy_excerpt"}:
        source_type = "policy"
    else:
        source_type = "document"
    return EvidenceSourceDescriptor(
        source_type=source_type,
        fingerprint=fingerprint,
        revision=source.record_revision,
        record_model=source.record_model,
        provenance=dict(source.provenance),
    )


def _normalize_focus_check_ids(
    value: str | Sequence[str],
    allowed_check_ids: Sequence[str],
) -> list[str]:
    requested = [value] if isinstance(value, str) else list(value)
    normalized = [str(item).strip() for item in requested]
    if not normalized or any(not item for item in normalized):
        raise ValueError("Focused CHECK ids must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError("Focused CHECK ids must not contain duplicates")
    outside = sorted(set(normalized) - set(allowed_check_ids))
    if outside:
        raise ValueError(f"Focus references CHECKs outside the ProofPlan: {outside}")
    return normalized


def _evidence_execution_payload(nodes: Sequence[Any], sandbox: EvidenceSandbox, check_ids: Sequence[str]) -> dict[str, Any]:
    """Present the sealed work without duplicating the proposal in every CHECK."""
    records = {}
    checks = []
    for node in nodes:
        contract = node.action_contract
        for record in contract.proposal_records:
            records[(record.action_id, record.record_ref)] = record.model_dump(mode="json")
        checks.append({
            "check_id": node.id,
            "instruction": node.statement,
            "execution_mode": contract.execution_mode,
            "requires_calculation": contract.requires_calculation,
            "numeric_decision": contract.numeric_decision.model_dump(mode="json") if contract.numeric_decision else None,
            "facet_ref": node.facet_refs[0],
            "action_id": contract.owner_action_id,
            "action_kind": contract.action_kind,
            "targets": contract.target_record_refs,
            "source_ids": contract.source_refs,
            "upstream_check_ids": node.upstream_check_ids,
            "evidence": [group.model_dump(mode="json") for group in contract.resolver_program.evidence],
        })
    allowed = {ref for node in nodes for ref in node.action_contract.source_refs}
    return {
        "checks": checks,
        "focus_check_ids": list(check_ids),
        "terminal_submission_contract": {
            "direct_support": "submit one CHECK_SATISFIED binding",
            "direct_refutation": "submit one CHECK_VIOLATED binding only when admitted evidence establishes the opposite or a failed predicate",
            "missing_or_ambiguous": "submit no terminal binding and state the exact gap in note",
        },
        "proposal_records": list(records.values()),
        "sources": [sandbox.read_source(ref)["source"] for ref in sorted(allowed)],
        "calculation_operation_protocol": _calculation_operation_protocol(),
        "existing_proof_terms": _submitted_proof_terms(sandbox, check_ids=set(check_ids)),
    }


def _erp_execution_program(
    focused_nodes: Sequence[Any],
    *,
    resolved_inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compile preflighted ERP CHECKs into calls the Executor only substitutes."""

    steps = []
    for position, node in enumerate(focused_nodes, start=1):
        contract = node.action_contract
        if not isinstance(contract, ERPReviewContract):
            raise TypeError("ERP execution programs require ERPReviewContract CHECKs")
        check_id = node.id
        if check_id not in resolved_inputs:
            raise ValueError(f"ERP CHECK {check_id!r} has no preflighted resolver inputs")
        if len(node.facet_refs) != 1:
            raise ValueError(f"ERP CHECK {check_id!r} requires exactly one submission facet")
        relation_by_status = {
            status: relation for relation, status in contract.terminal_relations.items()
        }
        steps.append(
            {
                "step_id": f"check_{position}",
                "check_id": check_id,
                "bound_source_refs": list(contract.source_refs),
                "resolved_inputs_hash": _hash(resolved_inputs[check_id]),
                "run_call": {
                    "tool": "run_registered_check",
                    "arguments": {"check_id": check_id},
                    "capture": "resolver_witness",
                },
                "derive": {
                    "witness_id": "$resolver_witness.id",
                    "terminal_relation": {
                        "from": "$resolver_witness.result",
                        "true": relation_by_status["SUPPORTED"],
                        "false": relation_by_status["CONTRADICTED"],
                    },
                },
                "submit_call": {
                    "tool": "submit_check",
                    "arguments": {
                        "check_id": check_id,
                        "claim_ids": [],
                        "witness_ids": ["$resolver_witness.id"],
                        "binding_proposals": [
                            {
                                "id": (
                                    "binding:resolver:"
                                    f"{contract.immutable_contract_hash[:16]}"
                                ),
                                "check_id": check_id,
                                "facet_ref": node.facet_refs[0],
                                "relation": "$terminal_relation",
                                "term_refs": [
                                    {
                                        "kind": "WITNESS",
                                        "ref_id": "$resolver_witness.id",
                                    }
                                ],
                                "reason": (
                                    "Bind the registered resolver result to this immutable "
                                    "ERP CHECK contract."
                                ),
                            }
                        ],
                    },
                    "repeat_unchanged": 1,
                },
            }
        )
    return {
        "schema_version": 1,
        "mode": "STRICT_REGISTERED_CALLS",
        "allowed_tools": ["run_registered_check", "submit_check"],
        "substitution_only": True,
        "steps": steps,
    }


def _ordered_check_ids(plan: ProofPlan) -> list[str]:
    checks = {node.id: node for node in plan.nodes if node.kind == "CHECK"}
    return list(
        TopologicalSorter(
            {
                check_id: set(node.upstream_check_ids)
                for check_id, node in checks.items()
            }
        ).static_order()
    )


def _registered_batch_check_ids(
    plan: ProofPlan,
    remaining_check_ids: Sequence[str],
) -> list[str]:
    """Keep one registered capability DAG in one logical Executor revision."""

    if not remaining_check_ids:
        return []
    nodes = {node.id: node for node in plan.nodes}
    contracts = [nodes[check_id].action_contract for check_id in remaining_check_ids]
    if not all(contracts):
        return []
    capabilities = {contract.capability_id for contract in contracts if contract}
    return list(remaining_check_ids) if len(capabilities) == 1 else []


def _transitive_upstream_check_ids(plan: ProofPlan, check_id: str) -> list[str]:
    checks = {node.id: node for node in plan.nodes if node.kind == "CHECK"}
    if check_id not in checks:
        raise ValueError(f"Unknown CHECK id: {check_id!r}")
    upstream: set[str] = set()
    pending = list(checks[check_id].upstream_check_ids)
    while pending:
        upstream_id = pending.pop()
        if upstream_id in upstream:
            continue
        upstream.add(upstream_id)
        pending.extend(checks[upstream_id].upstream_check_ids)
    return [item for item in _ordered_check_ids(plan) if item in upstream]


def _proof_terms_by_ids(
    sandbox: EvidenceSandbox,
    *,
    claim_ids: Iterable[str] = (),
    binding_ids: Iterable[str] = (),
    witness_ids: Iterable[str] = (),
    term_refs: Iterable[ProofTermRef] = (),
) -> dict[str, Any]:
    claims = {item.id: item for item in sandbox.evidence_ir.claims}
    bindings = {item.id: item for item in sandbox.binding_proposals}
    witnesses = {
        item.id: item
        for item in [*sandbox.calculation_witnesses, *sandbox.resolver_witnesses]
    }
    selected_claim_ids = sorted(set(claim_ids))
    selected_binding_ids = sorted(set(binding_ids))
    selected_witness_ids = sorted(set(witness_ids))
    pending = [*term_refs, *(ref for key in selected_binding_ids if key in bindings for ref in bindings[key].term_refs)]
    pending.extend(ProofTermRef(kind="WITNESS", ref_id=key) for key in selected_witness_ids)
    visited: set[str] = set()
    while pending:
        ref = pending.pop()
        if ref.kind == "CLAIM":
            selected_claim_ids.append(ref.ref_id)
        elif ref.kind == "WITNESS" and ref.ref_id not in visited:
            visited.add(ref.ref_id)
            selected_witness_ids.append(ref.ref_id)
            witness = witnesses.get(ref.ref_id)
            if isinstance(witness, CalculationWitness):
                pending.extend(operand.ref for operand in witness.operands)
    selected_claim_ids = sorted(set(selected_claim_ids))
    selected_witness_ids = sorted(set(selected_witness_ids))
    return {
        "claim_ids": selected_claim_ids,
        "claims": [
            claims[item].model_dump(mode="json")
            for item in selected_claim_ids
            if item in claims
        ],
        "binding_ids": selected_binding_ids,
        "bindings": [
            bindings[item].model_dump(mode="json")
            for item in selected_binding_ids
            if item in bindings
        ],
        "witness_ids": selected_witness_ids,
        "witnesses": [
            witnesses[item].model_dump(mode="json")
            for item in selected_witness_ids
            if item in witnesses
        ],
    }


def _submitted_proof_terms(
    sandbox: EvidenceSandbox,
    *,
    check_ids: set[str],
) -> dict[str, Any]:
    submissions = [item for item in sandbox.latest_submissions() if item.check_id in check_ids]
    return _proof_terms_by_ids(
        sandbox,
        claim_ids=(item for submission in submissions for item in submission.claim_ids),
        binding_ids=(item for submission in submissions for item in submission.binding_ids),
        witness_ids=(item for submission in submissions for item in submission.witness_ids),
    )


def _upstream_frontier_results(
    *,
    plan: ProofPlan,
    check_id: str,
    sandbox: EvidenceSandbox,
    assessments: Sequence[CheckAssessment],
    proof: CompiledProof,
) -> list[dict[str, Any]]:
    nodes = {node.id: node for node in plan.nodes}
    direct_upstream = set(nodes[check_id].upstream_check_ids)
    assessments_by_id = {item.check_id: item for item in assessments}
    results_by_id = {item.node_id: item for item in proof.node_results}
    results: list[dict[str, Any]] = []
    for upstream_id in _transitive_upstream_check_ids(plan, check_id):
        assessment = assessments_by_id.get(upstream_id)
        node_result = results_by_id.get(upstream_id)
        committed = bool(
            assessment is not None
            and node_result is not None
            and assessment.status == node_result.status
        )
        terms = (
            _proof_terms_by_ids(
                sandbox,
                claim_ids=node_result.claim_ids,
                binding_ids=node_result.binding_ids,
                witness_ids=node_result.witness_ids,
            )
            if committed and node_result is not None
            else _proof_terms_by_ids(sandbox)
        )
        results.append(
            {
                "check_id": upstream_id,
                "direct_dependency": upstream_id in direct_upstream,
                "statement": nodes[upstream_id].statement,
                "facet_refs": list(nodes[upstream_id].facet_refs),
                "semantic_role_refs": list(nodes[upstream_id].semantic_role_refs),
                "committed": committed,
                "accepted_terms": terms,
            }
        )
    return results


def _downstream_check_ids(plan: ProofPlan, target_check_id: str) -> set[str]:
    affected = {target_check_id}
    changed = True
    while changed:
        changed = False
        for node in plan.nodes:
            if node.kind != "CHECK" or node.id in affected:
                continue
            if affected.intersection(node.upstream_check_ids):
                affected.add(node.id)
                changed = True
    return affected


def _correction_feedback(
    plan: ProofPlan,
    check_id: str,
    corrections: Sequence[CompilerCorrection],
) -> list[dict[str, Any]]:
    feedback: list[dict[str, Any]] = []
    for correction in corrections:
        if correction.kind != "RECHECK":
            continue
        if check_id not in _downstream_check_ids(plan, correction.target_check_id):
            continue
        feedback.append(
            {
                "diagnostic_code": "HUMAN_RECHECK_REQUESTED",
                "check_id": check_id,
                "target_check_id": correction.target_check_id,
                "message": correction.message or "A human requested another evidence-grounded review.",
                "evidence_refs": list(correction.evidence_refs),
            }
        )
    return feedback


def _artifact(
    *,
    plan: ProofPlan,
    evidence_ir: EvidenceIR,
    assessments: list[CheckAssessment],
    submitted_claim_refs: Mapping[str, Sequence[str]],
    submitted_binding_refs: Mapping[str, Sequence[str]] | None = None,
    submitted_witness_refs: Mapping[str, Sequence[str]] | None = None,
    policy_excerpt: dict[str, Any],
    model: str,
    sandbox: EvidenceSandbox | None = None,
    execution_status: ExecutionStatus = "COMPLETED",
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
    proposal_hash: str = "",
) -> ReviewArtifact:
    # History remains in receipts; only the active submission's transitive proof
    # closure is published or restored into the next candidate revision.
    active_bindings = {ref for refs in (submitted_binding_refs or {}).values() for ref in refs}
    active_witnesses = {ref for refs in (submitted_witness_refs or {}).values() for ref in refs}
    active_claims = {ref for refs in submitted_claim_refs.values() for ref in refs}
    if sandbox is not None:
        witness_by_id = {item.id: item for item in [*sandbox.calculation_witnesses, *sandbox.resolver_witnesses]}
        refs = [ref for item in sandbox.binding_proposals if item.id in active_bindings for ref in item.term_refs]
        refs.extend(ProofTermRef(kind="WITNESS", ref_id=ref) for ref in active_witnesses)
        visited = set()
        while refs:
            ref = refs.pop()
            if ref.kind == "CLAIM":
                active_claims.add(ref.ref_id)
            elif ref.kind == "WITNESS" and ref.ref_id not in visited:
                visited.add(ref.ref_id)
                active_witnesses.add(ref.ref_id)
                witness = witness_by_id.get(ref.ref_id)
                if isinstance(witness, CalculationWitness):
                    refs.extend(operand.ref for operand in witness.operands)
        evidence_ir = evidence_ir.model_copy(update={"claims": [item for item in evidence_ir.claims if item.id in active_claims]})
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        requirement_pack_id=requirement_pack.pack_id,
        requirement_pack_version=requirement_pack.version,
        requirement_pack_hash=requirement_pack.content_hash,
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids, requirement_pack),
        evidence_ir=evidence_ir,
        source_snapshot_hash=evidence_ir.source_snapshot_hash(),
        evidence_snapshot_hash=evidence_ir.content_hash(),
        proposal_hash=proposal_hash,
        assessments=assessments,
        binding_proposals=[item for item in sandbox.binding_proposals if item.id in active_bindings] if sandbox is not None else [],
        calculation_witnesses=[item for item in sandbox.calculation_witnesses if item.id in active_witnesses] if sandbox is not None else [],
        resolver_witnesses=[item for item in sandbox.resolver_witnesses if item.id in active_witnesses] if sandbox is not None else [],
        submitted_claim_refs={
            check_id: _unique(claim_ids)
            for check_id, claim_ids in sorted(submitted_claim_refs.items())
        },
        submitted_binding_refs={
            check_id: _unique(binding_ids)
            for check_id, binding_ids in sorted((submitted_binding_refs or {}).items())
        },
        submitted_witness_refs={
            check_id: _unique(witness_ids)
            for check_id, witness_ids in sorted((submitted_witness_refs or {}).items())
        },
        policy_hash=policy_hash(policy_excerpt),
        policy_snapshot=canonical_policy_snapshot(policy_excerpt),
        resolved_policy_terms={
            ref_id: value
            for ref_id, value in (
                sandbox.resolved_policy_terms
                if sandbox is not None
                else _configured_policy_values(policy_excerpt)
            ).items()
            if ref_id in plan.policy_refs
        },
        unconfigured_policy_refs=_unconfigured_policy_refs(
            plan,
            policy_excerpt,
            requirement_pack,
        ),
        execution_status=execution_status,
        compiler_version=COMPILER_VERSION,
        model=model,
        prompt_versions=PROMPT_VERSIONS,
    )
    return artifact.model_copy(update={"artifact_hash": artifact.content_hash()})


def _submitted_claim_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    return {
        submission.check_id: _unique(submission.claim_ids)
        for submission in sandbox.latest_submissions()
    }


def _submitted_binding_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    return {
        submission.check_id: _unique(submission.binding_ids)
        for submission in sandbox.latest_submissions()
    }


def _submitted_witness_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    return {
        submission.check_id: _unique(submission.witness_ids)
        for submission in sandbox.latest_submissions()
    }


def _check_submission_count(sandbox: EvidenceSandbox, check_id: str) -> int:
    return sum(1 for item in sandbox.submissions if item.check_id == check_id)


def _focused_candidate_scope_violation(
    before: EvidenceSandbox,
    after: EvidenceSandbox,
    *,
    focused_check_ids: Sequence[str],
) -> dict[str, Any] | None:
    """Validate a focused candidate without assigning global Claims by fiat.

    CHECK-owned tools are blocked in ``EvidenceSandbox``. Claims intentionally
    remain globally typed observations, so every Claim created during a focused
    run must be reachable from a newly submitted focused CHECK (directly,
    through a submitted Binding, or through a submitted Witness DAG).
    """

    focused = set(focused_check_ids)
    before_claims = list(before.evidence_ir.claims)
    after_claims = list(after.evidence_ir.claims)
    before_bindings = list(before.binding_proposals)
    after_bindings = list(after.binding_proposals)
    before_witnesses = [*before.calculation_witnesses, *before.resolver_witnesses]
    after_witnesses = [*after.calculation_witnesses, *after.resolver_witnesses]
    before_submissions = list(before.submissions)
    after_submissions = list(after.submissions)

    frozen_prefixes = {
        "claim": (before_claims, after_claims),
        "binding": (before_bindings, after_bindings),
        "witness": (before_witnesses, after_witnesses),
        "submission": (before_submissions, after_submissions),
    }
    changed_prefixes = sorted(
        kind
        for kind, (frozen, candidate) in frozen_prefixes.items()
        if len(candidate) < len(frozen) or candidate[: len(frozen)] != frozen
    )
    if changed_prefixes:
        return {
            "scope_error": "PREEXISTING_PROOF_MATERIAL_CHANGED",
            "changed_material_kinds": changed_prefixes,
        }

    new_claims = after_claims[len(before_claims) :]
    new_bindings = after_bindings[len(before_bindings) :]
    new_witnesses = after_witnesses[len(before_witnesses) :]
    new_submissions = after_submissions[len(before_submissions) :]

    outside_focus = sorted(
        {
            item.check_id
            for item in [*new_bindings, *new_witnesses, *new_submissions]
            if item.check_id not in focused
        }
    )
    if outside_focus:
        return {
            "scope_error": "NON_FOCUSED_PROOF_MATERIAL_ADDED",
            "non_focused_check_ids": outside_focus,
        }

    binding_by_id = {item.id: item for item in after_bindings}
    witness_by_id = {item.id: item for item in after_witnesses}
    owned_claim_ids: set[str] = set()
    owned_binding_ids: set[str] = set()
    owned_witness_ids: set[str] = set()

    def admit_ref(ref: ProofTermRef) -> None:
        if ref.kind == "CLAIM":
            owned_claim_ids.add(ref.ref_id)
            return
        if ref.kind != "WITNESS" or ref.ref_id in owned_witness_ids:
            return
        witness = witness_by_id.get(ref.ref_id)
        if witness is None:
            return
        owned_witness_ids.add(witness.id)
        if isinstance(witness, ResolverWitness):
            return
        for operand in witness.operands:
            admit_ref(operand.ref)

    for submission in new_submissions:
        if submission.check_id not in focused:
            continue
        owned_claim_ids.update(submission.claim_ids)
        for binding_id in submission.binding_ids:
            binding = binding_by_id.get(binding_id)
            if binding is None:
                continue
            owned_binding_ids.add(binding.id)
            for ref in binding.term_refs:
                admit_ref(ref)
        for witness_id in submission.witness_ids:
            admit_ref(ProofTermRef(kind="WITNESS", ref_id=witness_id))

    orphan_claim_ids = sorted({item.id for item in new_claims} - owned_claim_ids)
    orphan_binding_ids = sorted({item.id for item in new_bindings} - owned_binding_ids)
    orphan_witness_ids = sorted({item.id for item in new_witnesses} - owned_witness_ids)
    if orphan_claim_ids or orphan_binding_ids or orphan_witness_ids:
        return {
            "scope_error": "UNOWNED_FOCUSED_PROOF_MATERIAL",
            "orphan_claim_ids": orphan_claim_ids,
            "orphan_binding_ids": orphan_binding_ids,
            "orphan_witness_ids": orphan_witness_ids,
        }
    return None


def _derived_execution_status(
    plan: ProofPlan,
    sandbox: EvidenceSandbox,
    assessments: Sequence[CheckAssessment],
) -> ExecutionStatus:
    check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
    submitted_ids = {item.check_id for item in sandbox.submissions}
    assessed_ids = {item.check_id for item in assessments}
    if check_ids and check_ids <= submitted_ids and check_ids <= assessed_ids:
        return "COMPLETED"
    if submitted_ids or assessed_ids:
        return "PARTIAL"
    return "FAILED"


def _terminal_semantic_status(
    proof: CompiledProof,
    compile_status: CompileStatus,
) -> AssessmentStatus | None:
    if compile_status != "COMMITTED":
        return None
    statuses = {item.status for item in proof.decisions}
    if "CONTRADICTED" in statuses:
        return "CONTRADICTED"
    if "NOT_FOUND" in statuses:
        return "NOT_FOUND"
    return "SUPPORTED" if statuses else None


def _calculation_operation_protocol() -> dict[str, dict[str, Any]]:
    """Expose existing deterministic-engine semantics next to model inputs."""

    return {
        "EQUAL": {
            "ordered_semantics": "refs[0] == refs[1]",
            "equality_result": True,
            "symmetric": True,
        },
        "GREATER_THAN": {
            "ordered_semantics": "refs[0] > refs[1]",
            "equality_result": False,
            "symmetric": False,
        },
        "GTE": {
            "ordered_semantics": "refs[0] >= refs[1]",
            "equality_result": True,
            "symmetric": False,
        },
        "LTE": {
            "ordered_semantics": "refs[0] <= refs[1]",
            "equality_result": True,
            "symmetric": False,
        },
    }


def _strong_status_link_protocol() -> dict[str, str]:
    """Describe the typed polarity field without adding a business rule."""

    return {
        "true_status": (
            "the CHECK classification that would follow if the linked boolean Witness "
            "replayed to true; it is not the current classification"
        ),
        "false_status": "the opposite strong classification, derived by the Proof Kernel",
    }


def _upsert_focused_assessment(
    plan: ProofPlan,
    current: Sequence[CheckAssessment],
    replacements: Sequence[CheckAssessment],
    *,
    check_id: str,
) -> list[CheckAssessment]:
    """Upsert exactly one focused assessment in stable ProofPlan CHECK order."""

    if len(replacements) != 1 or replacements[0].check_id != check_id:
        actual = [item.check_id for item in replacements]
        raise ValueError(
            f"Focused Fine Verifier must return exactly {check_id!r}: got={actual}"
        )
    ordered_check_ids = [node.id for node in plan.nodes if node.kind == "CHECK"]
    allowed = set(ordered_check_ids)
    current_by_id = {item.check_id: item for item in current}
    if len(current_by_id) != len(current):
        raise ValueError("Committed checkpoint contains duplicate CHECK assessments")
    unknown = sorted(set(current_by_id) - allowed)
    if unknown:
        raise ValueError(f"Committed checkpoint contains unknown CHECK assessments: {unknown}")
    current_by_id[check_id] = replacements[0]
    return [current_by_id[item] for item in ordered_check_ids if item in current_by_id]


def _frontier_feedback(
    check_id: str,
    *,
    code: str,
    message: str,
    node_id: str = "",
    previous_assessment: CheckAssessment | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "check_id": check_id,
        "diagnostic_code": code,
        "kernel_message": message,
    }
    if node_id:
        feedback["node_id"] = node_id
    if previous_assessment is not None:
        feedback["previous_assessment"] = previous_assessment.model_dump(mode="json")
    return feedback


def _verifier_rejected_latest_submission(
    sandbox: EvidenceSandbox,
    assessment: CheckAssessment,
) -> dict[str, Any] | None:
    """Distinguish an inadequate proof attempt from an actual evidence gap."""

    if assessment.status != "NOT_FOUND":
        return None
    submission = next(
        (item for item in reversed(sandbox.submissions) if item.check_id == assessment.check_id),
        None,
    )
    if submission is None:
        return None
    if assessment.gap_code in {"BINDING_MISSING", "WITNESS_MISSING"}:
        return _frontier_feedback(
            assessment.check_id, code="VERIFIER_PROOF_REPAIR_REQUIRED",
            message="Verifier found an incomplete proof despite available materials. Re-examine the original evidence and diagnosis; repair the proof or explain a genuine source gap.",
            previous_assessment=assessment,
        )
    rejected_bindings = sorted(set(submission.binding_ids) - set(assessment.accepted_binding_ids))
    rejected_witnesses = sorted(set(submission.witness_ids) - set(assessment.accepted_witness_ids))
    if not rejected_bindings and not rejected_witnesses:
        return None
    feedback = _frontier_feedback(
        assessment.check_id,
        code="VERIFIER_REJECTED_SUBMITTED_PROOF_TERM",
        message=(
            "Verifier rejected typed terms in the latest submission. Correct the proof attempt, "
            "or submit a clean NOT_FOUND result if the stated premise is genuinely absent."
        ),
        previous_assessment=assessment,
    )
    feedback["rejected_binding_ids"] = rejected_bindings
    feedback["rejected_witness_ids"] = rejected_witnesses
    return feedback


def _frontier_kernel_failures(
    *,
    check_id: str,
    committed_assessments: Sequence[CheckAssessment],
    committed_proof: CompiledProof,
    focused_assessment: CheckAssessment,
    candidate_proof: CompiledProof,
) -> list[dict[str, Any]]:
    """Return only failures that can invalidate the current committed frontier.

    Diagnostics for future, unexecuted CHECKs are expected during full Kernel
    replay and cannot block the current transaction. Artifact-global failures,
    the focused CHECK, and any already committed CHECK remain fail-closed.
    """

    committed_check_ids = {item.check_id for item in committed_assessments}
    protected_check_ids = committed_check_ids | {check_id}
    candidate_results = {item.node_id: item for item in candidate_proof.node_results}
    assessments_by_id = {
        item.check_id: item for item in committed_assessments
    } | {check_id: focused_assessment}
    failures = [
        _frontier_feedback(
            check_id,
            code=diagnostic.code,
            message=diagnostic.message,
            node_id=diagnostic.node_id,
            previous_assessment=assessments_by_id.get(diagnostic.node_id),
        )
        for diagnostic in candidate_proof.diagnostics
        if (not diagnostic.node_id or diagnostic.node_id in protected_check_ids)
        and not _admissible_policy_gap_diagnostic(
            diagnostic_code=diagnostic.code,
            assessment=assessments_by_id.get(diagnostic.node_id),
            result=candidate_results.get(diagnostic.node_id),
        )
    ]

    focused_result = candidate_results.get(check_id)
    if focused_result is None or focused_result.status != focused_assessment.status:
        failures.append(
            _frontier_feedback(
                check_id,
                code="KERNEL_RESULT_MISMATCH",
                message=(
                    "Kernel did not project the focused Verifier status exactly: "
                    f"assessment={focused_assessment.status}, "
                    f"result={getattr(focused_result, 'status', None)}"
                ),
                node_id=check_id,
                previous_assessment=focused_assessment,
            )
        )

    committed_results = {item.node_id: item for item in committed_proof.node_results}
    for committed_check_id in sorted(committed_check_ids):
        before = committed_results.get(committed_check_id)
        after = candidate_results.get(committed_check_id)
        if (
            before is None
            or after is None
            or before.model_dump(mode="json") != after.model_dump(mode="json")
        ):
            failures.append(
                _frontier_feedback(
                    check_id,
                    code="COMMITTED_CHECK_CHANGED",
                    message=(
                        f"Candidate for {check_id!r} changed committed CHECK "
                        f"{committed_check_id!r}."
                    ),
                    node_id=committed_check_id,
                    previous_assessment=focused_assessment,
                )
            )
    return failures


def _admissible_policy_gap_diagnostic(
    *,
    diagnostic_code: str,
    assessment: CheckAssessment | None,
    result: Any | None,
) -> bool:
    """Admit only the Kernel's explicit, typed unconfigured-policy business gap."""

    return bool(
        diagnostic_code == "POLICY_NOT_CONFIGURED"
        and assessment is not None
        and assessment.status == "NOT_FOUND"
        and assessment.gap_code == "POLICY_UNCONFIGURED"
        and assessment.missing_fact
        and result is not None
        and result.status == "NOT_FOUND"
        and result.gap_code == "POLICY_UNCONFIGURED"
    )


def _strong_status_link_boundary_violations(
    assessment: CheckAssessment,
    witnesses: Mapping[str, CalculationWitness],
) -> list[dict[str, Any]]:
    """Detect definite polarity conflicts while preserving verifier output.

    This is deliberately narrower than Kernel validation.  It never invents a
    polarity or edits an assessment: it only makes an already-structured
    contradiction observable before the Kernel performs the authoritative
    fail-closed projection.
    """

    if assessment.status not in {"SUPPORTED", "CONTRADICTED"}:
        return []

    accepted = set(assessment.accepted_witness_ids)
    observed: list[dict[str, Any]] = []
    for link in assessment.strong_status_links:
        witness = witnesses.get(link.witness_id)
        if (
            witness is None
            or link.witness_id not in accepted
            or witness.check_id != assessment.check_id
            or not isinstance(witness.result, bool)
        ):
            continue
        mapped_status = (
            link.true_status
            if witness.result
            else ("CONTRADICTED" if link.true_status == "SUPPORTED" else "SUPPORTED")
        )
        observed.append(
            {
                "witness_id": link.witness_id,
                "witness_result": witness.result,
                "true_status": link.true_status,
                "mapped_status": mapped_status,
            }
        )

    if assessment.status == "SUPPORTED":
        return [item for item in observed if item["mapped_status"] == "CONTRADICTED"]
    if observed and not any(item["mapped_status"] == "CONTRADICTED" for item in observed):
        return observed
    return []


def _sandbox_tools(
    sandbox: EvidenceSandbox,
    *,
    progress_sink: Callable[[str, dict[str, Any] | None], None] | None = None,
    submission_review_by_check: Mapping[str, Mapping[str, Any]] | None = None,
    allowed_source_ids: frozenset[str] | None = None,
    record_fields_only: bool = False,
    reference_ids_only: bool = False,
    resolver_only: bool = False,
    execution_program: Mapping[str, Any] | None = None,
    numeric_checks: Sequence[ProofNode] = (),
) -> list[FunctionTool]:
    compute_input = _ComputeWitnessIdsInput if reference_ids_only else _ComputeWitnessInput
    submit_input = _SubmitEvidenceCheckInput if reference_ids_only else _SubmitCheckInput
    strict_steps = list((execution_program or {}).get("steps") or [])
    planned_checks = {node.id: node for node in numeric_checks
                      if isinstance(node.action_contract, ERPReviewContract)
                      and node.action_contract.numeric_decision
                      and node.action_contract.numeric_decision.steps}
    strict_state: dict[str, Any] = {
        "step": 0,
        "phase": "run",
        "submit_args": None,
        "halted": False,
    }

    def program_violation(action: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "error": {
                "code": "EXECUTION_PROGRAM_VIOLATION",
                "message": message,
                "repair": "Use the next exact call from execution_program without changing its arguments.",
            },
        }

    def source_scope_failure(action: str, source_id: str) -> dict[str, Any] | None:
        if allowed_source_ids is None or source_id in allowed_source_ids:
            return None
        return {
            "ok": False,
            "action": action,
            "error": {
                "code": "SOURCE_OUT_OF_SCOPE",
                "message": f"Source {source_id!r} is outside the focused CHECK contract.",
                "repair": "Use only source_ids listed in the focused source_catalog.",
                "details": {"source_id": source_id},
            },
        }

    async def list_sources(_context: Any, raw: str) -> str:
        _ListSourcesInput.model_validate_json(raw or "{}")
        def invoke() -> dict[str, Any]:
            result = sandbox.list_sources()
            if allowed_source_ids is not None:
                result["sources"] = [
                    item
                    for item in result["sources"]
                    if item["source_id"] in allowed_source_ids
                ]
            return result

        return _observed_tool("list_sources", invoke)

    async def read_source(_context: Any, raw: str) -> str:
        data = _ReadSourceInput.model_validate_json(raw)
        return _observed_tool(
            "read_source",
            lambda: source_scope_failure("read_source", data.source_id)
            or sandbox.read_source(data.source_id),
        )

    async def bind_claim(_context: Any, raw: str) -> str:
        data = _BindClaimInput.model_validate_json(raw)
        return _observed_tool(
            "bind_claim",
            lambda: source_scope_failure("bind_claim", data.source_id)
            or sandbox.bind_claim(**data.model_dump()),
        )

    async def bind_record_field_claim(_context: Any, raw: str) -> str:
        data = _BindRecordFieldClaimInput.model_validate_json(raw)
        return _observed_tool(
            "bind_record_field_claim",
            lambda: source_scope_failure(
                "bind_record_field_claim", data.locator.record_ref
            )
            or sandbox.bind_record_field_claim(
                source_id=data.locator.record_ref, subject=data.locator.record_ref,
                **data.model_dump(),
            ),
        )

    async def bind_record_fields(_context: Any, raw: str) -> str:
        data = _BindRecordFieldsInput.model_validate_json(raw)
        error = source_scope_failure("bind_record_fields", data.record_ref)
        if error:
            return _observed_tool("bind_record_fields", lambda: error)
        results = []
        for field in data.fields:
            result = json.loads(_observed_tool(
                "bind_record_field_claim",
                lambda: sandbox.bind_record_field_claim(
                    source_id=data.record_ref, subject=data.record_ref,
                    locator={"record_ref": data.record_ref, "record_revision": data.record_revision,
                             "field_path": field.field_path},
                    **field.model_dump(exclude={"field_path"}),
                ),
            ))
            if result.get("ok"):
                result["claim"] = {key: result["claim"][key]
                                   for key in ("id", "predicate", "value", "confidence", "attributes")}
            results.append({"field_path": field.field_path, **result})
        return _tool_json({"ok": True, "record_ref": data.record_ref,
                           "record_revision": data.record_revision, "results": results})

    async def compute_witness_tool(_context: Any, raw: str) -> str:
        data = compute_input.model_validate_json(raw)
        return _observed_tool(
            "compute_witness",
            lambda: sandbox.compute_witness(**data.model_dump()),
        )

    async def compute_planned_witnesses(_context: Any, raw: str) -> str:
        data = _RunRegisteredCheckInput.model_validate_json(raw)

        def invoke() -> dict[str, Any]:
            node = planned_checks.get(data.check_id)
            if node is None:
                return program_violation("compute_planned_witnesses", "This focused CHECK has no sealed numeric steps.")
            contract, fields, steps = node.action_contract, {}, {}
            program = contract.numeric_decision
            sources = [source for source in sandbox.source_records
                       if source.source_id in contract.target_record_refs and source.source_id in contract.source_refs
                       and source.record_model == program.record_model]
            if len(sources) != 1 or len(node.facet_refs) != 1:
                return program_violation("compute_planned_witnesses", "The sealed calculation requires one admitted target and facet.")
            source = sources[0]
            revisions = {record.record_revision for record in contract.proposal_records if record.record_ref == source.source_id}
            if revisions != {source.record_revision}:
                return program_violation("compute_planned_witnesses", "The source revision must match the sealed target revision.")
            error = source_scope_failure("compute_planned_witnesses", source.source_id)
            if error:
                return error
            for step in program.steps:
                for operand in step.operands:
                    if operand.kind != "RECORD_FIELD" or operand.ref_id in fields:
                        continue
                    bound = sandbox.bind_record_field_claim(
                        source_id=source.source_id, subject=source.source_id, predicate=operand.ref_id,
                        locator={"record_ref": source.source_id, "record_revision": source.record_revision,
                                 "field_path": operand.ref_id},
                    )
                    if not bound["ok"]:
                        return bound
                    fields[operand.ref_id] = {key: bound["claim"][key] for key in ("id", "value")}
                calculated = sandbox.compute_witness(
                    check_id=node.id, facet_ref=node.facet_refs[0], operation=step.operation,
                    refs=[(steps if operand.kind == "STEP" else fields)[operand.ref_id]["id"]
                          for operand in step.operands],
                )
                if not calculated["ok"]:
                    return calculated
                steps[step.step_id] = {key: calculated["witness"][key] for key in ("id", "result")}
            return {"ok": True, "fields": fields, "steps": steps,
                    "terminal_witness_id": steps[program.steps[-1].step_id]["id"]}

        return _observed_tool("compute_planned_witnesses", invoke)

    async def run_registered_check(_context: Any, raw: str) -> str:
        data = _RunRegisteredCheckInput.model_validate_json(raw)
        def invoke() -> dict[str, Any]:
            if strict_steps:
                if strict_state["halted"]:
                    return program_violation("run_registered_check", "The execution program already halted.")
                if strict_state["step"] >= len(strict_steps):
                    return program_violation("run_registered_check", "The execution program is already complete.")
                step = strict_steps[strict_state["step"]]
                expected = _RunRegisteredCheckInput.model_validate(
                    step["run_call"]["arguments"]
                )
                if strict_state["phase"] != "run" or data != expected:
                    return program_violation(
                        "run_registered_check",
                        f"Expected {step['run_call']['tool']} for CHECK {step['check_id']!r}.",
                    )
            result = sandbox.run_registered_check(check_id=data.check_id)
            if strict_steps:
                if not result.get("ok"):
                    strict_state["halted"] = True
                    return result
                step = strict_steps[strict_state["step"]]
                witness = result["witness"]
                submit_args = copy.deepcopy(step["submit_call"]["arguments"])
                relation_map = step["derive"]["terminal_relation"]
                relation = relation_map["true"] if witness["result"] else relation_map["false"]
                submit_args["witness_ids"] = [witness["id"]]
                binding = submit_args["binding_proposals"][0]
                binding["relation"] = relation
                binding["term_refs"][0]["ref_id"] = witness["id"]
                strict_state["submit_args"] = _SubmitCheckInput.model_validate(
                    submit_args
                ).model_dump(mode="json")
                strict_state["phase"] = "submit_1"
            return result

        return _observed_tool("run_registered_check", invoke)

    async def submit_check(_context: Any, raw: str) -> str:
        data = submit_input.model_validate_json(raw)
        if isinstance(data, _SubmitEvidenceCheckInput):
            try:
                data = _expand_evidence_submission(data, sandbox, (submission_review_by_check or {}).get(data.check_id, {}))
            except ValueError as exc:
                return _observed_tool("submit_check", lambda: {
                    "ok": False, "action": "submit_check", "error": {
                        "code": "EVIDENCE_SELECTION_INVALID", "message": str(exc),
                        "repair": "Select only submitted declared upstream evidence, or submit no binding with an exact gap note.",
                    },
                })
        if progress_sink is not None:
            progress_sink("submit_check", None)
        if strict_steps:
            if strict_state["halted"]:
                result = program_violation("submit_check", "The execution program already halted.")
                if progress_sink is not None:
                    progress_sink("submit_check", result)
                return _tool_json(result)
            if strict_state["step"] >= len(strict_steps):
                result = program_violation("submit_check", "The execution program is already complete.")
                if progress_sink is not None:
                    progress_sink("submit_check", result)
                return _tool_json(result)
            if (
                strict_state["phase"] not in {"submit_1", "submit_2"}
                or data.model_dump(mode="json") != strict_state["submit_args"]
            ):
                step = strict_steps[strict_state["step"]]
                result = program_violation(
                    "submit_check",
                    f"Expected the compiled submit_check arguments for CHECK {step['check_id']!r}.",
                )
                if progress_sink is not None:
                    progress_sink("submit_check", result)
                return _tool_json(result)
        review = dict((submission_review_by_check or {}).get(data.check_id) or {})
        allowed_relations = set(review.get("terminal_relations") or ())
        invalid_relations = sorted(
            {
                proposal.relation
                for proposal in data.binding_proposals
                if allowed_relations and proposal.relation not in allowed_relations
            }
        )
        if invalid_relations:
            result = {
                "ok": False,
                "action": "submit_check",
                "error": {
                    "code": "UNREGISTERED_TERMINAL_RELATION",
                    "message": f"Terminal relations are not registered: {invalid_relations}",
                    "repair": "Use an exact relation key from focused_check.action_contract.terminal_relations; never use its status value.",
                },
            }
            if progress_sink is not None:
                progress_sink("submit_check", result)
            return _tool_json(result)
        if allowed_source_ids is not None:
            claims = {item.id: item for item in sandbox.evidence_ir.claims}
            witnesses = {
                item.id: item
                for item in [
                    *sandbox.calculation_witnesses,
                    *sandbox.resolver_witnesses,
                ]
            }
            source_ids: set[str] = set()
            visiting: set[str] = set()

            def collect_sources(ref: ProofTermRef) -> None:
                if ref.kind == "CLAIM":
                    claim = claims.get(ref.ref_id)
                    if claim is not None:
                        source_ids.add(claim.source_id)
                    return
                if ref.kind != "WITNESS" or ref.ref_id in visiting:
                    return
                witness = witnesses.get(ref.ref_id)
                if witness is None:
                    return
                if isinstance(witness, ResolverWitness):
                    source_ids.update(witness.source_refs)
                    return
                visiting.add(ref.ref_id)
                for operand in witness.operands:
                    collect_sources(operand.ref)
                visiting.remove(ref.ref_id)

            for claim_id in data.claim_ids:
                collect_sources(ProofTermRef(kind="CLAIM", ref_id=claim_id))
            for witness_id in data.witness_ids:
                collect_sources(ProofTermRef(kind="WITNESS", ref_id=witness_id))
            for proposal in data.binding_proposals:
                for ref in proposal.term_refs:
                    collect_sources(ref)
            outside_scope = sorted(source_ids - allowed_source_ids)
            if outside_scope:
                result = source_scope_failure("submit_check", outside_scope[0])
                if progress_sink is not None:
                    progress_sink("submit_check", result)
                return _tool_json(result)
        result = sandbox.submit_check(**data.model_dump())
        if strict_steps:
            if not result.get("ok"):
                strict_state["halted"] = True
            else:
                strict_state["step"] += 1
                strict_state["phase"] = "run"
                strict_state["submit_args"] = None
        if result.get("ok") and review and review.get("contract_kind") != "ERP_CHECK":
            if review.get("contract_kind") == "ERP_CHECK":
                instruction = (
                    "Before Runtime may stop this CHECK, confirm the submitted ResolverWitness "
                    "matches this exact contract and source closure, its terminal Binding uses "
                    "the correct relation for the boolean result, and no extra proof terms were "
                    "included. Then call submit_check once more unchanged."
                )
            else:
                instruction = (
                    "Before Runtime may stop this CHECK, re-read its full boundary and every "
                    "relevant source statement. Confirm that each distinct observation, "
                    "qualifier, treatment, inclusion, or exclusion needed by the CHECK has its "
                    "own grounded Claim; every declared semantic role has the required submitted "
                    "Claim, Binding, or Witness; and every referenced term is included. Do not "
                    "invent missing evidence. Add anything omitted, then call submit_check again "
                    "even when the candidate remains unchanged."
                )
            result["pre_commit_review"] = {
                **review,
                "candidate_committed": False,
                "instruction": instruction,
            }
        if progress_sink is not None:
            progress_sink("submit_check", result)
        return _tool_json(result)

    def _observed_tool(name: str, invoke: Callable[[], dict[str, Any]]) -> str:
        if progress_sink is not None:
            progress_sink(name, None)
        result = invoke()
        if progress_sink is not None:
            progress_sink(name, result)
        return _tool_json(result)

    tools = [
        _function_tool("list_sources", "List the evidence sources available in this run.", _ListSourcesInput, list_sources),
        _function_tool("read_source", "Read one source by source_id before binding claims.", _ReadSourceInput, read_source),
        _function_tool(
            "bind_claim",
            "Append one fact directly observed in an exact document quote to EvidenceIR; never use this tool for structured record fields.",
            _BindClaimInput,
            bind_claim,
        ),
        _function_tool(
            "bind_record_fields" if reference_ids_only else "bind_record_field_claim",
            ("Bind selected fields from one admitted record and revision. Each fields item supplies field_path, predicate and optional claim metadata; never values. Results stay in input order with per-field ok/error. Successful fields are retained even if another fails; retry only failed fields."
             if reference_ids_only else "Bind an observed record field. Runtime reads its exact typed value and derives source_id and subject from locator.record_ref. Do not supply value. Keep record_revision and field_path inside locator, without an extra record_field wrapper."),
            _BindRecordFieldsInput if reference_ids_only else _BindRecordFieldClaimInput,
            bind_record_fields if reference_ids_only else bind_record_field_claim,
        ),
        _function_tool(
            "compute_witness",
            "Compute a deterministic Decimal witness from ordered existing references; no values or results are accepted. EQUAL, GREATER_THAN, GTE, and LTE use their literal ordered Decimal semantics.",
            compute_input,
            compute_witness_tool,
        ),
        _function_tool(
            "run_registered_check",
            "Execute the focused CHECK's frozen registered ERP resolver and return one replayable boolean Witness.",
            _RunRegisteredCheckInput,
            run_registered_check,
        ),
        _function_tool(
            "submit_check",
            ("Submit one candidate binding or no binding with an exact gap note. Runtime derives claim_ids and witness_ids from binding term_refs; do not copy those lists. Optional upstream_check_ids expands already submitted ancestor facts, never verdicts. Independent verification follows."
             if reference_ids_only else "Submit candidate Claim refs, semantic bindings, Witness refs, and missing facts for one CHECK. This is not a verdict or commit. Submit once unless an explicit pre_commit_review asks for revision; an independent Fine Verifier follows."),
            submit_input,
            submit_check,
        ),
    ]
    if reference_ids_only and planned_checks and not resolver_only:
        tools.append(_function_tool(
            "compute_planned_witnesses", "Read the sealed numeric CHECK's exact target fields and run all its planned arithmetic through the existing calculator. Supply only check_id. Returns observed fields and witness IDs, never a business verdict or submission. Missing fields remain errors; no values are invented.",
            _RunRegisteredCheckInput, compute_planned_witnesses,
        ))
    if resolver_only:
        return [
            tool
            for tool in tools
            if tool.name in {"run_registered_check", "submit_check"}
        ]
    if record_fields_only:
        return [
            tool
            for tool in tools
            if tool.name
            in {
                "read_source",
                "bind_record_field_claim",
                "compute_witness",
                "run_registered_check",
                "submit_check",
            }
        ]
    return tools


def _completion_hook(
    sandbox: EvidenceSandbox,
    check_ids: Sequence[str],
    *,
    prior_submission_counts: Mapping[str, int] | None = None,
    require_final_review: bool = False,
) -> Any:
    expected = set(check_ids)
    baseline = dict(prior_submission_counts or {})
    review_rounds = {check_id: 0 for check_id in expected}

    def complete(_context: Any, tool_results: Any) -> ToolsToFinalOutputResult:
        if require_final_review:
            submitted_this_round: set[str] = set()
            for tool_result in list(tool_results or []):
                tool = getattr(tool_result, "tool", None)
                if str(getattr(tool, "name", "") or "") != "submit_check":
                    continue
                output = getattr(tool_result, "output", None)
                try:
                    payload = json.loads(output) if isinstance(output, str) else dict(output or {})
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                submission = payload.get("submission") or {}
                check_id = str(submission.get("check_id") or "")
                if payload.get("ok") and check_id in expected:
                    submitted_this_round.add(check_id)
            for check_id in submitted_this_round:
                review_rounds[check_id] += 1
            if any(review_rounds[check_id] < 2 for check_id in expected):
                return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
        submissions_by_check: dict[str, list[Any]] = {check_id: [] for check_id in expected}
        for item in sandbox.submissions:
            if item.check_id in expected:
                submissions_by_check[item.check_id].append(item)
        if any(
            len(submissions_by_check[check_id]) <= int(baseline.get(check_id, 0))
            for check_id in expected
        ):
            return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
        latest = {check_id: items[-1] for check_id, items in submissions_by_check.items()}
        unresolved = sorted(
            check_id
            for check_id, item in latest.items()
            if not (item.claim_ids or item.binding_ids or item.witness_ids)
        )
        return ToolsToFinalOutputResult(
            is_final_output=True,
            final_output=ExecutorSummary(
                completed_check_ids=sorted(expected - set(unresolved)),
                unresolved_check_ids=unresolved,
                summary="Every executable CHECK was submitted; the Runtime completion hook stopped the worker.",
                execution_status="COMPLETED",
            ),
        )

    return complete


def _function_tool(name: str, description: str, model: type[BaseModel], callback: Any) -> FunctionTool:
    async def invoke(context: Any, raw: str) -> str:
        try:
            return await callback(context, raw)
        except ValidationError as exc:
            return _tool_json(
                {
                    "ok": False,
                    "action": name,
                    "error": {
                        "code": "TOOL_INPUT_INVALID",
                        "message": "Tool arguments are not valid JSON for the declared schema.",
                        "repair": "Retry only this tool call with one strict JSON object matching the tool schema.",
                        "details": {
                            "validation_errors": exc.errors(
                                include_url=False,
                                include_input=False,
                            )
                        },
                    },
                }
            )

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=model.model_json_schema(),
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def _review_result(
    *,
    prepared_sources: Sequence[PreparedSource],
    sandbox: EvidenceSandbox,
    artifact: ReviewArtifact,
    proof: CompiledProof,
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
    compile_status: CompileStatus = "COMMITTED",
    semantic_status: AssessmentStatus | None = None,
) -> dict[str, Any]:
    if compile_status != "COMMITTED":
        return {
            "mode": "review",
            "source_doc_id": ",".join(item.record.source_id for item in prepared_sources),
            "evidence_type": "unknown",
            "credibility": "medium",
            "extracted_fields": {},
            "extraction_result": {},
            "source_traceability": "unclear",
            "support_level": "none",
            "risk_flags": [],
            "should_accept": False,
            "reason": "Compiler did not commit every CHECK; no semantic result is published.",
            "supports": [],
            "conflicts": [],
            "evidence_cards": [],
            "suggested_patch": {},
            "reply_to_user": "Evidence compilation did not converge; inspect the child run before recheck.",
            "compile_status": compile_status,
            "semantic_status": None,
        }
    claims_by_source: dict[str, list[Any]] = {}
    for claim in sandbox.evidence_ir.claims:
        claims_by_source.setdefault(claim.source_id, []).append(claim)
    claims_by_id = {claim.id: claim for claim in sandbox.evidence_ir.claims}
    decisions = {item.requirement_id: item for item in proof.decisions}
    node_results = {item.node_id: item for item in proof.node_results}
    submitted_claim_ids = {
        claim_id
        for submission in sandbox.submissions
        for claim_id in submission.claim_ids
    }
    evidence_items: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for prepared in prepared_sources:
        if prepared.metadata.get("already_persisted"):
            continue
        source_id = prepared.record.source_id
        claims = claims_by_source.get(source_id, [])
        grounded = any(claim.id in submitted_claim_ids for claim in claims)
        explicit_accept = prepared.metadata.get("should_accept")
        should_accept = grounded and explicit_accept is not False
        classification = str(prepared.metadata.get("classification") or "unclear").strip().lower()
        if classification == "unclear" and should_accept:
            classification = "business_evidence"
        credibility = _normalize_credibility(prepared.metadata.get("credibility"))
        supports: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        supported_evidence_types: set[str] = set()
        for requirement_id, decision in decisions.items():
            referenced_claims = [
                claim
                for claim in _decision_claims(decision, node_results, claims_by_id)
                if claim.source_id == source_id
            ]
            if not referenced_claims:
                continue
            quoted_text = "\n".join(_unique(claim.quote for claim in referenced_claims))
            if decision.status == "SUPPORTED":
                declared_evidence_type = requirement_pack.evidence_type(requirement_id)
                if declared_evidence_type:
                    supported_evidence_types.add(declared_evidence_type)
                supports.append(
                    {
                        "requirement": requirement_id,
                        "support_level": "full",
                        "quoted_text": quoted_text,
                    }
                )
            elif decision.status == "CONTRADICTED":
                conflicts.append(
                    {
                        "type": "proof_contradiction",
                        "requirement": requirement_id,
                        "severity": "high",
                        "description": decision.stop_reason,
                        "quoted_text": quoted_text,
                        "affected_evidence_ids": [source_id],
                    }
                )
        evidence_type = prepared.record.kind if prepared.record.kind in _EVIDENCE_TYPES else "unknown"
        if evidence_type == "unknown" and len(supported_evidence_types) == 1:
            candidate_type = next(iter(supported_evidence_types))
            if candidate_type in _EVIDENCE_TYPES:
                evidence_type = candidate_type
        item = {
            "id": source_id,
            "type": evidence_type,
            "credibility": credibility,
            "summary": f"Compiler-reviewed source with {len(claims)} grounded claim(s).",
            "source": str(prepared.metadata.get("source") or "attachment"),
            "content": prepared.record.content,
            "review_result": {
                "should_accept": should_accept,
                "reason": "Source has grounded Claims submitted to a proof check." if should_accept else "Source was not submitted to a proof check.",
                "evidence_type": evidence_type,
            },
            "supports": supports,
            "conflicts": conflicts,
            "quoted_text": _unique([claim.quote for claim in claims]),
            "reviewer_notes": "Admitted through read-before-bind provenance hooks.",
            "metadata": {
                **prepared.metadata,
                "classification": classification,
                "compiler_source_sha256": str(prepared.metadata["source_fingerprint"]),
                "claim_ids": [claim.id for claim in claims],
            },
        }
        evidence_items.append(item)
        cards.append(
            {
                "id": source_id,
                "title": prepared.record.title,
                "summary": item["summary"],
                "claim_ids": item["metadata"]["claim_ids"],
                "should_accept": should_accept,
            }
        )
    obligations = _unique([item.missing_fact for item in proof.obligations if item.missing_fact])
    has_blocking_obligations = any(item.blocking for item in proof.obligations)
    contradicted = sorted(item.requirement_id for item in proof.decisions if item.status == "CONTRADICTED")
    supported: list[dict[str, Any]] = []
    proof_conflicts: list[dict[str, Any]] = []
    for decision in proof.decisions:
        referenced_claims = _decision_claims(decision, node_results, claims_by_id)
        quoted_text = "\n".join(_unique(claim.quote for claim in referenced_claims))
        source_ids = _unique(claim.source_id for claim in referenced_claims)
        if decision.status == "SUPPORTED":
            supported.append(
                {
                    "requirement": decision.requirement_id,
                    "support_level": "full",
                    "quoted_text": quoted_text,
                }
            )
        elif decision.status == "CONTRADICTED":
            proof_conflicts.append(
                {
                    "type": "proof_contradiction",
                    "requirement": decision.requirement_id,
                    "severity": "high",
                    "description": decision.stop_reason,
                    "quoted_text": quoted_text,
                    "affected_evidence_ids": source_ids,
                }
            )
    accepted_items = [item for item in evidence_items if item["review_result"]["should_accept"]]
    accepted_credibility = [str(item["credibility"]) for item in accepted_items]
    overall_credibility = (
        "high"
        if accepted_credibility and all(value == "high" for value in accepted_credibility)
        else "low"
        if accepted_credibility and all(value == "low" for value in accepted_credibility)
        else "medium"
    )
    accepted_types = _unique(
        str(item.get("type") or "")
        for item in accepted_items
        if str(item.get("type") or "") != "unknown"
    )
    overall_evidence_type = accepted_types[0] if len(accepted_types) == 1 else "unknown"
    traceability = _review_traceability(accepted_items)
    return {
        "mode": "review",
        "compile_status": compile_status,
        "semantic_status": semantic_status,
        "source_doc_id": ",".join(item.record.source_id for item in prepared_sources),
        "evidence_type": overall_evidence_type,
        "credibility": overall_credibility,
        "extracted_fields": {},
        "extraction_result": {},
        "source_traceability": traceability,
        "support_level": "full" if supported and not has_blocking_obligations else "partial" if evidence_items else "none",
        "risk_flags": contradicted,
        "should_accept": bool(accepted_items),
        "reason": f"Compiled {len(proof.decisions)} requirement proof(s) from {len(sandbox.evidence_ir.claims)} grounded claim(s).",
        "supports": supported,
        "conflicts": proof_conflicts,
        "evidence_cards": cards,
        "suggested_patch": {
            "add_evidence": evidence_items,
            "risk_flags": contradicted,
            "next_questions": obligations,
            "evidence_cards": cards,
        },
        "reply_to_user": "Evidence review compiled into source-grounded proof checks.",
    }


def _decision_claims(
    decision: Any,
    node_results: Mapping[str, Any],
    claims_by_id: Mapping[str, Any],
) -> list[Any]:
    check_ids = (
        decision.supporting_check_ids
        if decision.status == "SUPPORTED"
        else decision.contradicting_check_ids
        if decision.status == "CONTRADICTED"
        else decision.unresolved_check_ids
    )
    claim_ids = _unique(
        claim_id
        for check_id in check_ids
        for claim_id in getattr(node_results.get(check_id), "claim_ids", [])
    )
    return [claims_by_id[claim_id] for claim_id in claim_ids if claim_id in claims_by_id]


def _review_traceability(accepted_items: Sequence[Mapping[str, Any]]) -> str:
    if not accepted_items:
        return "unclear"
    sources = {str(item.get("source") or "") for item in accepted_items}
    if sources == {"attachment"}:
        return "original_document"
    if sources == {"rag"}:
        return "rag_guidance"
    if sources == {"user_message"}:
        return "user_statement"
    return "unclear"


def _source_text(item: dict[str, Any]) -> str:
    lines = [
        f"SOURCE: {item.get('name') or item.get('attachment_id') or 'attachment'}",
        f"ATTACHMENT_ID: {item.get('attachment_id') or ''}",
    ]
    # Attachment dossiers keep a compact Markdown preview alongside the full
    # extracted text. The Worker must receive the full text when it is present.
    body = str(item.get("content") or item.get("body_markdown") or "")
    if body:
        lines.extend(["", "BODY:", body])
    for heading, key in (
        ("FIELDS", "field_inventory"),
        ("BLOCKS", "block_crops"),
        ("PAGES", "page_summaries"),
        ("VISUAL", "visual_check"),
        ("QUALITY", "quality_notes"),
        ("WARNINGS", "warnings"),
    ):
        value = item.get(key)
        if value not in (None, "", [], {}):
            lines.extend(["", f"{heading}:", json.dumps(value, ensure_ascii=False, indent=2, default=str)])
    return "\n".join(lines)


def _planning_source_catalog(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Give planning source shape without binding the plan to files or run-local IDs."""

    grouped: dict[str, dict[str, int | str]] = {}
    for item in items:
        kind = str(item.get("kind") or "unknown").strip() or "unknown"
        entry = grouped.setdefault(kind, {"kind": kind, "count": 0, "total_characters": 0})
        entry["count"] = int(entry["count"]) + 1
        entry["total_characters"] = int(entry["total_characters"]) + max(
            0, int(item.get("characters") or 0)
        )
    return [grouped[kind] for kind in sorted(grouped)]


def _task_compiler_repair_payload(
    payload: Mapping[str, Any],
    error: Exception,
    previous_draft: ProofPlan | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "instruction": "Return one corrected ProofPlan; preserve the supplied scope and objective.",
        "validation_error": str(error),
    }
    if previous_draft is not None:
        feedback["previous_draft"] = previous_draft.model_dump(mode="json")
    return {**payload, "repair_feedback": feedback}


def _planning_extraction_summary(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate extraction shape; case facts remain work for the sandboxed Executor."""

    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        kind = str(item.get("content_kind") or "unknown").strip() or "unknown"
        entry = grouped.setdefault(
            kind,
            {"content_kind": kind, "document_count": 0, "available_fields": set(), "warning_count": 0},
        )
        entry["document_count"] += 1
        entry["available_fields"].update(_unique(item.get("available_fields") or []))
        entry["warning_count"] += len(item.get("warnings") or [])
    return [
        {
            **entry,
            "available_fields": sorted(entry["available_fields"]),
        }
        for _kind, entry in sorted(grouped.items())
    ]


def _planning_source_documents(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expose complete extracted text without run-local source identity."""

    documents = []
    for index, item in enumerate(items, start=1):
        content = str(item.get("content") or item.get("source_content") or "").strip()
        if content:
            documents.append(
                {
                    "document_index": index,
                    "kind": str(
                        item.get("kind")
                        or item.get("evidence_type")
                        or item.get("type")
                        or "unknown"
                    ),
                    "content": content,
                }
            )
    return documents


def _active_proof_signatures(
    requirement_ids: Sequence[str],
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> list[dict[str, Any]]:
    return [
        signature.model_dump(mode="json")
        for requirement_id in requirement_ids
        if (signature := proof_signature_for(requirement_id, requirement_pack)) is not None
    ]


def _verifier_contracts(
    checks: Sequence[Mapping[str, Any]],
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> list[str]:
    contracts = [check.get("action_contract") or {} for check in checks]
    if any(
        contract.get("contract_kind") == "ERP_CHECK"
        and contract.get("execution_mode", "registered_resolver") == "registered_resolver"
        for contract in contracts
    ):
        return [
            (
                "For a registered_resolver ERP CHECK, independently inspect its submitted ResolverWitness and "
                "Runtime-selected source-bound inputs. Accept exactly one ResolverWitness and one terminal Binding. "
                "CHECK_SATISFIED maps to SUPPORTED; CHECK_VIOLATED maps to CONTRADICTED."
            )
        ]
    if any(
        contract and contract.get("contract_kind") != "ERP_CHECK"
        for contract in contracts
    ):
        return [
            (
                "For a registered action CHECK, execute and accept exactly the Witness steps in "
                "action_contract.predicate_program, then accept exactly one submitted Binding. "
                "Its term_refs must include every outcome predicate Witness. Derive the policy "
                "action with predicate_program.outcome, compare it with action_kind, and use the "
                "corresponding registered terminal relation/status. Do not use strong_status_links "
                "for this semantic lane."
            )
        ]
    if any(
        requirement_pack.kind(requirement_id) == "document"
        for check in checks
        for requirement_id in check.get("requirement_refs", [])
    ):
        return [
            "An explicitly stated parent document family and a more specific subtype are "
            "compatible unless the CHECK requires mutually exclusive subtypes; preserve both "
            "observations, and do not use the subtype alone to refute the parent business role."
        ]
    return []


def _unconfigured_policy_refs(
    plan: ProofPlan,
    policy_excerpt: Mapping[str, Any],
    requirement_pack: RequirementPack,
) -> list[str]:
    raw_values = policy_excerpt.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    refs = set(plan.policy_refs)
    for node in plan.nodes:
        if isinstance(node.action_contract, RegisteredActionContract):
            refs.update(
                requirement_pack.capability(node.action_contract.capability_id).get(
                    "optional_policy_values"
                )
                or []
            )
    return sorted(
        policy_ref
        for policy_ref in refs
        if not isinstance(values.get(policy_ref), Mapping)
        or values[policy_ref].get("configured") is not True
    )


def _tool_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _source_attribute(item: Mapping[str, Any], key: str) -> str:
    for container in (
        item,
        item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {},
        item.get("review_result") if isinstance(item.get("review_result"), Mapping) else {},
    ):
        value = container.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _true_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _optional_bool(value: Any) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _normalize_credibility(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else "medium"


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
