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
    assert [kind for kind, _revision, _focus in calls] == ["executor", "verifier"] * 2
    assert {labels[item] for item in calls[2][2]} == {"b", "c"}
    assert result.retry_count == 1
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
    assert len(rejected) == 2
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
    assert [kind for kind, _revision, _focus in first_calls] == ["executor", "verifier"] * 2
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


@pytest.mark.parametrize('retained', [1, 2, 4])
def test_rechecking_an_earlier_node_canonicalizes_completion_and_delivers_feedback(monkeypatch, retained):
    from app.compiler_runtime.runtime import CompilerCorrection, revise_compiler_checkpoint, _ordered_check_ids
    plan, proposal, prepared, pack = _case({'first': [], **{f'retained{i}': [] for i in range(retained)}})
    runtime, _, _, _ = _runtime(monkeypatch, pack, rejected={'first'})
    first = _run(runtime, plan, proposal, prepared)
    target = _ordered_check_ids(plan)[0]
    revised = revise_compiler_checkpoint(first.checkpoint, CompilerCorrection(
        kind='RECHECK', target_check_id=target, message='Review the missing authorization scope against the original sources.'), requirement_pack=pack)
    resumed, _, _, _ = _runtime(monkeypatch, pack)
    execute = resumed.execute_plan
    received = []
    def capture(**kwargs):
        received.extend(kwargs.get('runtime_observations', []))
        return execute(**kwargs)
    monkeypatch.setattr(resumed, 'execute_plan', capture)
    result = _run(resumed, plan, proposal, prepared, checkpoint=revised)
    assert result.compile_status=='COMMITTED' and result.checkpoint.status=='completed'
    assert result.checkpoint.completed_check_ids==_ordered_check_ids(plan)
    assert received and received[0]['diagnostic_code']=='HUMAN_RECHECK_REQUESTED'
    assert received[0]['message']==revised.corrections[-1].message
    legacy = result.checkpoint.model_copy(deep=True, update={
        'status':'running', 'compile_status':'NON_CONVERGED', 'semantic_status':None,
        'completed_check_ids':list(reversed(result.checkpoint.completed_check_ids))})
    def forbidden(**_kwargs):
        raise AssertionError('A fully proved checkpoint must finalize without a model')
    monkeypatch.setattr(resumed, 'execute_plan', forbidden)
    recovered = _run(resumed, plan, proposal, prepared, checkpoint=legacy)
    assert recovered.compile_status=='COMMITTED' and recovered.proof==result.proof
    for ids in ([target, target], ['invented']):
        with pytest.raises(ValueError, match='duplicate or unknown'):
            _run(resumed, plan, proposal, prepared, checkpoint=legacy.model_copy(update={'completed_check_ids':ids}))
    forged = legacy.model_copy(deep=True)
    forged.proof.decisions[0].status='CONTRADICTED'
    with pytest.raises(ValueError, match='Kernel replay'):
        _run(resumed, plan, proposal, prepared, checkpoint=forged)


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


def test_repair_receives_diagnosis_and_preserves_independent_proof(monkeypatch):
    plan, proposal, prepared, pack = _case({"a": [], "b": [], "c": ["b"]})
    rejected = {"b"}
    runtime, calls, _events, inputs = _runtime(monkeypatch, pack, rejected=rejected)
    verify, execute = runtime.verify, runtime.execute_plan
    feedback, sessions = [], []

    def first_rejection(**kwargs):
        result = verify(**kwargs)
        rejected.clear()
        return result

    def capture(**kwargs):
        feedback.append(kwargs.get("runtime_observations"))
        sessions.append(kwargs["conversation"].session)
        return execute(**kwargs)

    monkeypatch.setattr(runtime, "verify", first_rejection)
    monkeypatch.setattr(runtime, "execute_plan", capture)
    result = _run(runtime, plan, proposal, prepared)
    assert result.compile_status == "COMMITTED" and result.semantic_status == "SUPPORTED"
    assert len(calls) == 4 and result.retry_count == 1
    assert {term.id for term in inputs[1].binding_proposals} == {"binding:a:r1:current"}
    assert not feedback[0] and feedback[1][0]["previous_assessment"]["status"] == "NOT_FOUND"
    assert sessions[1] is None


