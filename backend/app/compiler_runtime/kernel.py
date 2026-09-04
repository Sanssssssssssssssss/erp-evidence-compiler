from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import TopologicalSorter
import hashlib
import json
from typing import Any, Iterable, Mapping

from app.compiler_runtime.graph_walk import reachable_ids
from app.compiler_runtime.models import (
    AssessmentStatus,
    BusinessGapCode,
    CheckAssessment,
    Claim,
    CompilationDiagnostic,
    CompiledProof,
    DecisionProof,
    ERPReviewContract,
    NodeResult,
    ProofNode,
    ProofObligation,
    RecordFieldLocator,
    RegisteredActionContract,
    RegisteredPredicateOperand,
    ReviewArtifact,
    explicit_final_statuses,
)
from app.compiler_runtime.policy import configured_policy_values, policy_hash
from app.compiler_runtime.proof_terms import (
    CalculationWitness,
    ProofTermRef,
    ResolverWitness,
    SemanticBindingProposal,
    replay_witness,
)
from app.compiler_runtime.requirement_pack import (
    DEFAULT_REQUIREMENT_PACK,
    RequirementPack,
)
from app.compiler_runtime.signatures import (
    proof_signature_for,
    proof_signature_hash_for,
    validate_plan_conformance,
)


@dataclass
class _AcceptedTerms:
    claim_ids: set[str] = field(default_factory=set)
    binding_ids: set[str] = field(default_factory=set)
    witness_ids: set[str] = field(default_factory=set)
    policy_refs: set[str] = field(default_factory=set)
    facet_terms: dict[str, set[str]] = field(default_factory=dict)

    def add_term(self, facet_ref: str, kind: str) -> None:
        self.facet_terms.setdefault(facet_ref, set()).add(kind)


@dataclass(frozen=True)
class _TermFailure:
    code: str
    message: str
    missing_fact: str


@dataclass(frozen=True)
class _WitnessOwnerIndex:
    """Submission lineage used only when a Witness depends on another CHECK."""

    check_nodes: Mapping[str, ProofNode]
    submitted_claim_refs: Mapping[str, set[str]]
    submitted_witness_refs: Mapping[str, set[str]]


def compile_review_artifact(
    artifact: ReviewArtifact,
    *,
    requirement_requiredness: Mapping[str, bool] | None = None,
    requirement_pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
    source_records: Mapping[str, Any] | None = None,
) -> CompiledProof:
    """Compile model work through a business-blind typed proof boundary."""
    plan = artifact.plan
    nodes = {node.id: node for node in plan.nodes}
    assessments = {item.check_id: item for item in artifact.assessments}
    claims = {claim.id: claim for claim in artifact.evidence_ir.claims}
    bindings = {item.id: item for item in artifact.binding_proposals}
    witnesses = {
        item.id: item
        for item in [*artifact.calculation_witnesses, *artifact.resolver_witnesses]
    }
    source_ids = set(artifact.evidence_ir.source_ids)
    witness_evidence_snapshot_hash = artifact.evidence_ir.source_snapshot_hash()
    submitted_claim_refs = {
        check_id: set(ref_ids) for check_id, ref_ids in artifact.submitted_claim_refs.items()
    }
    submitted_binding_refs = {
        check_id: set(ref_ids) for check_id, ref_ids in artifact.submitted_binding_refs.items()
    }
    submitted_witness_refs = {
        check_id: set(ref_ids) for check_id, ref_ids in artifact.submitted_witness_refs.items()
    }
    witness_owner_index = _WitnessOwnerIndex(
        check_nodes={
            node_id: node for node_id, node in nodes.items() if node.kind == "CHECK"
        },
        submitted_claim_refs=submitted_claim_refs,
        submitted_witness_refs=submitted_witness_refs,
    )
    unconfigured_policy_refs = set(artifact.unconfigured_policy_refs)
    diagnostics: list[CompilationDiagnostic] = []

    integrity_errors = _artifact_integrity_errors(artifact, diagnostics, requirement_pack)
    check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
    for check_id in sorted(set(assessments) - set(nodes)):
        diagnostics.append(
            CompilationDiagnostic(
                code="UNKNOWN_CHECK_ASSESSMENT",
                node_id=check_id,
                message="Verifier returned an assessment for a node outside this ProofPlan",
                blocking=False,
            )
        )
    for check_id in sorted(set(assessments) & (set(nodes) - check_ids)):
        diagnostics.append(
            CompilationDiagnostic(
                code="NON_CHECK_ASSESSMENT",
                node_id=check_id,
                message="Verifier assessments are accepted only for CHECK nodes",
                blocking=False,
            )
        )

    results: dict[str, NodeResult] = {}
    missing_facts: dict[str, str] = {}
    order = tuple(
        TopologicalSorter(
            {node.id: set(node.depends_on) for node in plan.nodes}
        ).static_order()
    )
    for node_id in order:
        node = nodes[node_id]
        if node.kind == "CHECK":
            result, diagnostic, missing_fact = _compile_check(
                node,
                assessments.get(node_id),
                claims=claims,
                bindings=bindings,
                witnesses=witnesses,
                source_fingerprints=artifact.evidence_ir.source_fingerprints,
                source_revisions=artifact.evidence_ir.source_revisions,
                source_records=source_records,
                source_ids=source_ids,
                check_submitted=node_id
                in (set(submitted_claim_refs) | set(submitted_binding_refs) | set(submitted_witness_refs)),
                submitted_claim_ids=submitted_claim_refs.get(node_id, set()),
                submitted_binding_ids=submitted_binding_refs.get(node_id, set()),
                submitted_witness_ids=submitted_witness_refs.get(node_id, set()),
                witness_owner_index=witness_owner_index,
                policy_values=artifact.resolved_policy_terms,
                evidence_snapshot_hash=witness_evidence_snapshot_hash,
                policy_snapshot_hash=artifact.policy_hash,
                unconfigured_policy_refs=unconfigured_policy_refs,
                integrity_errors=integrity_errors,
                requirement_pack=requirement_pack,
            )
            results[node_id] = result
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            if result.status == "NOT_FOUND":
                missing_facts[node_id] = missing_fact or node.statement
            continue

        dependency_results = [results[item] for item in node.depends_on]
        status = _aggregate(node.kind, [item.status for item in dependency_results])
        results[node_id] = NodeResult(
            node_id=node.id,
            kind=node.kind,
            status=status,
            reason=_aggregate_reason(node, dependency_results, status),
            claim_ids=_unique(item for result in dependency_results for item in result.claim_ids),
            binding_ids=_unique(
                item for result in dependency_results for item in result.binding_ids
            ),
            witness_ids=_unique(
                item for result in dependency_results for item in result.witness_ids
            ),
            source_ids=_unique(item for result in dependency_results for item in result.source_ids),
        )

    obligations: list[ProofObligation] = []
    decisions: list[DecisionProof] = []
    for requirement_id in plan.active_requirement_ids:
        required = (
            True
            if requirement_requiredness is None
            else bool(requirement_requiredness.get(requirement_id, True))
        )
        root_id = plan.roots[requirement_id]
        root_status = results[root_id].status
        reachable_checks = sorted(
            node_id
            for node_id in reachable_ids(root_id, lambda current: nodes[current].depends_on)
            if nodes[node_id].kind == "CHECK"
        )
        supporting = [
            node_id for node_id in reachable_checks if results[node_id].status == "SUPPORTED"
        ]
        contradicting = [
            node_id
            for node_id in reachable_checks
            if results[node_id].status == "CONTRADICTED"
        ]
        unresolved = [
            node_id for node_id in reachable_checks if results[node_id].status == "NOT_FOUND"
        ]
        obligation_ids: list[str] = []
        if root_status == "NOT_FOUND":
            for check_id in unresolved:
                obligation_id = f"obligation:{requirement_id}:{check_id}"
                obligation_ids.append(obligation_id)
                obligations.append(
                    ProofObligation(
                        id=obligation_id,
                        requirement_id=requirement_id,
                        check_id=check_id,
                        missing_fact=missing_facts.get(check_id, nodes[check_id].statement),
                        blocking=required,
                    )
                )
        decisions.append(
            DecisionProof(
                requirement_id=requirement_id,
                root_node_id=root_id,
                status=root_status,
                supporting_check_ids=supporting,
                contradicting_check_ids=contradicting,
                unresolved_check_ids=unresolved,
                obligation_ids=obligation_ids,
                plan_hash=artifact.plan_hash,
                requirement_pack_hash=artifact.requirement_pack_hash,
                source_snapshot_hash=artifact.source_snapshot_hash,
                evidence_snapshot_hash=artifact.evidence_snapshot_hash,
                proposal_hash=artifact.proposal_hash,
                policy_hash=artifact.policy_hash,
                stop_reason=_stop_reason(root_status, obligation_ids, blocking=required),
            )
        )

    blocking_nodes = {item.check_id for item in obligations if item.blocking}
    diagnostics = [
        item.model_copy(
            update={
                "blocking": item.blocking
                and (not item.node_id or item.node_id in blocking_nodes)
            }
        )
        for item in diagnostics
    ]
    return CompiledProof(
        node_results=[results[node.id] for node in plan.nodes],
        decisions=decisions,
        obligations=obligations,
        diagnostics=diagnostics,
    )


