"""Offline negative controls for the explicitly separate evidence-review mode."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import (
    CheckAssessment, Claim, ERPReviewContract, EvidenceIR, EvidenceSourceDescriptor,
    ProofNode, ProofPlan, RecordFieldLocator, RegisteredResolverProgram, ReviewArtifact, StrongStatusLink,
)
from app.compiler_runtime.policy import policy_hash
from app.compiler_runtime.proof_terms import (
    CalculationRequest, ProofTermRef, SemanticBindingProposal, compute_witness,
)
from app.compiler_runtime.requirement_pack import RequirementPack, ODOO_ERP_ACTION_PLAN_PACK
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord


ROOT = Path(__file__).resolve().parents[3]
PACK = RequirementPack.from_path(ROOT / "policies/evidence_action_review_v1.json")


def _fixture(*, status="SUPPORTED", requires_calculation=False, values=(), mode="evidence_review", record_kind="", live=False,
             action_kind="confirm", policy_text="Maximum amount: 100.", numeric_decision=None):
    pack = PACK if mode == "evidence_review" else ODOO_ERP_ACTION_PLAN_PACK
    sources = {
        "policy": SourceRecord(
            source_id="policy", kind="document", content=policy_text,
            provenance={"role": "instruction"},
        ),
        "request": SourceRecord(
            source_id="request", kind="document",
            content="Request R9 approved by finance. Amount: 90. Alternative amount: 110.",
        ),
        "cover": SourceRecord(source_id="cover", kind="document", content="Case cover sheet."),
    }
    if record_kind:
        provenance = {}
        if record_kind == "native":
            provenance = {"source_records": {"native:request": {"revision": "native:r1", "fingerprint": "a" * 64}}}
        sources["request"] = SourceRecord(
            source_id="request", kind="record", content="", record_model="request",
            record_revision="r1", structured_fields={"approved": True}, provenance=provenance,
        )
    fingerprints = {
        key: hashlib.sha256(item.content.encode()).hexdigest() for key, item in sources.items()
    }
    source_hash = hashlib.sha256(json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    claims = [Claim(
        id="approval", subject="R9", predicate="approval_statement", value="approved by finance",
        source_id="request", quote="Request R9 approved by finance.", locator="line 1",
    )]
    if record_kind:
        claims = [Claim(
            id="approval", subject="request", predicate="approved", value=True,
            source_id="request", locator=RecordFieldLocator(
                record_ref="request", field_path="/approved", record_revision="r1",
            ),
        )]
    if values:
        claims.append(Claim(
            id="limit", subject="policy", predicate="amount_limit", value="100",
            source_id="policy", quote="Maximum amount: 100.", locator="line 1",
        ))
        for index, value in enumerate(values):
            claims.append(Claim(
                id=f"amount{index}", subject="R9", predicate="amount", value=str(value),
                source_id="request", quote=f"Amount: {value}." if value == 90 else f"Alternative amount: {value}.",
                locator="line 1",
            ))
    ir = EvidenceIR(
        schema_version="2" if record_kind else "1",
        source_ids=list(sources), source_fingerprints=fingerprints,
        source_revisions={key: value.record_revision for key, value in sources.items() if value.record_revision},
        source_descriptors={key: EvidenceSourceDescriptor(
            source_type="record" if value.kind == "record" else "document", fingerprint=fingerprints[key],
            revision=value.record_revision, record_model=value.record_model, provenance=dict(value.provenance),
        ) for key, value in sources.items()}, claims=claims,
    )
    contract = ERPReviewContract(
        logical_check_id="check:request", compiler_revision=1, template_id="request_review",
        local_check_id="request_permitted", owner_action_id="release", check_kind="shared",
        action_kind=action_kind, proposal_hash="proposal:r1", source_snapshot_hash=source_hash,
        policy_source_hash=fingerprints["policy"], requirement_pack_hash=pack.content_hash,
        source_refs=list(sources), execution_mode=mode, requires_calculation=requires_calculation,
        **({"numeric_decision": numeric_decision} if numeric_decision is not None else {}),
        resolver_program=RegisteredResolverProgram(resolver_id="semantic_evidence", evidence=[{
            "group_id": "request", "source": "BOUND_SOURCE", "method": "MODEL_QUOTE",
            "source_role": "request", "facts": ["approval", "amount"],
        }] + ([{"group_id": "native", "source": "LIVE_ODOO", "method": "RUNTIME_TOOL", "facts": ["approved"]}] if live else [])),
    )
    node = ProofNode(
        id=contract.execution_check_instance_id, kind="CHECK", statement=f"The proposed {action_kind} is appropriate under the source policy.",
        requirement_refs=["erp_action_plan_valid"], facet_refs=["complete_action_plan"], action_contract=contract,
    )
    plan = ProofPlan(
        plan_id="plan:r1", objective="Review the proposed request.", active_requirement_ids=["erp_action_plan_valid"],
        roots={"erp_action_plan_valid": node.id}, nodes=[node],
    )
    witnesses = []
    for index, _value in enumerate(values):
        witnesses.append(compute_witness(
            CalculationRequest(id=f"w{index}", check_id=node.id, facet_ref="complete_action_plan",
                               operation="LTE", operands=[ProofTermRef(kind="CLAIM", ref_id=f"amount{index}"),
                                                          ProofTermRef(kind="CLAIM", ref_id="limit")]),
            claims={item.id: item for item in claims}, witnesses={}, policy_values={},
            evidence_snapshot_hash=ir.source_snapshot_hash(), policy_snapshot_hash=policy_hash(pack.policy),
        ))
    binding = SemanticBindingProposal(
        id="terminal", check_id=node.id, facet_ref="complete_action_plan",
        relation="CHECK_VIOLATED" if status == "CONTRADICTED" else "CHECK_SATISFIED",
        term_refs=[ProofTermRef(kind="CLAIM", ref_id=item.id) for item in claims]
                  + [ProofTermRef(kind="WITNESS", ref_id=item.id) for item in witnesses],
        reason="Source-grounded candidate relationship, subject to independent review.",
    )
    assessment = CheckAssessment(
        check_id=node.id, claim_ids=[item.id for item in claims],
        accepted_binding_ids=[] if status == "NOT_FOUND" else [binding.id],
        accepted_witness_ids=[item.id for item in witnesses],
        source_ids=sorted({item.source_id for item in claims}), examined_source_ids=list(sources),
        reason=f"Independent source review. Final classification: {status}", status=status,
        missing_fact="A required authorization scope is absent." if status == "NOT_FOUND" else "",
    )
    artifact = ReviewArtifact(
        plan=plan, plan_hash=plan.content_hash(), requirement_pack_id=pack.pack_id,
        requirement_pack_version=pack.version, requirement_pack_hash=pack.content_hash,
        proof_signature_hash=pack.signature_hash_for(plan.active_requirement_ids), evidence_ir=ir,
        source_snapshot_hash=ir.source_snapshot_hash(), evidence_snapshot_hash=ir.content_hash(),
        proposal_hash=contract.proposal_hash, assessments=[assessment], binding_proposals=[binding],
        calculation_witnesses=witnesses, submitted_claim_refs={node.id: [item.id for item in claims]},
        submitted_binding_refs={node.id: [binding.id]}, submitted_witness_refs={node.id: [item.id for item in witnesses]},
        policy_hash=policy_hash(pack.policy), policy_snapshot=pack.policy, compiler_version="test", model="offline",
    )
    return artifact, sources, pack


def _compile(artifact, sources, pack):
    # Re-sealing makes negative controls cross-object validation tests, not stale-hash tests.
    artifact.evidence_snapshot_hash = artifact.evidence_ir.content_hash()
    artifact.artifact_hash = artifact.content_hash()
    return compile_review_artifact(artifact, requirement_pack=pack, source_records=sources)


def _codes(proof):
    return {item.code for item in proof.diagnostics}


@pytest.mark.parametrize("status", ["SUPPORTED", "CONTRADICTED", "NOT_FOUND"])
def test_semantic_review_needs_no_fake_numeric_witness(status):
    artifact, sources, pack = _fixture(status=status)
    proof = _compile(artifact, sources, pack)
    assert proof.decisions[0].status == status
    assert not _codes(proof)


def test_legacy_resolver_still_rejects_model_claims():
    artifact, sources, pack = _fixture(mode="registered_resolver")
    assert "ERP_CLAIM_NOT_ALLOWED" in _codes(_compile(artifact, sources, pack))


@pytest.mark.parametrize("requires", [False, True])
def test_positive_polarity_false_cannot_be_reported_as_supported(requires):
    artifact, sources, pack = _fixture(requires_calculation=requires, values=(90, 110))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w1", true_status="SUPPORTED")]
    assert "TERMINAL_WITNESS_STATUS_MISMATCH" in _codes(_compile(artifact, sources, pack))


def test_failed_eligibility_directly_contradicts_confirmation():
    artifact, sources, pack = _fixture(status="CONTRADICTED", requires_calculation=True, values=(90, 110))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w1", true_status="SUPPORTED")]
    assert _compile(artifact, sources, pack).decisions[0].status == "CONTRADICTED"


def test_negative_polarity_failed_eligibility_supports_correct_cancellation():
    artifact, sources, pack = _fixture(action_kind="cancel", requires_calculation=True, values=(110,))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w0", true_status="CONTRADICTED")]
    proof = _compile(artifact, sources, pack)
    assert not _codes(proof), proof
    assert proof.decisions[0].status == "SUPPORTED"


def test_negative_polarity_successful_eligibility_cannot_support_cancellation():
    artifact, sources, pack = _fixture(action_kind="cancel", requires_calculation=True, values=(90,))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w0", true_status="CONTRADICTED")]
    assert "TERMINAL_WITNESS_STATUS_MISMATCH" in _codes(_compile(artifact, sources, pack))


def test_one_failed_condition_can_justify_cancellation_without_hiding_other_results():
    artifact, sources, pack = _fixture(action_kind="cancel", requires_calculation=True, values=(90, 110))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w1", true_status="CONTRADICTED")]
    assert artifact.assessments[0].accepted_witness_ids == ["w0", "w1"]
    assert {ref.ref_id for ref in artifact.binding_proposals[0].term_refs if ref.kind == "WITNESS"} == {"w0", "w1"}
    proof = _compile(artifact, sources, pack)
    assert not _codes(proof), proof
    assert proof.decisions[0].status == "SUPPORTED"


def test_true_arithmetic_does_not_override_an_independent_semantic_conflict():
    prohibition = "Finance signoff is explicitly forbidden as payment authorization."
    artifact, sources, pack = _fixture(status="CONTRADICTED", requires_calculation=True, values=(90,),
                                       policy_text=f"Maximum amount: 100. {prohibition}")
    policy_claim = Claim(id="authorization", subject="policy", predicate="authorization_restriction",
                        value="Finance signoff is forbidden", source_id="policy", quote=prohibition, locator="line 1")
    artifact.evidence_ir.claims.append(policy_claim)
    artifact.assessments[0].claim_ids.append(policy_claim.id)
    artifact.submitted_claim_refs[artifact.assessments[0].check_id].append(policy_claim.id)
    artifact.binding_proposals[0].term_refs.append(ProofTermRef(kind="CLAIM", ref_id=policy_claim.id))
    artifact.binding_proposals[0].reason = "The amount is within the limit but the recorded Finance authorization is explicitly prohibited."
    assert not artifact.assessments[0].strong_status_links
    assert _compile(artifact, sources, pack).decisions[0].status == "CONTRADICTED"


def test_required_calculation_cannot_be_replaced_with_prose():
    artifact, sources, pack = _fixture(requires_calculation=True)
    assert "TERMINAL_WITNESS_REQUIRED" in _codes(_compile(artifact, sources, pack))


@pytest.mark.parametrize("defect,code", [
    ("missing_link", "NUMERIC_DECISION_LINK_REQUIRED"),
    ("polarity", "NUMERIC_DECISION_CONTRACT_MISMATCH"),
    ("operation", "NUMERIC_DECISION_CONTRACT_MISMATCH"),
    ("self_compare", "NUMERIC_DECISION_OPERANDS_INVALID"),
    ("renamed_self_compare", "NUMERIC_DECISION_OPERANDS_INVALID"),
    ("policy_operand", "NUMERIC_DECISION_OPERANDS_INVALID"),
])
def test_sealed_numeric_decision_rejects_wrong_model_proofs(defect, code):
    artifact, sources, pack = _fixture(requires_calculation=True, values=(110,), numeric_decision={
        "operation": "LTE", "true_status": "SUPPORTED", "policy_operand": None if defect == "renamed_self_compare" else 1,
    })
    assessment = artifact.assessments[0]
    assessment.strong_status_links = [StrongStatusLink(witness_id="w0", true_status="SUPPORTED")]
    if defect == "missing_link":
        assessment.strong_status_links = []
    elif defect == "polarity":
        assessment.strong_status_links[0].true_status = "CONTRADICTED"
    else:
        operands = ["amount0", "limit"]
        if defect == "self_compare":
            operands = ["amount0", "amount0"]
        elif defect == "renamed_self_compare":
            alias = next(item for item in artifact.evidence_ir.claims if item.id == "amount0").model_copy(update={"id": "alias"})
            artifact.evidence_ir.claims.append(alias)
            assessment.claim_ids.append(alias.id)
            artifact.submitted_claim_refs[assessment.check_id].append(alias.id)
            artifact.binding_proposals[0].term_refs.append(ProofTermRef(kind="CLAIM", ref_id=alias.id))
            operands = ["amount0", "alias"]
        elif defect == "policy_operand":
            operands.reverse()
        artifact.calculation_witnesses = [compute_witness(
            CalculationRequest(id="w0", check_id=assessment.check_id, facet_ref="complete_action_plan",
                               operation="GTE" if defect == "operation" else "LTE",
                               operands=[ProofTermRef(kind="CLAIM", ref_id=ref) for ref in operands]),
            claims={item.id: item for item in artifact.evidence_ir.claims}, witnesses={}, policy_values={},
            evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(), policy_snapshot_hash=artifact.policy_hash,
        )]
    proof = _compile(artifact, sources, pack)
    assert code in _codes(proof)
    assert proof.decisions[0].status != "SUPPORTED"


@pytest.mark.parametrize("amount,status,true_status,action", [
    (90, "SUPPORTED", "SUPPORTED", "confirm"),
    (110, "CONTRADICTED", "SUPPORTED", "confirm"),
    (110, "SUPPORTED", "CONTRADICTED", "cancel"),
    (90, "NOT_FOUND", "SUPPORTED", "confirm"),
])
def test_numeric_decision_preserves_valid_polarity_and_missing_evidence(amount, status, true_status, action):
    artifact, sources, pack = _fixture(status=status, requires_calculation=True, values=(amount,),
        action_kind=action, numeric_decision={"operation": "LTE", "true_status": true_status, "policy_operand": 1})
    if status != "NOT_FOUND":
        artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w0", true_status=true_status)]
    proof = _compile(artifact, sources, pack)
    assert proof.decisions[0].status == status
    assert not proof.diagnostics


def test_numeric_decision_accepts_a_sum_of_distinct_evidence_operands():
    artifact, sources, pack = _fixture(status="CONTRADICTED", requires_calculation=True, values=(90, 110),
        numeric_decision={"operation": "LTE", "true_status": "SUPPORTED", "policy_operand": 1})
    assessment = artifact.assessments[0]
    computed = {}
    for witness_id, operation, operands in [
        ("sum", "SUM", [("CLAIM", "amount0"), ("CLAIM", "amount1")]),
        ("comparison", "LTE", [("WITNESS", "sum"), ("CLAIM", "limit")]),
    ]:
        computed[witness_id] = compute_witness(
            CalculationRequest(id=witness_id, check_id=assessment.check_id, facet_ref="complete_action_plan",
                operation=operation, operands=[ProofTermRef(kind=kind, ref_id=ref) for kind, ref in operands]),
            claims={item.id: item for item in artifact.evidence_ir.claims}, witnesses=computed, policy_values={},
            evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(), policy_snapshot_hash=artifact.policy_hash,
        )
    artifact.calculation_witnesses = list(computed.values())
    artifact.submitted_witness_refs[assessment.check_id] = list(computed)
    assessment.accepted_witness_ids = list(computed)
    assessment.strong_status_links = [StrongStatusLink(witness_id="comparison", true_status="SUPPORTED")]
    binding = artifact.binding_proposals[0]
    binding.term_refs = [ref for ref in binding.term_refs if ref.kind != "WITNESS"] + [ProofTermRef(kind="WITNESS", ref_id="comparison")]
    proof = _compile(artifact, sources, pack)
    assert proof.decisions[0].status == "CONTRADICTED" and not proof.diagnostics


def test_decisive_link_cannot_name_an_unknown_witness():
    artifact, sources, pack = _fixture(requires_calculation=True, values=(90,))
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="absent", true_status="SUPPORTED")]
    assert "INVALID_TERMINAL_WITNESS_REFERENCE" in _codes(_compile(artifact, sources, pack))


def test_decisive_link_cannot_turn_an_amount_into_a_boolean():
    artifact, sources, pack = _fixture(requires_calculation=True, values=(90,))
    node = artifact.plan.nodes[0]
    artifact.calculation_witnesses = [compute_witness(
        CalculationRequest(id="w0", check_id=node.id, facet_ref="complete_action_plan",
                           operation="SUM", operands=[ProofTermRef(kind="CLAIM", ref_id="amount0"),
                                                      ProofTermRef(kind="CLAIM", ref_id="limit")]),
        claims={item.id: item for item in artifact.evidence_ir.claims}, witnesses={}, policy_values={},
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(), policy_snapshot_hash=artifact.policy_hash,
    )]
    artifact.assessments[0].strong_status_links = [StrongStatusLink(witness_id="w0", true_status="SUPPORTED")]
    assert "INVALID_TERMINAL_WITNESS_REFERENCE" in _codes(_compile(artifact, sources, pack))


def test_unused_false_comparison_cannot_be_hidden_from_verifier():
    artifact, sources, pack = _fixture(requires_calculation=True, values=(90, 110))
    artifact.assessments[0].accepted_witness_ids = ["w0"]
    artifact.binding_proposals[0].term_refs = [ref for ref in artifact.binding_proposals[0].term_refs if ref.ref_id != "w1"]
    assert "ERP_CALCULATION_NOT_CONSUMED" in _codes(_compile(artifact, sources, pack))


@pytest.mark.parametrize("mutation, expected", [
    ("quote", "ERP_CLAIM_NOT_OBSERVED"),
    ("low_confidence", "LOW_CONFIDENCE_CLAIM"),
    ("source_changed", "ERP_SOURCE_FINGERPRINT_MISMATCH"),
    ("missing_source", "ERP_SOURCE_REBINDING_REQUIRED"),
    ("coverage", "SOURCE_COVERAGE_INCOMPLETE"),
    ("polarity", "ERP_VERIFIER_STATUS_MISMATCH"),
])
def test_semantic_mode_preserves_existing_integrity_boundaries(mutation, expected):
    artifact, sources, pack = _fixture()
    if mutation == "quote":
        artifact.evidence_ir.claims[0].quote = "The source does not say this."
    elif mutation == "low_confidence":
        artifact.evidence_ir.claims[0].confidence = "low"
    elif mutation == "source_changed":
        sources["request"] = SourceRecord(source_id="request", kind="document", content="Changed")
    elif mutation == "missing_source":
        sources.pop("request")
    elif mutation == "coverage":
        artifact.assessments[0].examined_source_ids.remove("cover")
    else:
        artifact.binding_proposals[0].relation = "CHECK_VIOLATED"
    assert expected in _codes(_compile(artifact, sources, pack))


def test_new_pack_cannot_remove_sealed_contract_to_bypass_semantic_gate():
    artifact, sources, pack = _fixture()
    artifact.plan.nodes[0].action_contract = None
    artifact.plan_hash = artifact.plan.content_hash()
    assert "ERP_REVIEW_CONTRACT_MISSING" in _codes(_compile(artifact, sources, pack))


def test_new_mode_cannot_claim_legacy_signature_guarantee():
    artifact, sources, _pack = _fixture()
    assert "ERP_REVIEW_MODE_MISMATCH" in _codes(_compile(artifact, sources, ODOO_ERP_ACTION_PLAN_PACK))


@pytest.mark.parametrize("record_kind", ["", "proposed"])
def test_native_state_cannot_be_proved_by_policy_or_manager_material(record_kind):
    artifact, sources, pack = _fixture(live=True, record_kind=record_kind)
    assert "NATIVE_SOURCE_REQUIRED" in _codes(_compile(artifact, sources, pack))


def test_native_record_claim_closes_native_source_requirement():
    artifact, sources, pack = _fixture(live=True, record_kind="native")
    assert _compile(artifact, sources, pack).decisions[0].status == "SUPPORTED"


def test_native_claim_must_enter_terminal_binding_not_just_candidate_list():
    artifact, sources, pack = _fixture(live=True, record_kind="native")
    policy_claim = Claim(id="policy-limit", subject="policy", predicate="limit", value="100",
                         source_id="policy", quote="Maximum amount: 100.", locator="line 1")
    artifact.evidence_ir.claims.append(policy_claim)
    assessment = artifact.assessments[0]
    assessment.claim_ids.append(policy_claim.id)
    assessment.source_ids.append("policy")
    artifact.submitted_claim_refs[assessment.check_id].append(policy_claim.id)
    artifact.binding_proposals[0].term_refs = [ProofTermRef(kind="CLAIM", ref_id=policy_claim.id)]
    assert "NATIVE_SOURCE_REQUIRED" in _codes(_compile(artifact, sources, pack))


def test_record_field_value_is_rechecked_against_original_source():
    artifact, sources, pack = _fixture(record_kind="native")
    artifact.evidence_ir.claims[0].value = False
    assert "ERP_CLAIM_NOT_OBSERVED" in _codes(_compile(artifact, sources, pack))


def _dependent_fixture(*, declared=True):
    artifact, sources, pack = _fixture(requires_calculation=True, values=(90,))
    producer = artifact.plan.nodes[0]
    body = producer.action_contract.model_dump(mode="json")
    body.update(
        logical_check_id="check:downstream", contract_id="", execution_check_instance_id="",
        immutable_contract_hash="", requires_calculation=False,
        upstream_logical_check_ids=[producer.action_contract.logical_check_id] if declared else [],
    )
    contract = ERPReviewContract.model_validate(body)
    consumer = ProofNode(
        id=contract.execution_check_instance_id, kind="CHECK", statement="Review the dependent relationship.",
        requirement_refs=["erp_action_plan_valid"], facet_refs=["complete_action_plan"], action_contract=contract,
        upstream_check_ids=[producer.id] if declared else [],
    )
    artifact.plan.nodes += [consumer, ProofNode(id="root", kind="ALL", depends_on=[producer.id, consumer.id])]
    artifact.plan.roots = {"erp_action_plan_valid": "root"}
    artifact.plan_hash = artifact.plan.content_hash()
    binding = SemanticBindingProposal(
        id="dependent-binding", check_id=consumer.id, facet_ref="complete_action_plan", relation="CHECK_SATISFIED",
        term_refs=[ProofTermRef(kind="CLAIM", ref_id="approval"), ProofTermRef(kind="WITNESS", ref_id="w0")],
        reason="The dependent review consumes the declared producer result without changing it.",
    )
    sandbox = EvidenceSandbox.from_artifact(artifact=artifact, sources=sources.values())
    submitted = sandbox.submit_check(check_id=consumer.id, claim_ids=["approval"],
                                     binding_proposals=[binding], witness_ids=["w0"])
    # Also build the artifact for negative controls that bypass a rejected sandbox call.
    artifact.binding_proposals.append(binding)
    artifact.submitted_claim_refs[consumer.id] = ["approval"]
    artifact.submitted_binding_refs[consumer.id] = [binding.id]
    artifact.submitted_witness_refs[consumer.id] = ["w0"]
    artifact.assessments.append(CheckAssessment(
        check_id=consumer.id, claim_ids=["approval"], accepted_binding_ids=[binding.id],
        accepted_witness_ids=["w0"], source_ids=["policy", "request"], examined_source_ids=list(sources),
        status="SUPPORTED", reason="Independent review of the dependent relationship. Final classification: SUPPORTED",
    ))
    return artifact, sources, pack, submitted


def test_declared_upstream_witness_passes_real_sandbox_submission_and_kernel():
    artifact, sources, pack, submitted = _dependent_fixture()
    assert submitted["ok"], submitted
    proof = _compile(artifact, sources, pack)
    assert not _codes(proof), proof
    assert proof.decisions[0].status == "SUPPORTED"


def test_decisive_link_can_use_the_declared_upstream_boolean_without_fake_recalculation():
    artifact, sources, pack, submitted = _dependent_fixture()
    assert submitted["ok"], submitted
    artifact.assessments[-1].strong_status_links = [StrongStatusLink(witness_id="w0", true_status="SUPPORTED")]
    proof = _compile(artifact, sources, pack)
    assert not _codes(proof), proof
    assert proof.decisions[0].status == "SUPPORTED"


def test_undeclared_upstream_witness_is_rejected_at_sandbox_and_kernel():
    artifact, sources, pack, submitted = _dependent_fixture(declared=False)
    assert not submitted["ok"]
    assert "UNDECLARED_WITNESS_DEPENDENCY" in _codes(_compile(artifact, sources, pack))