@pytest.mark.parametrize("gap", ["SOURCE_MISSING", "BINDING_MISSING", "WITNESS_MISSING"])
def test_note_only_proof_gap_repairs_but_true_material_gap_does_not(monkeypatch, gap):
    plan, proposal, prepared, pack = _case({"a": []})
    runtime, calls, _events, _inputs = _runtime(monkeypatch, pack)
    execute, verify = runtime.execute_plan, runtime.verify

    def note_only_first(**kwargs):
        if calls:
            return execute(**kwargs)
        kwargs["model_budget"].consume()
        focus = list(kwargs["focus_check_id"])
        calls.append(("executor", runtime.current_revision, focus))
        candidate = copy.deepcopy(kwargs["sandbox"])
        assert candidate.submit_check(check_id=focus[0], claim_ids=[], binding_proposals=[],
                                      witness_ids=[], note="Unable to establish the required fact.")["ok"]
        kwargs["conversation"].sandbox = candidate
        return ExecutorSummary(unresolved_check_ids=focus), candidate

    def diagnose(**kwargs):
        items = verify(**kwargs)
        if len(calls) == 2:
            return [item.model_copy(update={"status": "NOT_FOUND", "gap_code": gap,
                     "reason": "Independent source review.", "missing_fact": "Exact required fact or proof term."})
                    for item in items]
        return items

    monkeypatch.setattr(runtime, "execute_plan", note_only_first)
    monkeypatch.setattr(runtime, "verify", diagnose)
    result = _run(runtime, plan, proposal, prepared)
    assert result.compile_status == "COMMITTED"
    assert len(calls) == (2 if gap == "SOURCE_MISSING" else 4)
    assert result.semantic_status == ("NOT_FOUND" if gap == "SOURCE_MISSING" else "SUPPORTED")


def test_plan_objection_sees_global_context_and_stops_without_repair(monkeypatch):
    from app.compiler_runtime.runtime import EvidenceVerificationBatch
    from app.compiler_runtime.sandbox import SourceRecord
    import hashlib

    plan, proposal, prepared, pack = _case({"a": [], "b": []})
    extra = SourceRecord(source_id="unrouted", title="Extra original", kind="document",
                         content="An explicitly required approval must also cover the recipient.")
    prepared.append(PreparedSource(record=extra, metadata={
        "source_fingerprint": hashlib.sha256(extra.content.encode()).hexdigest(),
    }))
    runtime, calls, events, _inputs = _runtime(monkeypatch, pack)
    monkeypatch.setattr(runtime, "verify", EvidenceCompilerRuntime.verify.__get__(runtime))
    packets = []

    def object_to_plan(**kwargs):
        if kwargs.get("model_budget"):
            kwargs["model_budget"].consume()
        packets.append(kwargs["payload"])
        return EvidenceVerificationBatch(assessments=[], plan_issue="Required recipient approval is unrouted.")

    monkeypatch.setattr(runtime, "_run_phase", object_to_plan)
    result = _run(runtime, plan, proposal, prepared)
    assert len(calls) == 1 and len(packets) == 1
    assert {s["source_id"] for s in packets[0]["sources"]} == {s.record.source_id for s in prepared}
    assert packets[0]["review_plan"]["roots"] == plan.roots
    assert len(packets[0]["review_plan"]["nodes"]) == len(plan.nodes)
    assert not result.artifact.assessments and not result.artifact.binding_proposals
    assert result.compile_status == "NON_CONVERGED" and result.retry_count == 0
    rejected = [p for k, p in events if k == "rejected_candidate"]
    assert rejected[0]["status"] == "plan_review_required"
    assert "recipient approval" in rejected[0]["error"]
    from app.compiler_runtime.runtime import CompilerSupervisionPause, _initial_sandbox
    with pytest.raises(CompilerSupervisionPause):
        runtime.verify(plan=plan, sandbox=_initial_sandbox(plan=plan, prepared_sources=prepared,
                       policy_excerpt=pack.policy), policy_excerpt=pack.policy, focus_check_id=plan.nodes[0].id)
    assert len(packets[1]["checks"]) == 1
    assert packets[1]["review_plan"] == packets[0]["review_plan"]
    assert packets[1]["sources"] == packets[0]["sources"]


@pytest.mark.parametrize("failed_stage", ["execute_plan", "verify"])
def test_protocol_failure_during_repair_keeps_first_pass_valid_closure(monkeypatch, failed_stage):
    plan, proposal, prepared, pack = _case({"a": [], "b": []})
    runtime, calls, _events, _inputs = _runtime(monkeypatch, pack, rejected={"b"})
    method = getattr(runtime, failed_stage)

    def fail_on_repair(**kwargs):
        if len(kwargs["focus_check_id"]) == 1:
            raise ModelBehaviorError("Injected incomplete repair response")
        return method(**kwargs)

    monkeypatch.setattr(runtime, failed_stage, fail_on_repair)
    result = _run(runtime, plan, proposal, prepared)
    assert result.compile_status == "NON_CONVERGED" and result.retry_count == 1
    assert {term.id for term in result.artifact.binding_proposals} == {"binding:a:r1:current"}
    assert len(calls) == (2 if failed_stage == "execute_plan" else 3)