def _artifact_integrity_errors(
    artifact: ReviewArtifact,
    diagnostics: list[CompilationDiagnostic],
    requirement_pack: RequirementPack,
) -> list[str]:
    errors: list[str] = []

    def add(code: str, message: str) -> None:
        errors.append(code)
        diagnostics.append(CompilationDiagnostic(code=code, message=message))

    if not artifact.artifact_hash:
        add("MISSING_ARTIFACT_HASH", "ReviewArtifact has no finalized content hash")
    elif artifact.artifact_hash != artifact.content_hash():
        add("STALE_ARTIFACT", "ReviewArtifact content hash does not match its contents")
    if artifact.plan_hash != artifact.plan.content_hash():
        add("STALE_PLAN", "ReviewArtifact plan hash does not match its ProofPlan")
    pack_lineage = (
        artifact.requirement_pack_id,
        artifact.requirement_pack_version,
        artifact.requirement_pack_hash,
    )
    if any(pack_lineage) and pack_lineage != (
        requirement_pack.pack_id,
        requirement_pack.version,
        requirement_pack.content_hash,
    ):
        add(
            "STALE_REQUIREMENT_PACK",
            "ReviewArtifact RequirementPack lineage does not match the active registered pack",
        )
    current_signature_hash = proof_signature_hash_for(
        artifact.plan.active_requirement_ids,
        requirement_pack,
    )
    if not artifact.proof_signature_hash:
        add(
            "MISSING_PROOF_SIGNATURE_HASH",
            "ReviewArtifact has no active ProofSignature snapshot hash",
        )
    elif artifact.proof_signature_hash != current_signature_hash:
        add(
            "STALE_PROOF_SIGNATURE",
            "ReviewArtifact ProofSignature hash does not match the active type constraints",
        )
    if artifact.evidence_snapshot_hash != artifact.evidence_ir.content_hash():
        add(
            "STALE_EVIDENCE_SNAPSHOT",
            "ReviewArtifact evidence snapshot hash does not match its EvidenceIR",
        )
    if not artifact.source_snapshot_hash:
        add(
            "MISSING_SOURCE_SNAPSHOT_HASH",
            "ReviewArtifact has no admitted source snapshot hash",
        )
    elif artifact.source_snapshot_hash != artifact.evidence_ir.source_snapshot_hash():
        add(
            "STALE_SOURCE_SNAPSHOT",
            "ReviewArtifact source snapshot hash does not match its admitted source fingerprints",
        )
    source_ids = set(artifact.evidence_ir.source_ids)
    fingerprint_ids = set(artifact.evidence_ir.source_fingerprints)
    if source_ids and fingerprint_ids != source_ids:
        missing = sorted(source_ids - fingerprint_ids)
        extra = sorted(fingerprint_ids - source_ids)
        add(
            "INCOMPLETE_SOURCE_FINGERPRINTS",
            "ReviewArtifact source fingerprints do not cover its admitted sources: "
            f"missing={missing}, extra={extra}",
        )
    try:
        validate_plan_conformance(artifact.plan, requirement_pack.proof_signatures)
    except ValueError as exc:
        add("PLAN_SIGNATURE_MISMATCH", f"ProofPlan violates its ProofSignature: {exc}")
    check_nodes = [node for node in artifact.plan.nodes if node.kind == "CHECK"]
    action_contracts = [
        node.action_contract for node in check_nodes if node.action_contract is not None
    ]
    validated_action_contracts: list[RegisteredActionContract] = []
    validated_erp_contracts: list[ERPReviewContract] = []
    for contract in action_contracts:
        if isinstance(contract, ERPReviewContract):
            try:
                validated_erp_contracts.append(
                    ERPReviewContract.model_validate(contract.model_dump(mode="json"))
                )
            except (TypeError, ValueError) as exc:
                add(
                    "INVALID_ERP_REVIEW_CONTRACT",
                    f"ERP review contract failed boundary validation: {exc}",
                )
            continue
        if not isinstance(contract, RegisteredActionContract):
            add(
                "INVALID_REGISTERED_ACTION_CONTRACT",
                "ProofNode action_contract is not a validated RegisteredActionContract",
            )
            continue
        try:
            validated_action_contracts.append(
                RegisteredActionContract.model_validate(contract.model_dump(mode="json"))
            )
        except (TypeError, ValueError) as exc:
            add(
                "INVALID_REGISTERED_ACTION_CONTRACT",
                f"Registered action contract failed boundary validation: {exc}",
            )
    action_contracts_valid = (
        len(validated_action_contracts) + len(validated_erp_contracts)
        == len(action_contracts)
    )
    pack_execution_mode = requirement_pack.raw.get("execution_mode", "registered_resolver")
    if pack_execution_mode == "evidence_review" and len(validated_erp_contracts) != len(check_nodes):
        add(
            "ERP_REVIEW_CONTRACT_MISSING",
            "The evidence-review pack requires a sealed ERPReviewContract for every CHECK",
        )
    if any(contract.execution_mode != pack_execution_mode for contract in validated_erp_contracts):
        add(
            "ERP_REVIEW_MODE_MISMATCH",
            "The RequirementPack execution mode differs from its CHECK contracts",
        )
    optional_policy_refs: set[str] = set()
    try:
        capability_requirements = requirement_pack.capability_requirement_ids()
    except ValueError as exc:
        add("INVALID_REQUIREMENT_PACK", str(exc))
        capability_requirements = frozenset()
    requires_registered_actions = bool(
        set(artifact.plan.active_requirement_ids).intersection(capability_requirements)
    )
    if requires_registered_actions and len(action_contracts) != len(check_nodes):
        add(
            "REGISTERED_ACTION_CONTRACT_MISSING",
            "Every CHECK for a capability-backed Requirement must retain its registered action contract",
        )
    elif validated_action_contracts and action_contracts_valid:
        try:
            optional_policy_refs = {
                ref_id
                for contract in validated_action_contracts
                for ref_id in (
                    requirement_pack.capability(contract.capability_id).get(
                        "optional_policy_values"
                    )
                    or []
                )
            }
            canonical_policy_values = artifact.resolved_policy_terms
            if (
                optional_policy_refs
                and artifact.policy_snapshot
                and policy_hash(artifact.policy_snapshot) == artifact.policy_hash
            ):
                canonical_policy_values = configured_policy_values(
                    artifact.policy_snapshot
                )
            if artifact.plan != requirement_pack.lower_registered_action_plan(
                validated_action_contracts,
                configured_policy_refs=set(canonical_policy_values),
            ):
                raise ValueError("plan differs from canonical lowering")
        except ValueError as exc:
            add(
                "REGISTERED_PLAN_MISMATCH",
                f"Registered ProofPlan violates its RequirementPack contract: {exc}",
            )
    if validated_action_contracts and action_contracts_valid:
        lineage_errors = []
        for contract in validated_action_contracts:
            if contract.proposal_hash != artifact.proposal_hash:
                lineage_errors.append(f"{contract.contract_id}:proposal_hash")
            if contract.policy_hash != artifact.policy_hash:
                lineage_errors.append(f"{contract.contract_id}:policy_hash")
            if contract.requirement_pack_hash != requirement_pack.content_hash:
                lineage_errors.append(f"{contract.contract_id}:requirement_pack_hash")
            for source_id in contract.source_refs:
                if contract.source_fingerprints.get(source_id) != artifact.evidence_ir.source_fingerprints.get(
                    source_id
                ):
                    lineage_errors.append(f"{contract.contract_id}:{source_id}:fingerprint")
                if contract.source_revisions.get(source_id) != artifact.evidence_ir.source_revisions.get(
                    source_id
                ):
                    lineage_errors.append(f"{contract.contract_id}:{source_id}:revision")
        if lineage_errors:
            add(
                "REGISTERED_ACTION_LINEAGE_MISMATCH",
                f"Registered action lineage differs from the sealed artifact: {sorted(lineage_errors)}",
            )
    if validated_erp_contracts and action_contracts_valid:
        if len(validated_erp_contracts) != len(check_nodes):
            add(
                "ERP_REVIEW_CONTRACT_MISSING",
                "Every CHECK in a typed ERP ProofPlan must retain its ERPReviewContract",
            )
        logical_to_node = {
            contract.logical_check_id: contract.execution_check_instance_id
            for contract in validated_erp_contracts
        }
        node_by_id = {node.id: node for node in check_nodes}
        lineage_errors = []
        policy_source_hashes = {
            descriptor.fingerprint
            for descriptor in artifact.evidence_ir.source_descriptors.values()
            if descriptor.provenance.get("role") == "instruction"
        }
        for contract in validated_erp_contracts:
            node = node_by_id.get(contract.execution_check_instance_id)
            if node is None or node.action_contract != contract:
                lineage_errors.append(f"{contract.contract_id}:node")
                continue
            if contract.proposal_hash != artifact.proposal_hash:
                lineage_errors.append(f"{contract.contract_id}:proposal_hash")
            if contract.requirement_pack_hash != requirement_pack.content_hash:
                lineage_errors.append(f"{contract.contract_id}:requirement_pack_hash")
            source_fingerprints = {
                source_id: artifact.evidence_ir.source_fingerprints.get(source_id, "")
                for source_id in contract.source_refs
            }
            source_hash = hashlib.sha256(
                json.dumps(
                    source_fingerprints,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if source_hash != contract.source_snapshot_hash:
                lineage_errors.append(f"{contract.contract_id}:source_snapshot_hash")
            if contract.policy_source_hash not in policy_source_hashes:
                lineage_errors.append(f"{contract.contract_id}:policy_source_hash")
            expected_upstream = sorted(
                logical_to_node[item] for item in contract.upstream_logical_check_ids
            )
            if sorted(node.upstream_check_ids) != expected_upstream:
                lineage_errors.append(f"{contract.contract_id}:upstream")
        if lineage_errors:
            add(
                "ERP_REVIEW_LINEAGE_MISMATCH",
                f"Typed ERP review lineage differs from the sealed artifact: {sorted(lineage_errors)}",
            )

    plan_policy_refs = set(artifact.plan.policy_refs)
    resolved_refs = set(artifact.resolved_policy_terms)
    unconfigured_refs = set(artifact.unconfigured_policy_refs)
    if optional_policy_refs:
        if not artifact.policy_snapshot:
            add(
                "POLICY_SNAPSHOT_MISSING",
                "Registered optional policies require the canonical policy snapshot",
            )
        elif policy_hash(artifact.policy_snapshot) != artifact.policy_hash:
            add(
                "POLICY_SNAPSHOT_HASH_MISMATCH",
                "Canonical policy snapshot differs from policy_hash",
            )
        else:
            expected_values = {
                ref_id: value
                for ref_id, value in configured_policy_values(
                    artifact.policy_snapshot
                ).items()
                if ref_id in plan_policy_refs | optional_policy_refs
            }
            expected_unconfigured = (
                plan_policy_refs | optional_policy_refs
            ) - set(expected_values)
            if (
                artifact.resolved_policy_terms != expected_values
                or unconfigured_refs != expected_unconfigured
            ):
                add(
                    "POLICY_SNAPSHOT_STATE_MISMATCH",
                    "Resolved and unconfigured policies differ from the canonical policy snapshot",
                )
    unknown_refs = sorted(
        resolved_refs - plan_policy_refs
        | (unconfigured_refs - plan_policy_refs - optional_policy_refs)
    )
    if unknown_refs:
        add(
            "INVALID_POLICY_LINEAGE",
            f"Artifact policy state references policies outside its ProofPlan: {unknown_refs}",
        )
    overlap = sorted(resolved_refs & unconfigured_refs)
    if overlap:
        add(
            "INVALID_POLICY_LINEAGE",
            f"Artifact policies cannot be both resolved and unconfigured: {overlap}",
        )
    missing_refs = sorted(plan_policy_refs - resolved_refs - unconfigured_refs)
    if missing_refs:
        add(
            "INCOMPLETE_POLICY_LINEAGE",
            f"Artifact has no resolved or unconfigured state for policies: {missing_refs}",
        )
    missing_optional_state = sorted(
        optional_policy_refs - resolved_refs - unconfigured_refs
    )
    if missing_optional_state:
        add(
            "INCOMPLETE_POLICY_LINEAGE",
            f"Artifact has no state for optional capability policies: {missing_optional_state}",
        )
    return errors


def _compile_check(
    node: ProofNode,
    assessment: CheckAssessment | None,
    *,
    claims: dict[str, Claim],
    bindings: dict[str, SemanticBindingProposal],
    witnesses: dict[str, CalculationWitness | ResolverWitness],
    source_fingerprints: Mapping[str, str],
    source_revisions: Mapping[str, str],
    source_records: Mapping[str, Any] | None,
    source_ids: set[str],
    check_submitted: bool,
    submitted_claim_ids: set[str],
    submitted_binding_ids: set[str],
    submitted_witness_ids: set[str],
    witness_owner_index: _WitnessOwnerIndex,
    policy_values: Mapping[str, object],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    unconfigured_policy_refs: set[str],
    integrity_errors: list[str],
    requirement_pack: RequirementPack,
) -> tuple[NodeResult, CompilationDiagnostic | None, str]:
    if integrity_errors:
        return (
            NodeResult(
                node_id=node.id,
                kind=node.kind,
                status="NOT_FOUND",
                reason="artifact integrity check failed",
            ),
            None,
            "refresh the stale plan, evidence, policy, or artifact snapshot",
        )
    blocked_policy_refs = sorted(set(node.policy_refs) & unconfigured_policy_refs)
    if blocked_policy_refs:
        partial_claim_ids = sorted(set(claims) & submitted_claim_ids)
        return _rejected_check(
            node,
            code="POLICY_NOT_CONFIGURED",
            message=f"Required policy values are not configured: {blocked_policy_refs}",
            missing_fact=f"configure policy values: {', '.join(blocked_policy_refs)}",
            claim_ids=partial_claim_ids,
            source_ids=_unique(claims[item].source_id for item in partial_claim_ids),
            gap_code="POLICY_UNCONFIGURED",
        )
    if not check_submitted:
        return _rejected_check(
            node,
            code="MISSING_CHECK_SUBMISSION",
            message="Executor did not submit this CHECK before verification",
            missing_fact="complete and submit this CHECK from the evidence sandbox",
        )
    if assessment is None:
        return _rejected_check(
            node,
            code="MISSING_ASSESSMENT",
            message="Verifier did not return an assessment for this CHECK",
            missing_fact=node.statement,
        )

    evidence_review = (
        isinstance(node.action_contract, ERPReviewContract)
        and node.action_contract.execution_mode == "evidence_review"
    )
    if isinstance(node.action_contract, ERPReviewContract) and not evidence_review:
        return _compile_erp_check(
            node,
            assessment,
            bindings=bindings,
            witnesses=witnesses,
            submitted_claim_ids=submitted_claim_ids,
            submitted_binding_ids=submitted_binding_ids,
            submitted_witness_ids=submitted_witness_ids,
            source_fingerprints=source_fingerprints,
            source_revisions=source_revisions,
            source_records=source_records,
            evidence_snapshot_hash=evidence_snapshot_hash,
        )

    accepted, failure = _accepted_term_closure(
        node,
        assessment,
        claims=claims,
        bindings=bindings,
        witnesses=witnesses,
        submitted_claim_ids=submitted_claim_ids,
        submitted_binding_ids=submitted_binding_ids,
        submitted_witness_ids=submitted_witness_ids,
        witness_owner_index=witness_owner_index,
        policy_values=policy_values,
        evidence_snapshot_hash=evidence_snapshot_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        unconfigured_policy_refs=unconfigured_policy_refs,
    )
    if failure is not None:
        return _rejected_check(
            node,
            code=failure.code,
            message=failure.message,
            missing_fact=failure.missing_fact,
        )

    canonical_claim_ids = sorted(accepted.claim_ids)
    canonical_binding_ids = sorted(accepted.binding_ids)
    canonical_witness_ids = sorted(accepted.witness_ids)
    canonical_source_ids = sorted({claims[item].source_id for item in canonical_claim_ids})
    expected_check_source_ids = (
        set(node.action_contract.source_refs)
        if node.action_contract is not None
        else source_ids
    )

    if node.action_contract is not None and (
        not set(canonical_source_ids).issubset(expected_check_source_ids)
        or (not evidence_review and set(canonical_source_ids) != expected_check_source_ids)
    ):
        return _rejected_check(
            node,
            code="REGISTERED_CHECK_SOURCE_SCOPE_MISMATCH",
            message=(
                "Registered action proof terms violate the source scope in their action "
                f"contract; expected={sorted(expected_check_source_ids)}, "
                f"actual={canonical_source_ids}"
            ),
            missing_fact="use the source scope registered for this action contract",
        )

    if evidence_review:
        source_failure = _evidence_review_source_failure(
            node,
            claims=[claims[item] for item in canonical_claim_ids],
            source_records=source_records,
            source_fingerprints=source_fingerprints,
            source_revisions=source_revisions,
        )
        if source_failure is not None:
            return _rejected_check(
                node,
                code=source_failure.code,
                message=source_failure.message,
                missing_fact=source_failure.missing_fact,
            )

    unknown_sources = sorted(set(assessment.source_ids) - source_ids)
    if unknown_sources:
        return _rejected_check(
            node,
            code="INVALID_SOURCE_REFERENCE",
            message=f"Assessment cites unknown sources: {unknown_sources}",
            missing_fact="cite a source admitted to the EvidenceIR",
        )
    uncited_claim_sources = sorted(set(canonical_source_ids) - set(assessment.source_ids))
    if uncited_claim_sources:
        return _rejected_check(
            node,
            code="CLAIM_SOURCE_NOT_CITED",
            message=f"Assessment omits sources used by its proof terms: {uncited_claim_sources}",
            missing_fact="cite every source used by the assessment proof terms",
        )
    explicit_statuses = explicit_final_statuses(assessment.reason)
    if explicit_statuses and explicit_statuses != {assessment.status}:
        return _rejected_check(
            node,
            code="ASSESSMENT_STATUS_REASON_CONFLICT",
            message=(
                "Verifier structured status conflicts with its explicit final classification: "
                f"status={assessment.status}, reason_statuses={sorted(explicit_statuses)}"
            ),
            missing_fact="return one internally consistent final classification for this CHECK",
            claim_ids=canonical_claim_ids,
            binding_ids=canonical_binding_ids,
            witness_ids=canonical_witness_ids,
            source_ids=canonical_source_ids,
        )
    if assessment.status == "NOT_FOUND":
        diagnostic = None
        if not assessment.missing_fact:
            diagnostic = CompilationDiagnostic(
                code="MISSING_FACT_UNSPECIFIED",
                node_id=node.id,
                message="NOT_FOUND assessment did not describe the missing fact",
            )
        return (
            NodeResult(
                node_id=node.id,
                kind=node.kind,
                status="NOT_FOUND",
                reason=assessment.reason or "verifier could not establish the check",
                claim_ids=canonical_claim_ids,
                binding_ids=canonical_binding_ids,
                witness_ids=canonical_witness_ids,
                source_ids=canonical_source_ids,
                gap_code=assessment.gap_code,
            ),
            diagnostic,
            assessment.missing_fact or node.statement,
        )

    extra_sources = sorted(set(assessment.source_ids) - set(canonical_source_ids))
    if extra_sources:
        return _rejected_check(
            node,
            code="UNSUPPORTED_SOURCE_REFERENCE",
            message=f"Strong assessment cites sources not used by its proof terms: {extra_sources}",
            missing_fact="cite only the concrete sources used by the strong assessment proof terms",
        )
    examined_source_ids = set(assessment.examined_source_ids)
    if examined_source_ids != expected_check_source_ids:
        missing_sources = sorted(expected_check_source_ids - examined_source_ids)
        extra_examined_sources = sorted(examined_source_ids - expected_check_source_ids)
        return _rejected_check(
            node,
            code="SOURCE_COVERAGE_INCOMPLETE",
            message=(
                "Strong assessment did not examine exactly the CHECK source snapshot; "
                f"missing={missing_sources}, extra={extra_examined_sources}"
            ),
            missing_fact="examine every source admitted to this CHECK before a strong conclusion",
        )
    low_confidence_claims = sorted(
        claim_id for claim_id in canonical_claim_ids if claims[claim_id].confidence == "low"
    )
    if low_confidence_claims:
        return _rejected_check(
            node,
            code="LOW_CONFIDENCE_CLAIM",
            message=f"Strong assessment cites low-confidence Claims: {low_confidence_claims}",
            missing_fact="replace low-confidence Claims with sufficiently reliable grounded evidence",
        )
    if not canonical_claim_ids:
        return _rejected_check(
            node,
            code="MISSING_CLAIM_REFERENCE",
            message="A strong assessment must close over at least one admitted Claim",
            missing_fact="cite an admitted Claim that grounds the assessment proof terms",
        )
    if not canonical_source_ids:
        return _rejected_check(
            node,
            code="MISSING_SOURCE_REFERENCE",
            message="A strong assessment must close over at least one concrete source",
            missing_fact="cite the concrete source for the assessment",
        )

    missing_consumed_policy_refs = sorted(
        set(node.policy_refs) - accepted.policy_refs
    )
    if missing_consumed_policy_refs:
        return _rejected_check(
            node,
            code="REQUIRED_POLICY_TERM_NOT_CONSUMED",
            message=(
                "Strong assessment proof closure does not consume required Policy terms: "
                f"{missing_consumed_policy_refs}"
            ),
            missing_fact=(
                "consume required configured Policy terms through an accepted Binding "
                f"or Witness: {', '.join(missing_consumed_policy_refs)}"
            ),
            claim_ids=canonical_claim_ids,
            binding_ids=canonical_binding_ids,
            witness_ids=canonical_witness_ids,
            source_ids=canonical_source_ids,
        )

    typed_failure = _typed_closure_failure(
        node,
        assessment.status,
        accepted.facet_terms,
        requirement_pack,
    )
    if typed_failure is not None:
        return _rejected_check(
            node,
            code=typed_failure.code,
            message=typed_failure.message,
            missing_fact=typed_failure.missing_fact,
            claim_ids=canonical_claim_ids,
            binding_ids=canonical_binding_ids,
            witness_ids=canonical_witness_ids,
            source_ids=canonical_source_ids,
        )
    terminal_failure = _strong_status_terminal_failure(
        node,
        assessment,
        facet_terms=accepted.facet_terms,
        claims=claims,
        bindings=bindings,
        witnesses=witnesses,
        submitted_binding_ids=submitted_binding_ids,
        submitted_witness_ids=submitted_witness_ids,
        accepted_witness_ids=accepted.witness_ids,
        source_records=source_records,
        requirement_pack=requirement_pack,
    )
    if terminal_failure is not None:
        return _rejected_check(
            node,
            code=terminal_failure.code,
            message=terminal_failure.message,
            missing_fact=terminal_failure.missing_fact,
            claim_ids=canonical_claim_ids,
            binding_ids=canonical_binding_ids,
            witness_ids=canonical_witness_ids,
            source_ids=canonical_source_ids,
        )
    return (
        NodeResult(
            node_id=node.id,
            kind=node.kind,
            status=assessment.status,
            reason=assessment.reason,
            claim_ids=canonical_claim_ids,
            binding_ids=canonical_binding_ids,
            witness_ids=canonical_witness_ids,
            source_ids=canonical_source_ids,
            gap_code=assessment.gap_code,
        ),
        None,
        "",
    )


def _accepted_term_closure(
    node: ProofNode,
    assessment: CheckAssessment,
    *,
    claims: dict[str, Claim],
    bindings: dict[str, SemanticBindingProposal],
    witnesses: dict[str, CalculationWitness],
    submitted_claim_ids: set[str],
    submitted_binding_ids: set[str],
    submitted_witness_ids: set[str],
    witness_owner_index: _WitnessOwnerIndex,
    policy_values: Mapping[str, object],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    unconfigured_policy_refs: set[str],
) -> tuple[_AcceptedTerms, _TermFailure | None]:
    accepted = _AcceptedTerms()
    unknown_claims = sorted(set(assessment.claim_ids) - set(claims))
    if unknown_claims:
        return accepted, _TermFailure(
            "INVALID_CLAIM_REFERENCE",
            f"Assessment cites unknown Claims: {unknown_claims}",
            "bind the cited facts as admitted Claims",
        )
    unsubmitted_claims = sorted(set(assessment.claim_ids) - submitted_claim_ids)
    if unsubmitted_claims:
        return accepted, _TermFailure(
            "UNSUBMITTED_CLAIM_REFERENCE",
            f"Assessment cites Claims not submitted for this CHECK: {unsubmitted_claims}",
            "submit the candidate Claims for this CHECK before verification",
        )
    accepted.claim_ids.update(assessment.claim_ids)
    for facet_ref in node.facet_refs:
        if assessment.claim_ids:
            accepted.add_term(facet_ref, "CLAIM")

    for binding_id in assessment.accepted_binding_ids:
        binding = bindings.get(binding_id)
        if binding is None:
            return accepted, _unknown_term("BINDING", binding_id)
        if binding_id not in submitted_binding_ids:
            return accepted, _TermFailure(
                "UNSUBMITTED_BINDING_REFERENCE",
                f"Verifier accepted Binding {binding_id!r} that was not submitted for this CHECK",
                "submit the Binding for this CHECK before verification",
            )
        failure = _validate_binding(
            binding,
            node=node,
            accepted=accepted,
            claims=claims,
            witnesses=witnesses,
            submitted_claim_ids=submitted_claim_ids,
            submitted_witness_ids=submitted_witness_ids,
            witness_owner_index=witness_owner_index,
            policy_values=policy_values,
            evidence_snapshot_hash=evidence_snapshot_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            unconfigured_policy_refs=unconfigured_policy_refs,
            visiting_witnesses=set(),
        )
        if failure is not None:
            return accepted, failure
        accepted.binding_ids.add(binding.id)
        accepted.add_term(binding.facet_ref, "BINDING")

    for witness_id in assessment.accepted_witness_ids:
        if witness_id not in witnesses:
            return accepted, _unknown_term("WITNESS", witness_id)
        if witness_id not in submitted_witness_ids:
            return accepted, _TermFailure(
                "UNSUBMITTED_WITNESS_REFERENCE",
                f"Verifier accepted Witness {witness_id!r} that was not submitted for this CHECK",
                "submit the Witness for this CHECK before verification",
            )
        witness = witnesses[witness_id]
        if (
            isinstance(node.action_contract, ERPReviewContract)
            and node.action_contract.execution_mode == "evidence_review"
            and witness.check_id != node.id
        ):
            failure = _validate_term_ref(
                ProofTermRef(kind="WITNESS", ref_id=witness_id),
                facet_ref=node.facet_refs[0], node=node, accepted=accepted,
                claims=claims, witnesses=witnesses, submitted_claim_ids=submitted_claim_ids,
                submitted_witness_ids=submitted_witness_ids, witness_owner_index=witness_owner_index,
                policy_values=policy_values, evidence_snapshot_hash=evidence_snapshot_hash,
                policy_snapshot_hash=policy_snapshot_hash, unconfigured_policy_refs=unconfigured_policy_refs,
                visiting_witnesses=set(), credit_claim=False, allow_cross_check_witness=True,
            )
            if failure is not None:
                return accepted, failure
            continue
        failure = _validate_witness(
            witness,
            node=node,
            accepted=accepted,
            claims=claims,
            witnesses=witnesses,
            submitted_claim_ids=submitted_claim_ids,
            submitted_witness_ids=submitted_witness_ids,
            witness_owner_index=witness_owner_index,
            policy_values=policy_values,
            evidence_snapshot_hash=evidence_snapshot_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            unconfigured_policy_refs=unconfigured_policy_refs,
            visiting=set(),
            credit_facet=True,
        )
        if failure is not None:
            return accepted, failure
    return accepted, None


def _validate_binding(
    binding: SemanticBindingProposal,
    *,
    node: ProofNode,
    accepted: _AcceptedTerms,
    claims: dict[str, Claim],
    witnesses: dict[str, CalculationWitness],
    submitted_claim_ids: set[str],
    submitted_witness_ids: set[str],
    witness_owner_index: _WitnessOwnerIndex,
    policy_values: Mapping[str, object],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    unconfigured_policy_refs: set[str],
    visiting_witnesses: set[str],
) -> _TermFailure | None:
    scope_failure = _term_scope_failure(
        kind="Binding",
        term_id=binding.id,
        check_id=binding.check_id,
        facet_ref=binding.facet_ref,
        node=node,
    )
    if scope_failure is not None:
        return scope_failure
    for ref in binding.term_refs:
        failure = _validate_term_ref(
            ref,
            facet_ref=binding.facet_ref,
            node=node,
            accepted=accepted,
            claims=claims,
            witnesses=witnesses,
            submitted_claim_ids=submitted_claim_ids,
            submitted_witness_ids=submitted_witness_ids,
            witness_owner_index=witness_owner_index,
            policy_values=policy_values,
            evidence_snapshot_hash=evidence_snapshot_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            unconfigured_policy_refs=unconfigured_policy_refs,
            visiting_witnesses=visiting_witnesses,
            credit_claim=True,
            allow_cross_check_witness=(
                isinstance(node.action_contract, ERPReviewContract)
                and node.action_contract.execution_mode == "evidence_review"
            ),
        )
        if failure is not None:
            return failure
    return None


def _validate_witness(
    witness: CalculationWitness,
    *,
    node: ProofNode,
    accepted: _AcceptedTerms,
    claims: dict[str, Claim],
    witnesses: dict[str, CalculationWitness],
    submitted_claim_ids: set[str],
    submitted_witness_ids: set[str],
    witness_owner_index: _WitnessOwnerIndex,
    policy_values: Mapping[str, object],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    unconfigured_policy_refs: set[str],
    visiting: set[str],
    credit_facet: bool,
) -> _TermFailure | None:
    if not isinstance(witness, CalculationWitness):
        return _TermFailure(
            "INVALID_CALCULATION_WITNESS",
            "A source-driven calculation cannot consume a registered resolver receipt",
            "compute a CalculationWitness from grounded source facts",
        )
    scope_failure = _term_scope_failure(
        kind="Witness",
        term_id=witness.id,
        check_id=witness.check_id,
        facet_ref=witness.facet_ref,
        node=node,
    )
    if scope_failure is not None:
        return scope_failure
    if witness.id in accepted.witness_ids:
        if credit_facet:
            accepted.add_term(witness.facet_ref, "WITNESS")
        return None
    if witness.id in visiting:
        return _TermFailure(
            "INVALID_WITNESS_LINEAGE",
            f"Witness lineage is cyclic at {witness.id!r}",
            "recompute the Witness from an acyclic set of proof terms",
        )
    if witness.id not in submitted_witness_ids:
        return _TermFailure(
            "UNSUBMITTED_WITNESS_REFERENCE",
            f"Witness {witness.id!r} was not submitted by its owner CHECK {node.id!r}",
            "submit the Witness under its owner CHECK before using it in a proof lineage",
        )
    if witness.evidence_snapshot_hash != evidence_snapshot_hash:
        return _TermFailure(
            "STALE_WITNESS_EVIDENCE",
            f"Witness {witness.id!r} belongs to a different evidence snapshot",
            "recompute the Witness from the current evidence snapshot",
        )
    if witness.policy_snapshot_hash != policy_snapshot_hash:
        return _TermFailure(
            "STALE_WITNESS_POLICY",
            f"Witness {witness.id!r} belongs to a different policy snapshot",
            "recompute the Witness from the current policy snapshot",
        )

    visiting.add(witness.id)
    try:
        for operand in witness.operands:
            failure = _validate_term_ref(
                operand.ref,
                facet_ref=witness.facet_ref,
                node=node,
                accepted=accepted,
                claims=claims,
                witnesses=witnesses,
                submitted_claim_ids=submitted_claim_ids,
                submitted_witness_ids=submitted_witness_ids,
                witness_owner_index=witness_owner_index,
                policy_values=policy_values,
                evidence_snapshot_hash=evidence_snapshot_hash,
                policy_snapshot_hash=policy_snapshot_hash,
                unconfigured_policy_refs=unconfigured_policy_refs,
                visiting_witnesses=visiting,
                credit_claim=credit_facet,
                allow_cross_check_witness=True,
            )
            if failure is not None:
                return failure
    finally:
        visiting.remove(witness.id)

    if not replay_witness(
        witness,
        claims=claims,
        witnesses=witnesses,
        policy_values=policy_values,
    ):
        return _TermFailure(
            "INVALID_WITNESS_REPLAY",
            f"Witness {witness.id!r} failed deterministic replay",
            "recompute the Witness using current submitted Claims and Policy terms",
        )
    accepted.witness_ids.add(witness.id)
    if credit_facet:
        accepted.add_term(witness.facet_ref, "WITNESS")
    return None


def _validate_term_ref(
    ref: ProofTermRef,
    *,
    facet_ref: str,
    node: ProofNode,
    accepted: _AcceptedTerms,
    claims: dict[str, Claim],
    witnesses: dict[str, CalculationWitness],
    submitted_claim_ids: set[str],
    submitted_witness_ids: set[str],
    witness_owner_index: _WitnessOwnerIndex,
    policy_values: Mapping[str, object],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    unconfigured_policy_refs: set[str],
    visiting_witnesses: set[str],
    credit_claim: bool,
    allow_cross_check_witness: bool,
) -> _TermFailure | None:
    if ref.kind == "CLAIM":
        if ref.ref_id not in claims:
            return _unknown_term("CLAIM", ref.ref_id)
        if ref.ref_id not in submitted_claim_ids:
            return _TermFailure(
                "UNSUBMITTED_CLAIM_REFERENCE",
                f"Proof term cites Claim {ref.ref_id!r} not submitted for this CHECK",
                "submit every Claim used by the accepted proof terms",
            )
        accepted.claim_ids.add(ref.ref_id)
        if credit_claim:
            accepted.add_term(facet_ref, "CLAIM")
        return None
    if ref.kind == "WITNESS":
        parent = witnesses.get(ref.ref_id)
        if parent is None:
            return _unknown_term("WITNESS", ref.ref_id)
        if allow_cross_check_witness:
            if (
                parent.check_id != node.id
                and isinstance(node.action_contract, ERPReviewContract)
                and node.action_contract.execution_mode == "evidence_review"
                and parent.check_id not in reachable_ids(
                    node.id,
                    lambda check_id: witness_owner_index.check_nodes[check_id].upstream_check_ids,
                )
            ):
                return _TermFailure(
                    "UNDECLARED_WITNESS_DEPENDENCY",
                    f"Witness {parent.id!r} is not owned by a declared upstream CHECK",
                    "use only the current CHECK or its declared dependency results",
                )
            owner_node = witness_owner_index.check_nodes.get(parent.check_id)
            if owner_node is None:
                return _TermFailure(
                    "INVALID_WITNESS_OWNER",
                    f"Witness {parent.id!r} names unknown owner CHECK {parent.check_id!r}",
                    "use a Witness owned and submitted by a CHECK in this ProofPlan",
                )
            owner_claim_ids = set(
                witness_owner_index.submitted_claim_refs.get(owner_node.id, set())
            )
            owner_witness_ids = set(
                witness_owner_index.submitted_witness_refs.get(owner_node.id, set())
            )
        else:
            owner_node = node
            owner_claim_ids = submitted_claim_ids
            owner_witness_ids = submitted_witness_ids
        return _validate_witness(
            parent,
            node=owner_node,
            accepted=accepted,
            claims=claims,
            witnesses=witnesses,
            submitted_claim_ids=owner_claim_ids,
            submitted_witness_ids=owner_witness_ids,
            witness_owner_index=witness_owner_index,
            policy_values=policy_values,
            evidence_snapshot_hash=evidence_snapshot_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            unconfigured_policy_refs=unconfigured_policy_refs,
            visiting=visiting_witnesses,
            credit_facet=not allow_cross_check_witness,
        )
    if ref.ref_id in unconfigured_policy_refs or ref.ref_id not in policy_values:
        return _TermFailure(
            "POLICY_TERM_NOT_CONFIGURED",
            f"Proof term cites unavailable Policy {ref.ref_id!r}",
            f"configure policy value: {ref.ref_id}",
        )
    if ref.ref_id not in node.policy_refs:
        return _TermFailure(
            "POLICY_TERM_OUT_OF_SCOPE",
            f"Proof term cites Policy {ref.ref_id!r} outside CHECK {node.id!r}",
            "use only configured Policy refs declared on this CHECK",
        )
    accepted.policy_refs.add(ref.ref_id)
    return None


def _term_scope_failure(
    *,
    kind: str,
    term_id: str,
    check_id: str,
    facet_ref: str,
    node: ProofNode,
) -> _TermFailure | None:
    if check_id != node.id:
        return _TermFailure(
            f"{kind.upper()}_CHECK_MISMATCH",
            f"{kind} {term_id!r} belongs to CHECK {check_id!r}, not {node.id!r}",
            f"create the {kind} under the current CHECK",
        )
    if facet_ref not in node.facet_refs:
        return _TermFailure(
            f"{kind.upper()}_FACET_MISMATCH",
            f"{kind} {term_id!r} uses facet {facet_ref!r} outside CHECK {node.id!r}",
            f"create the {kind} under a facet declared on the current CHECK",
        )
    return None


def _unknown_term(kind: str, term_id: str) -> _TermFailure:
    return _TermFailure(
        f"INVALID_{kind}_REFERENCE",
        f"Verifier proof closure cites unknown {kind.title()} {term_id!r}",
        f"create and submit the cited {kind.title()} before verification",
    )


def _typed_closure_failure(
    node: ProofNode,
    status: AssessmentStatus,
    facet_terms: Mapping[str, set[str]],
    requirement_pack: RequirementPack,
) -> _TermFailure | None:
    required = _required_facet_terms(node, requirement_pack)
    if not required:
        return None

    missing = {
        facet_ref: sorted(kinds - set(facet_terms.get(facet_ref, set())))
        for facet_ref, kinds in required.items()
    }
    closed = {facet_ref for facet_ref, kinds in missing.items() if not kinds}
    valid = len(closed) == len(required) if status == "SUPPORTED" else bool(closed)
    if valid:
        return None
    detail = ", ".join(
        f"{facet_ref}={kinds or ['closed']}" for facet_ref, kinds in sorted(missing.items())
    )
    expectation = (
        "every declared required facet"
        if status == "SUPPORTED"
        else "at least one declared required facet"
    )
    return _TermFailure(
        "TYPED_PROOF_INCOMPLETE",
        f"{status} requires typed closure for {expectation}; missing terms: {detail}",
        f"supply the minimum typed proof terms for {expectation}",
    )


def _erp_source_snapshot_failure(
    contract: ERPReviewContract,
    *,
    source_records: Mapping[str, Any] | None,
    source_fingerprints: Mapping[str, str],
    source_revisions: Mapping[str, str],
) -> _TermFailure | None:
    if source_records is None or not set(contract.source_refs) <= set(source_records):
        return _TermFailure(
            "ERP_SOURCE_REBINDING_REQUIRED",
            "Kernel requires the frozen SourceRecords to validate ERP proof sources",
            "restore the frozen sources bound to this CHECK contract",
        )
    for source_id in contract.source_refs:
        record = source_records[source_id]
        if (
            getattr(record, "source_id", None) != source_id
            or getattr(record, "record_revision", "") != source_revisions.get(source_id, "")
        ):
            return _TermFailure(
                "ERP_SOURCE_REBINDING_MISMATCH",
                "Frozen SourceRecord identity differs from the admitted source snapshot",
                "restore the original source identity and revision",
            )
        if hashlib.sha256(record.content.encode("utf-8")).hexdigest() != source_fingerprints.get(source_id, ""):
            return _TermFailure(
                "ERP_SOURCE_FINGERPRINT_MISMATCH",
                "Frozen SourceRecord content differs from the admitted source fingerprint",
                "start a new revision from the changed source snapshot",
            )
    return None


def _evidence_review_source_failure(
    node: ProofNode,
    *,
    claims: Iterable[Claim],
    source_records: Mapping[str, Any] | None,
    source_fingerprints: Mapping[str, str],
    source_revisions: Mapping[str, str],
) -> _TermFailure | None:
    """Recheck quoted bytes and fields, never their model-proposed meaning."""
    from app.compiler_runtime.sandbox import EvidenceSandbox

    contract = node.action_contract
    assert isinstance(contract, ERPReviewContract)
    failure = _erp_source_snapshot_failure(
        contract,
        source_records=source_records,
        source_fingerprints=source_fingerprints,
        source_revisions=source_revisions,
    )
    if failure is not None:
        return failure
    assert source_records is not None
    for claim in claims:
        source = source_records[claim.source_id]
        if isinstance(claim.locator, RecordFieldLocator):
            locator = claim.locator
            try:
                valid = (
                    source.record_fields is not None
                    and locator.record_ref == claim.source_id == claim.subject
                    and locator.record_revision == source.record_revision
                    and json.dumps(
                        EvidenceSandbox._resolve_json_pointer(source.record_fields, locator.field_path),
                        ensure_ascii=False, sort_keys=True, allow_nan=False,
                    ) == json.dumps(claim.value, ensure_ascii=False, sort_keys=True, allow_nan=False)
                )
            except (KeyError, IndexError, TypeError, ValueError):
                valid = False
        else:
            valid = (
                source.kind != "record"
                and bool(claim.quote)
                and (claim.quote in source.content or claim.quote in source.provenance_text)
                and (
                    not EvidenceSandbox._has_numeric_intent(claim.value)
                    or EvidenceSandbox._numeric_value_matches_quote(claim.value, claim.quote)
                )
            )
        if not valid:
            return _TermFailure(
                "ERP_CLAIM_NOT_OBSERVED",
                f"Claim {claim.id!r} does not reproduce a fact in its frozen source",
                "bind the exact original quote or revision-bound record field",
            )
    return None


def _evidence_review_terminal_failure(
    node: ProofNode,
    assessment: CheckAssessment,
    *,
    bindings: Mapping[str, SemanticBindingProposal],
    witnesses: Mapping[str, CalculationWitness],
    submitted_witness_ids: set[str],
    accepted_witness_ids: set[str],
    claims: Mapping[str, Claim],
    source_records: Mapping[str, Any] | None,
) -> _TermFailure | None:
    """Validate a verifier-owned semantic decision and replayed calculation links.

    This gate checks integrity, not whether natural-language evidence entails a
    business relationship. That judgment belongs to the independent Verifier.
    """
    contract = node.action_contract
    assert isinstance(contract, ERPReviewContract)
    if len(assessment.accepted_binding_ids) != 1:
        return _TermFailure(
            "ERP_TERMINAL_BINDING_REQUIRED",
            "Evidence review requires exactly one accepted terminal Binding",
            "submit the source-grounded relationship for this CHECK",
        )
    binding = bindings[assessment.accepted_binding_ids[0]]
    if contract.terminal_relations.get(binding.relation) != assessment.status:
        return _TermFailure(
            "ERP_VERIFIER_STATUS_MISMATCH",
            "The accepted terminal relationship differs from the Verifier classification",
            "independently review the relationship and return its registered classification",
        )
    binding_claim_ids: set[str] = set()
    pending = list(binding.term_refs)
    visited: set[str] = set()
    while pending:
        ref = pending.pop()
        if ref.kind == "CLAIM":
            binding_claim_ids.add(ref.ref_id)
        elif ref.kind == "WITNESS" and ref.ref_id not in visited:
            visited.add(ref.ref_id)
            pending.extend(operand.ref for operand in witnesses[ref.ref_id].operands)
    if not binding_claim_ids:
        return _TermFailure(
            "ERP_TERMINAL_SOURCE_REQUIRED",
            "The terminal relationship does not consume any grounded source fact",
            "reference the relevant Claims directly or through replayed calculation lineage",
        )
    if any(group.source == "LIVE_ODOO" for group in contract.resolver_program.evidence):
        native_sources = {
            source_id
            for source_id, source in (source_records or {}).items()
            if source.kind == "record" and source.record_revision
            and isinstance(source.provenance.get("source_records"), dict)
            and source.provenance["source_records"]
            and all(
                isinstance(item, dict) and item.get("revision") and item.get("fingerprint")
                for item in source.provenance["source_records"].values()
            )
        }
        if not any(claims[item].source_id in native_sources for item in binding_claim_ids):
            return _TermFailure(
                "NATIVE_SOURCE_REQUIRED",
                "The planned native-state check has no source-bound native record in its terminal proof",
                "admit the required native records; policy prose and proposed state are not native state",
            )
    if not submitted_witness_ids <= accepted_witness_ids:
        return _TermFailure(
            "ERP_CALCULATION_NOT_CONSUMED",
            "A submitted calculation is absent from the accepted proof closure",
            "review every submitted calculation or replace the candidate before verification",
        )
    current: dict[str, CalculationWitness] = {}
    for witness_id in submitted_witness_ids:
        witness = witnesses.get(witness_id)
        if not isinstance(witness, CalculationWitness):
            return _TermFailure(
                "INVALID_CALCULATION_WITNESS",
                "Evidence review accepts only CalculationWitnesses owned by this CHECK",
                "use the existing calculation tool over grounded source facts",
            )
        if witness.check_id != node.id:
            # The accepted closure already checked its declared owner and latest submission.
            continue
        current[witness_id] = witness
    consumed = {
        operand.ref.ref_id
        for witness in current.values()
        for operand in witness.operands
        if operand.ref.kind == "WITNESS"
    }
    terminal_ids = set(current) - consumed
    bound_witness_ids = {ref.ref_id for ref in binding.term_refs if ref.kind == "WITNESS"}
    if not terminal_ids <= set(assessment.accepted_witness_ids) & bound_witness_ids:
        return _TermFailure(
            "ERP_CALCULATION_NOT_CONSUMED",
            "A submitted calculation result is omitted from the terminal relationship or independent review",
            "include every terminal calculation result in the Binding and Verifier acceptance",
        )
    if contract.requires_calculation and not visited:
        return _TermFailure(
            "TERMINAL_WITNESS_REQUIRED",
            "This CHECK requires a source-grounded calculation consumed by its terminal relationship",
            "perform the calculation required by the plan and reference its result",
        )
    # Polarity belongs to the reviewed proposition: false eligibility can justify
    # cancellation. Mixed semantic/numeric checks need not be decided by arithmetic.
    for link in assessment.strong_status_links:
        witness = witnesses.get(link.witness_id)
        if (
            not isinstance(witness, CalculationWitness)
            or not isinstance(witness.result, bool)
            or link.witness_id not in visited & submitted_witness_ids & accepted_witness_ids
        ):
            return _TermFailure(
                "INVALID_TERMINAL_WITNESS_REFERENCE",
                "A decisive status link must cite an accepted, submitted boolean in the terminal proof",
                "link only a reviewed boolean calculation actually consumed by this CHECK",
            )
        outcome = link.true_status if witness.result else (
            "CONTRADICTED" if link.true_status == "SUPPORTED" else "SUPPORTED"
        )
        if outcome != assessment.status:
            return _TermFailure(
                "TERMINAL_WITNESS_STATUS_MISMATCH",
                "The replayed decisive comparison and its reviewed polarity disagree with the classification",
                "review the comparison's meaning for this proposed action",
            )
    return None


def _compile_erp_check(
    node: ProofNode,
    assessment: CheckAssessment,
    *,
    bindings: Mapping[str, SemanticBindingProposal],
    witnesses: Mapping[str, CalculationWitness | ResolverWitness],
    submitted_claim_ids: set[str],
    submitted_binding_ids: set[str],
    submitted_witness_ids: set[str],
    source_fingerprints: Mapping[str, str],
    source_revisions: Mapping[str, str],
    source_records: Mapping[str, Any] | None,
    evidence_snapshot_hash: str,
) -> tuple[NodeResult, CompilationDiagnostic | None, str]:
    """Replay the small typed ERP terminal contract without Claim expansion."""

    contract = node.action_contract
    if not isinstance(contract, ERPReviewContract):
        raise AssertionError("ERP compiler path requires ERPReviewContract")

    def reject(code: str, message: str) -> tuple[NodeResult, CompilationDiagnostic, str]:
        return _rejected_check(
            node,
            code=code,
            message=message,
            missing_fact="submit one exact replayable ERP resolver receipt and terminal Binding",
        )

    if submitted_claim_ids or assessment.claim_ids:
        return reject("ERP_CLAIM_NOT_ALLOWED", "Typed ERP CHECKs do not accept model-authored Claims")
    if len(submitted_witness_ids) != 1 or set(assessment.accepted_witness_ids) != submitted_witness_ids:
        return reject(
            "ERP_RESOLVER_WITNESS_REQUIRED",
            "Exactly one submitted ResolverWitness must be accepted for this CHECK",
        )
    witness = witnesses.get(next(iter(submitted_witness_ids)))
    if not isinstance(witness, ResolverWitness) or witness.check_id != node.id:
        return reject("ERP_RESOLVER_WITNESS_INVALID", "Submitted Witness is not owned by this ERP CHECK")
    if len(submitted_binding_ids) != 1 or set(assessment.accepted_binding_ids) != submitted_binding_ids:
        return reject(
            "ERP_TERMINAL_BINDING_REQUIRED",
            "Exactly one submitted terminal Binding must be accepted for this CHECK",
        )
    binding = bindings.get(next(iter(submitted_binding_ids)))
    if binding is None or binding.check_id != node.id:
        return reject("ERP_TERMINAL_BINDING_INVALID", "Terminal Binding is absent or owned by another CHECK")
    if binding.facet_ref not in node.facet_refs:
        return reject("ERP_TERMINAL_BINDING_INVALID", "Terminal Binding uses an undeclared facet")
    if binding.term_refs != [ProofTermRef(kind="WITNESS", ref_id=witness.id)]:
        return reject("ERP_TERMINAL_BINDING_INVALID", "Terminal Binding must reference only its ResolverWitness")

    expected_sources = set(contract.source_refs)
    if set(assessment.source_ids) != expected_sources or set(assessment.examined_source_ids) != expected_sources:
        return reject("SOURCE_COVERAGE_INCOMPLETE", "Verifier did not examine exactly the registered source closure")
    expected_source_fingerprints = {
        source_id: source_fingerprints[source_id] for source_id in contract.source_refs
    }
    source_binding_hash = hashlib.sha256(
        json.dumps(
            expected_source_fingerprints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        witness.contract_hash != contract.immutable_contract_hash
        or witness.resolver_id != contract.resolver_program.resolver_id
        or witness.resolver_version != contract.resolver_program.resolver_version
        or witness.source_refs != contract.source_refs
        or witness.source_fingerprints != expected_source_fingerprints
        or witness.source_binding_hash != source_binding_hash
        or witness.source_binding_hash != contract.source_snapshot_hash
        or witness.evidence_snapshot_hash != evidence_snapshot_hash
        or witness.proposal_hash != contract.proposal_hash
        or witness.policy_source_hash != contract.policy_source_hash
    ):
        return reject("ERP_RESOLVER_LINEAGE_MISMATCH", "ResolverWitness lineage differs from the sealed CHECK contract")

    source_failure = _erp_source_snapshot_failure(
        contract,
        source_records=source_records,
        source_fingerprints=source_fingerprints,
        source_revisions=source_revisions,
    )
    if source_failure is not None:
        return reject(source_failure.code, source_failure.message)
    assert source_records is not None
    bound_sources = {source_id: source_records[source_id] for source_id in contract.source_refs}
    try:
        from erp_agent_odoo.capabilities.erp_resolvers import resolve_erp_check

        resolved_inputs, replayed, diagnostics = resolve_erp_check(
            contract,
            bound_sources,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return reject("ERP_RESOLVER_REPLAY_FAILED", f"Resolver replay failed: {exc}")
    if resolved_inputs != witness.resolved_inputs:
        return reject(
            "ERP_RESOLVER_INPUT_MISMATCH",
            "ResolverWitness inputs differ from deterministic source selection",
        )
    if replayed != witness.result or diagnostics != witness.diagnostics:
        return reject("ERP_RESOLVER_REPLAY_MISMATCH", "ResolverWitness result does not replay exactly")

    relation = "CHECK_SATISFIED" if replayed else "CHECK_VIOLATED"
    status: AssessmentStatus = "SUPPORTED" if replayed else "CONTRADICTED"
    if binding.relation != relation or contract.terminal_relations.get(relation) != status:
        return reject("ERP_TERMINAL_BINDING_MISMATCH", "Terminal Binding polarity differs from resolver replay")
    if assessment.status != status or assessment.strong_status_links:
        return reject("ERP_VERIFIER_STATUS_MISMATCH", "Verifier classification differs from resolver replay")
    explicit = explicit_final_statuses(assessment.reason)
    if explicit and explicit != {status}:
        return reject("ASSESSMENT_STATUS_REASON_CONFLICT", "Verifier reason conflicts with its structured status")

    return (
        NodeResult(
            node_id=node.id,
            kind=node.kind,
            status=status,
            reason=assessment.reason,
            binding_ids=[binding.id],
            witness_ids=[witness.id],
            source_ids=sorted(expected_sources),
        ),
        None,
        "",
    )


def _required_facet_terms(
    node: ProofNode,
    requirement_pack: RequirementPack,
) -> dict[str, set[str]]:
    required: dict[str, set[str]] = {}
    for requirement_id in node.requirement_refs:
        signature = proof_signature_for(requirement_id, requirement_pack)
        if signature is None:
            continue
        for facet in signature.facets:
            if facet.id in node.facet_refs:
                path = facet.path_for_roles(node.semantic_role_refs)
                terms = (
                    path.minimum_proof_terms
                    if path is not None
                    else [term for candidate in facet.proof_paths for term in candidate.minimum_proof_terms]
                )
                required.setdefault(facet.id, set()).update(terms)
    return required


def _strong_status_terminal_failure(
    node: ProofNode,
    assessment: CheckAssessment,
    *,
    facet_terms: Mapping[str, set[str]],
    claims: Mapping[str, Claim],
    bindings: Mapping[str, SemanticBindingProposal],
    witnesses: Mapping[str, CalculationWitness],
    submitted_binding_ids: set[str],
    submitted_witness_ids: set[str],
    accepted_witness_ids: set[str],
    source_records: Mapping[str, Any] | None,
    requirement_pack: RequirementPack,
) -> _TermFailure | None:
    """Bind a strong arithmetic status to replayed boolean proof, fail closed.

    ProofSignature remains only a minimum proof-kind declaration.  This gate is
    generic: any declared facet whose minimum kinds include WITNESS needs an
    explicit verifier polarity link when it contributes to a strong status.
    """

    if (
        isinstance(node.action_contract, ERPReviewContract)
        and node.action_contract.execution_mode == "evidence_review"
    ):
        return _evidence_review_terminal_failure(
            node,
            assessment,
            bindings=bindings,
            witnesses=witnesses,
            submitted_witness_ids=submitted_witness_ids,
            accepted_witness_ids=accepted_witness_ids,
            claims=claims,
            source_records=source_records,
        )
    if node.action_contract is not None:
        return _registered_action_terminal_failure(
            node,
            assessment,
            claims=claims,
            bindings=bindings,
            witnesses=witnesses,
            submitted_binding_ids=submitted_binding_ids,
        )

    required = _required_facet_terms(node, requirement_pack)
    witness_facets = {
        facet_ref for facet_ref, kinds in required.items() if "WITNESS" in kinds
    }
    if not witness_facets:
        return None

    closed_facets = {
        facet_ref
        for facet_ref, kinds in required.items()
        if kinds.issubset(set(facet_terms.get(facet_ref, set())))
    }
    direct_accepted = set(assessment.accepted_witness_ids)
    outcomes: dict[str, list[AssessmentStatus]] = {
        facet_ref: [] for facet_ref in witness_facets
    }
    for link in assessment.strong_status_links:
        witness = witnesses.get(link.witness_id)
        if witness is None:
            return _TermFailure(
                "INVALID_TERMINAL_WITNESS_REFERENCE",
                f"Strong status link cites unknown Witness {link.witness_id!r}",
                "create, submit, and accept a replayable boolean terminal Witness",
            )
        if link.witness_id not in direct_accepted:
            return _TermFailure(
                "TERMINAL_WITNESS_NOT_DIRECTLY_ACCEPTED",
                f"Terminal Witness {link.witness_id!r} was not directly accepted for CHECK {node.id!r}",
                "list the terminal Witness in this CHECK's accepted_witness_ids",
            )
        if link.witness_id not in submitted_witness_ids:
            return _TermFailure(
                "UNSUBMITTED_TERMINAL_WITNESS",
                f"Terminal Witness {link.witness_id!r} was not submitted for CHECK {node.id!r}",
                "submit the terminal Witness under the CHECK that owns it",
            )
        if witness.check_id != node.id:
            return _TermFailure(
                "TERMINAL_WITNESS_CHECK_MISMATCH",
                f"Terminal Witness {witness.id!r} belongs to CHECK {witness.check_id!r}, not {node.id!r}",
                "compute and submit the terminal Witness under the current CHECK",
            )
        if witness.facet_ref not in witness_facets:
            return _TermFailure(
                "TERMINAL_WITNESS_FACET_MISMATCH",
                f"Terminal Witness {witness.id!r} does not close a WITNESS facet on CHECK {node.id!r}",
                "link a boolean Witness under a declared facet that requires WITNESS proof",
            )
        if not isinstance(witness.result, bool):
            return _TermFailure(
                "TERMINAL_WITNESS_NOT_BOOLEAN",
                f"Terminal Witness {witness.id!r} has a Decimal result, not a boolean result",
                "compute a replayable boolean terminal Witness from the accepted numeric lineage",
            )
        terminal_policy_refs = _facet_policy_refs(
            node,
            witness.facet_ref,
            requirement_pack,
        )
        if terminal_policy_refs:
            consumed_policy_refs = _witness_lineage_policy_refs(
                witness,
                witnesses=witnesses,
                visiting=set(),
            )
            if not terminal_policy_refs.intersection(consumed_policy_refs):
                return _TermFailure(
                    "TERMINAL_WITNESS_POLICY_MISSING",
                    f"Terminal Witness {witness.id!r} does not consume a Policy term declared on CHECK {node.id!r}",
                    "compute the terminal predicate from a configured typed Policy operand",
                )
        false_status: AssessmentStatus = (
            "CONTRADICTED" if link.true_status == "SUPPORTED" else "SUPPORTED"
        )
        outcomes[witness.facet_ref].append(
            link.true_status if witness.result else false_status
        )

    if assessment.status == "SUPPORTED":
        missing = sorted(
            facet_ref
            for facet_ref in witness_facets
            if "SUPPORTED" not in outcomes[facet_ref]
        )
        contrary = sorted(
            facet_ref
            for facet_ref, statuses in outcomes.items()
            if "CONTRADICTED" in statuses
        )
        if missing or contrary:
            if contrary:
                return _TermFailure(
                    "TERMINAL_WITNESS_STATUS_MISMATCH",
                    "Replayed terminal Witness polarity conflicts with the Verifier's SUPPORTED status; "
                    f"missing={missing}, contradicted={contrary}",
                    "re-evaluate the terminal Witness true_status mapping for this CHECK",
                )
            return _TermFailure(
                "TERMINAL_WITNESS_REQUIRED",
                "SUPPORTED requires one replayable boolean terminal outcome for every WITNESS facet; "
                f"missing={missing}, contradicted={contrary}",
                "compute, submit, accept, and link a boolean terminal Witness for every WITNESS facet",
            )
        return None

    contradicted_facets = {
        facet_ref
        for facet_ref, statuses in outcomes.items()
        if "CONTRADICTED" in statuses and facet_ref in closed_facets
    }
    if not contradicted_facets:
        if any(outcomes.values()):
            return _TermFailure(
                "TERMINAL_WITNESS_STATUS_MISMATCH",
                "Replayed terminal Witness polarity does not establish the Verifier's CONTRADICTED status",
                "re-evaluate the terminal Witness true_status mapping for this CHECK",
            )
        return _TermFailure(
            "TERMINAL_WITNESS_REQUIRED",
            "CONTRADICTED requires a replayable boolean terminal outcome on at least one closed WITNESS facet",
            "compute, submit, accept, and link a boolean terminal Witness for the contradicted facet",
        )
    return None


def _registered_action_terminal_failure(
    node: ProofNode,
    assessment: CheckAssessment,
    *,
    claims: Mapping[str, Claim],
    bindings: Mapping[str, SemanticBindingProposal],
    witnesses: Mapping[str, CalculationWitness],
    submitted_binding_ids: set[str],
) -> _TermFailure | None:
    contract = node.action_contract
    if contract is None:
        raise AssertionError("registered action terminal requires an action contract")
    if len(assessment.accepted_binding_ids) != 1:
        return _TermFailure(
            "TERMINAL_BINDING_REQUIRED",
            "Registered action CHECK requires exactly one accepted terminal Binding",
            "submit and accept one registered action-to-policy terminal relation",
        )
    binding = bindings[assessment.accepted_binding_ids[0]]
    if binding.id not in submitted_binding_ids:
        return _TermFailure(
            "UNSUBMITTED_TERMINAL_BINDING",
            f"Terminal Binding {binding.id!r} was not submitted for CHECK {node.id!r}",
            "submit the terminal Binding under the current CHECK",
        )
    if binding.relation not in contract.terminal_relations:
        return _TermFailure(
            "UNREGISTERED_TERMINAL_RELATION",
            f"Terminal relation {binding.relation!r} is not registered for this action capability",
            "use the terminal relation registered by the active RequirementPack",
        )
    bound_steps: dict[str, CalculationWitness] = {}
    remaining_witness_ids = set(assessment.accepted_witness_ids)
    for step in contract.predicate_program.steps:
        matches = [
            witnesses[witness_id]
            for witness_id in remaining_witness_ids
            if witnesses[witness_id].operation == step.operation
            and len(witnesses[witness_id].operands) == len(step.operands)
            and all(
                _registered_operand_matches(
                    expected,
                    actual.ref,
                    target_record_ref=contract.target_record_ref,
                    target_revision=contract.source_revisions[contract.target_record_ref],
                    claims=claims,
                    bound_steps=bound_steps,
                )
                for expected, actual in zip(
                    step.operands,
                    witnesses[witness_id].operands,
                    strict=True,
                )
            )
        ]
        if len(matches) != 1:
            return _TermFailure(
                "REGISTERED_PREDICATE_WITNESS_MISMATCH",
                f"Predicate step {step.step_id!r} requires exactly one accepted matching Witness",
                "submit and accept the exact Witness program registered by the active RequirementPack",
            )
        bound_steps[step.step_id] = matches[0]
        remaining_witness_ids.remove(matches[0].id)
    if remaining_witness_ids:
        return _TermFailure(
            "UNREGISTERED_ACCEPTED_WITNESS",
            f"Registered action assessment accepted Witnesses outside its predicate program: {sorted(remaining_witness_ids)}",
            "accept only the exact Witness program registered for this action capability",
        )

    outcome = contract.predicate_program.outcome
    terminal_witness_ids = {bound_steps[step_id].id for step_id in outcome.predicate_refs}
    referenced_witnesses = {
        ref.ref_id for ref in binding.term_refs if ref.kind == "WITNESS"
    }
    if not terminal_witness_ids.issubset(referenced_witnesses):
        return _TermFailure(
            "TERMINAL_BINDING_WITNESS_CLOSURE_INCOMPLETE",
            "Terminal Binding must reference every registered outcome predicate Witness",
            "bind the registered terminal relation to the complete accepted predicate outcome",
        )
    predicates_hold = all(
        bound_steps[step_id].result is True for step_id in outcome.predicate_refs
    )
    expected_action = outcome.true_action if predicates_hold else outcome.false_action
    expected_status: AssessmentStatus = (
        "SUPPORTED" if contract.action_kind == expected_action else "CONTRADICTED"
    )
    expected_relation = next(
        relation
        for relation, status in contract.terminal_relations.items()
        if status == expected_status
    )
    if binding.relation != expected_relation or assessment.status != expected_status:
        return _TermFailure(
            "TERMINAL_BINDING_STATUS_MISMATCH",
            "Registered predicate outcomes, proposed action, terminal relation, and assessment status disagree",
            "derive the terminal relation and classification from the complete registered predicate program",
        )
    return None


def _registered_operand_matches(
    expected: RegisteredPredicateOperand,
    actual: ProofTermRef,
    *,
    target_record_ref: str,
    target_revision: str,
    claims: Mapping[str, Claim],
    bound_steps: Mapping[str, CalculationWitness],
) -> bool:
    if expected.kind == "POLICY":
        return actual.kind == "POLICY" and actual.ref_id == expected.ref_id
    if expected.kind == "STEP":
        witness = bound_steps.get(expected.ref_id)
        return actual.kind == "WITNESS" and witness is not None and actual.ref_id == witness.id
    claim = claims.get(actual.ref_id) if actual.kind == "CLAIM" else None
    return bool(
        claim is not None
        and isinstance(claim.locator, RecordFieldLocator)
        and claim.source_id == target_record_ref
        and claim.subject == target_record_ref
        and claim.locator.record_ref == target_record_ref
        and claim.locator.record_revision == target_revision
        and claim.locator.field_path == expected.ref_id
    )


def _facet_policy_refs(
    node: ProofNode,
    facet_ref: str,
    requirement_pack: RequirementPack,
) -> set[str]:
    refs: set[str] = set()
    for requirement_id in node.requirement_refs:
        signature = proof_signature_for(requirement_id, requirement_pack)
        if signature is None:
            continue
        facet = next((item for item in signature.facets if item.id == facet_ref), None)
        if facet is None:
            continue
        refs.update(
            signature.required_policy_refs
            if facet.required_policy_refs is None
            else facet.required_policy_refs
        )
    return refs


def _witness_lineage_policy_refs(
    witness: CalculationWitness,
    *,
    witnesses: Mapping[str, CalculationWitness],
    visiting: set[str],
) -> set[str]:
    if witness.id in visiting:
        return set()
    visiting.add(witness.id)
    try:
        result = {
            operand.ref.ref_id
            for operand in witness.operands
            if operand.ref.kind == "POLICY"
        }
        for operand in witness.operands:
            if operand.ref.kind != "WITNESS":
                continue
            parent = witnesses.get(operand.ref.ref_id)
            if parent is not None:
                result.update(
                    _witness_lineage_policy_refs(
                        parent,
                        witnesses=witnesses,
                        visiting=visiting,
                    )
                )
        return result
    finally:
        visiting.remove(witness.id)


def _rejected_check(
    node: ProofNode,
    *,
    code: str,
    message: str,
    missing_fact: str,
    claim_ids: Iterable[str] = (),
    binding_ids: Iterable[str] = (),
    witness_ids: Iterable[str] = (),
    source_ids: Iterable[str] = (),
    gap_code: BusinessGapCode | None = None,
) -> tuple[NodeResult, CompilationDiagnostic, str]:
    return (
        NodeResult(
            node_id=node.id,
            kind=node.kind,
            status="NOT_FOUND",
            reason=message,
            claim_ids=_unique(claim_ids),
            binding_ids=_unique(binding_ids),
            witness_ids=_unique(witness_ids),
            source_ids=_unique(source_ids),
            gap_code=gap_code,
        ),
        CompilationDiagnostic(code=code, node_id=node.id, message=message),
        missing_fact,
    )


def _aggregate(kind: str, statuses: list[AssessmentStatus]) -> AssessmentStatus:
    if kind == "ALL":
        if "CONTRADICTED" in statuses:
            return "CONTRADICTED"
        if all(item == "SUPPORTED" for item in statuses):
            return "SUPPORTED"
        return "NOT_FOUND"
    if kind == "ANY":
        if "SUPPORTED" in statuses:
            return "SUPPORTED"
        if all(item == "CONTRADICTED" for item in statuses):
            return "CONTRADICTED"
        return "NOT_FOUND"
    raise ValueError(f"Unsupported aggregate kind: {kind}")


def _aggregate_reason(
    node: ProofNode,
    dependencies: list[NodeResult],
    status: AssessmentStatus,
) -> str:
    child_states = ", ".join(f"{item.node_id}={item.status}" for item in dependencies)
    return f"{node.kind} evaluated to {status} from {child_states}"


def _unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _stop_reason(
    status: AssessmentStatus,
    obligation_ids: list[str],
    *,
    blocking: bool = True,
) -> str:
    if status == "SUPPORTED":
        return "the requirement proof is supported"
    if status == "CONTRADICTED":
        return "the requirement proof is contradicted"
    if obligation_ids:
        return (
            "blocking proof obligations remain unresolved"
            if blocking
            else "optional evidence remains unresolved"
        )
    return "the requirement could not be established"
