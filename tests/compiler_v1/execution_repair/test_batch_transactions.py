"""Offline orchestration controls: fake model responses, real sandbox and Kernel."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from agents.exceptions import ModelBehaviorError

from app.compiler_runtime.models import ActionProposal, CheckAssessment, ERPReviewContract, ProofNode, ProofPlan, ProposedAction
from app.compiler_runtime.proof_terms import ProofTermRef, SemanticBindingProposal
from app.compiler_runtime.runtime import EvidenceCompilerRuntime, ExecutorSummary, PreparedSource

from test_kernel_evidence_review import _fixture


def _case(dependencies, *, mode="evidence_review"):
    original, sources, pack = _fixture(mode=mode)
    proposal = ActionProposal(
        proposal_id="proposal:batch", actions=[ProposedAction(
            action_id="release", record_ref="request:R9", action="confirm",
        )], target_record_refs=["request:R9"], expected_preconditions={"request:R9": {}},
    )
    contracts = {}
    for label, upstream in dependencies.items():
        body = original.plan.nodes[0].action_contract.model_dump(mode="json")
        body.update(
            logical_check_id=label, local_check_id=label,
            contract_id="", execution_check_instance_id="", immutable_contract_hash="",
            proposal_hash=proposal.proposal_hash, upstream_logical_check_ids=list(upstream),
        )
        contracts[label] = ERPReviewContract.model_validate(body)
    nodes = [ProofNode(
        id=contract.execution_check_instance_id, kind="CHECK", statement=f"Review relationship {label}.",
        requirement_refs=["erp_action_plan_valid"], facet_refs=["complete_action_plan"], action_contract=contract,
        upstream_check_ids=[contracts[parent].execution_check_instance_id for parent in dependencies[label]],
    ) for label, contract in contracts.items()]
    root = nodes[0].id
    if len(nodes) > 1:
        root = "all:batch"
        nodes.append(ProofNode(id=root, kind="ALL", depends_on=[node.id for node in nodes]))
    plan = ProofPlan(plan_id="plan:batch", objective="Review the frozen proposed action.",
                     active_requirement_ids=["erp_action_plan_valid"], roots={"erp_action_plan_valid": root}, nodes=nodes)
    prepared = [PreparedSource(record=source, metadata={
        "source_fingerprint": original.evidence_ir.source_fingerprints[source_id],
    }) for source_id, source in sources.items()]
    return plan, proposal, prepared, pack


def _runtime(monkeypatch, pack, *, rejected=(), replace_first=False):
    settings = SimpleNamespace(llm_model="offline-orchestration-control")
    events, calls, inputs = [], [], []
    runtime = EvidenceCompilerRuntime(
        llm=SimpleNamespace(settings=settings), settings=settings, requirement_pack=pack,
        progress_sink=lambda kind, payload, _action: events.append((kind, copy.deepcopy(payload))),
    )

    def executor(**kwargs):
        kwargs["model_budget"].consume()
        focused = list(kwargs["focus_check_id"])
        calls.append(("executor", runtime.current_revision, focused))
        inputs.append(copy.deepcopy(kwargs["sandbox"]))
        candidate = copy.deepcopy(kwargs["sandbox"])
        candidate.read_source("request")
        nodes = {node.id: node for node in kwargs["plan"].nodes}
        for check_id in focused:
            label = nodes[check_id].action_contract.logical_check_id
            versions = ["obsolete", "current"] if replace_first else ["current"]
            for version in versions:
                receipt = candidate.bind_claim(
                    claim_id=f"claim:{label}:r{runtime.current_revision}:{version}", subject="R9",
                    predicate=f"relationship:{label}:{version}", value="approved by finance", source_id="request",
                    quote="Request R9 approved by finance.", locator="line 1",
                )
                assert receipt["ok"], receipt
                binding = SemanticBindingProposal(
                    id=f"binding:{label}:r{runtime.current_revision}:{version}", check_id=check_id,
                    facet_ref="complete_action_plan", relation="CHECK_SATISFIED",
                    term_refs=[ProofTermRef(kind="CLAIM", ref_id=receipt["claim"]["id"])],
                    reason="Candidate semantic relationship for independent review.",
                )
                submitted = candidate.submit_check(check_id=check_id, claim_ids=[receipt["claim"]["id"]],
                                                     binding_proposals=[binding], witness_ids=[])
                assert submitted["ok"], submitted
        kwargs["conversation"].sandbox = candidate
        return ExecutorSummary(completed_check_ids=focused), candidate

    def verifier(**kwargs):
        kwargs["model_budget"].consume()
        focused = list(kwargs["focus_check_id"])
        calls.append(("verifier", runtime.current_revision, focused))
        nodes = {node.id: node for node in kwargs["plan"].nodes}
        latest = {item.check_id: item for item in kwargs["sandbox"].latest_submissions()}
        results = []
        for check_id in focused:
            node, submission = nodes[check_id], latest[check_id]
            refused = node.action_contract.logical_check_id in rejected
            results.append(CheckAssessment(
                check_id=check_id, claim_ids=list(submission.claim_ids),
                accepted_binding_ids=[] if refused else list(submission.binding_ids),
                accepted_witness_ids=list(submission.witness_ids), source_ids=["request"],
                examined_source_ids=list(node.action_contract.source_refs),
                status="NOT_FOUND" if refused else "SUPPORTED",
                missing_fact="The submitted relationship was not supported by its cited text." if refused else "",
                reason="Independent verifier rejected the candidate relationship." if refused else "Final classification: SUPPORTED",
            ))
        return results

    def no_single_check_fallback(**_kwargs):
        raise AssertionError("A batch revision must never fall back to per-CHECK model loops")

    monkeypatch.setattr(runtime, "execute_plan", executor)
    monkeypatch.setattr(runtime, "verify", verifier)
    monkeypatch.setattr(runtime, "_run_check_frontier", no_single_check_fallback)
    return runtime, calls, events, inputs


def _run(runtime, plan, proposal, prepared, *, checkpoint=None):
    return runtime.run(
        active_requirement_ids=["erp_action_plan_valid"], prepared_sources=prepared,
        compiler_run_id="transaction-control", action_proposal=proposal,
        proof_plan=plan if checkpoint is None else None, checkpoint=checkpoint,
    )


@pytest.mark.parametrize("count", [1, 4])
def test_one_or_many_checks_use_exactly_one_executor_and_one_verifier(monkeypatch, count):
    plan, proposal, prepared, pack = _case({f"check{i}": [] for i in range(count)})
    runtime, calls, _events, _inputs = _runtime(monkeypatch, pack)
    result = _run(runtime, plan, proposal, prepared)
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"]
    assert len(calls[0][2]) == count
    assert result.compile_status == "COMMITTED"
    assert result.semantic_status == "SUPPORTED"
    assert len(result.checkpoint.completed_check_ids) == count


def test_failed_branch_keeps_maximal_valid_dependency_closure_and_raw_receipt(monkeypatch):
    plan, proposal, prepared, pack = _case({"a": [], "b": [], "c": ["b"], "d": ["a"]})
    runtime, calls, events, _inputs = _runtime(monkeypatch, pack, rejected={"b"})
    result = _run(runtime, plan, proposal, prepared)
    labels = {node.id: node.action_contract.logical_check_id for node in plan.nodes if node.kind == "CHECK"}
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"]
    assert {labels[item] for item in result.checkpoint.completed_check_ids} == {"a", "d"}
    assert {labels[item.check_id] for item in result.artifact.assessments} == {"a", "d"}
    assert result.compile_status == "NON_CONVERGED"
    assert result.semantic_status is None
    assert result.checkpoint.status == "running"
    assert {item.id for item in result.artifact.binding_proposals} == {
        "binding:a:r1:current", "binding:d:r1:current",
    }
    assert {item.id for item in result.artifact.evidence_ir.claims} == {
        "claim:a:r1:current", "claim:d:r1:current",
    }
    rejected = [payload for kind, payload in events if kind == "rejected_candidate"]
    assert len(rejected) == 1
    assert {labels[item] for item in rejected[0]["rejected_check_ids"]} == {"b", "c"}
    assert len(rejected[0]["candidate_artifact"]["assessments"]) == 4


def test_resume_uses_new_revision_only_for_remaining_checks_without_old_candidate_leak(monkeypatch):
    plan, proposal, prepared, pack = _case({"a": [], "b": [], "c": ["b"]})
    runtime, first_calls, _events, _inputs = _runtime(monkeypatch, pack, rejected={"b"})
    first = _run(runtime, plan, proposal, prepared)
    first_hash = first.checkpoint.artifact.artifact_hash
    resumed, calls, events, inputs = _runtime(monkeypatch, pack)
    result = _run(resumed, plan, proposal, prepared, checkpoint=first.checkpoint)
    labels = {node.id: node.action_contract.logical_check_id for node in plan.nodes if node.kind == "CHECK"}
    assert [kind for kind, _revision, _focus in first_calls] == ["executor", "verifier"]
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"]
    assert {labels[item] for item in calls[0][2]} == {"b", "c"}
    assert {revision for _kind, revision, _focus in calls} == {2}
    assert result.checkpoint.revision == 2
    assert all(payload["compiler_revision"] == 2 for _kind, payload in events)
    assert {item.id for item in inputs[0].binding_proposals} == {"binding:a:r1:current"}
    assert {item.id for item in result.artifact.binding_proposals} == {
        "binding:a:r1:current", "binding:b:r2:current", "binding:c:r2:current",
    }
    assert result.compile_status == "COMMITTED"
    assert first.checkpoint.artifact.artifact_hash == first_hash


def test_only_latest_submission_proof_terms_are_published(monkeypatch):
    plan, proposal, prepared, pack = _case({"a": []})
    runtime, calls, _events, _inputs = _runtime(monkeypatch, pack, replace_first=True)
    result = _run(runtime, plan, proposal, prepared)
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"]
    assert result.compile_status == "COMMITTED"
    assert all("obsolete" not in item.id for item in result.artifact.evidence_ir.claims)
    assert all("obsolete" not in item.id for item in result.artifact.binding_proposals)


def test_batch_does_not_weaken_legacy_resolver_proof_requirements(monkeypatch):
    plan, proposal, prepared, pack = _case({"a": []}, mode="registered_resolver")
    runtime, calls, events, _inputs = _runtime(monkeypatch, pack)
    result = _run(runtime, plan, proposal, prepared)
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"]
    assert result.compile_status == "NON_CONVERGED"
    assert not result.checkpoint.completed_check_ids
    assert not result.artifact.assessments
    rejected = [payload for kind, payload in events if kind == "rejected_candidate"]
    assert "ERP_CLAIM_NOT_ALLOWED" in rejected[0]["diagnostic_codes"]


@pytest.mark.parametrize("failed_stage", ["executor", "fine_verifier"])
def test_protocol_failure_reports_actual_phase_without_retry_or_committed_changes(
    monkeypatch, failed_stage,
):
    plan, proposal, prepared, pack = _case({"a": [], "b": []})
    first_runtime, _calls, _events, _inputs = _runtime(monkeypatch, pack, rejected={"b"})
    first = _run(first_runtime, plan, proposal, prepared)
    committed_before = first.artifact.model_dump(mode="json")
    resumed, calls, events, _inputs = _runtime(monkeypatch, pack)
    method = "execute_plan" if failed_stage == "executor" else "verify"
    original = getattr(resumed, method)

    def fail_after_response(**kwargs):
        original(**kwargs)
        raise ModelBehaviorError(f"injected {failed_stage} malformed output")

    monkeypatch.setattr(resumed, method, fail_after_response)
    result = _run(resumed, plan, proposal, prepared, checkpoint=first.checkpoint)
    expected_calls = ["executor"] if failed_stage == "executor" else ["executor", "verifier"]
    assert [kind for kind, _revision, _focus in calls] == expected_calls
    assert all(revision == 2 for _kind, revision, _focus in calls)
    rejected = [payload for kind, payload in events if kind == "rejected_candidate"]
    assert len(rejected) == 1
    assert rejected[0]["stage"] == failed_stage
    assert rejected[0]["status"] == "protocol_failure"
    assert rejected[0]["error_type"] == "ModelBehaviorError"
    assert result.artifact.model_dump(mode="json") == committed_before
    assert first.artifact.model_dump(mode="json") == committed_before
    assert result.checkpoint.completed_check_ids == first.checkpoint.completed_check_ids
    assert result.compile_status == "NON_CONVERGED"
    assert result.semantic_status is None
