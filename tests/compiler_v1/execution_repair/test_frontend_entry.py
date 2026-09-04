"""Offline input/plan boundary checks, not claims of model task-solving success."""
from types import SimpleNamespace
from dataclasses import replace

import pytest

from app.compiler_runtime.runtime import EvidenceCompilerRuntime, VerificationBatch, _validate_registered_proof_plan, policy_excerpt_for, _initial_sandbox, _evidence_execution_payload, _verifier_contracts
from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK
from erp_agent_odoo.evidence_review import ReviewRouting, compile_review
from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog
from probe import control


def routing():
    return ReviewRouting(
        scenario_id="external-control",
        selected_template_ids=["treasury_controls.v1", "stock_disposal.v1"],
        action_bindings=[
            {"proposal_action_id": "transfer", "template_id": "treasury_controls.v1", "source_ids": ["policy:controls", "payment:q7"]},
            {"proposal_action_id": "destroy", "template_id": "stock_disposal.v1", "source_ids": ["policy:controls", "disposal:b8"]},
        ], unresolved_manager_inputs=[],
    )


def test_entry_exposes_original_materials_and_seals_executable_plan():
    request, sources, catalog = control()
    calls = []
    def phase(**kwargs):
        calls.append(kwargs)
        return routing()
    runtime = SimpleNamespace(compile_review_route=phase, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    plan, proposal, answer = compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)
    assert len(calls) == 1 and not calls[0].get("tools")
    assert calls[0]["payload"]["manager_request"] == request
    assert "pack_id" not in request
    assert plan.objective == request["task_objective"]
    assert {row["source_id"]: row["content"] for row in calls[0]["payload"]["sources"]} == {item.source_id: item.record.content for item in sources}
    assert len([node for node in plan.nodes if node.kind == "CHECK"]) == 3
    _validate_registered_proof_plan(
        plan, action_proposal=proposal, prepared_sources=sources,
        policy_excerpt=policy_excerpt_for(plan.active_requirement_ids, EVIDENCE_ACTION_REVIEW_PACK),
        requirement_pack=EVIDENCE_ACTION_REVIEW_PACK,
    )
    assert answer["routing"]["action_bindings"] == routing().model_dump(mode="json")["action_bindings"]
    assert answer["routing"]["scenario_id"] == request["scenario_id"]
    assert answer["routing"]["selected_template_ids"] == ["treasury_controls.v1", "stock_disposal.v1"]
    sandbox = _initial_sandbox(plan=plan, prepared_sources=sources, policy_excerpt=EVIDENCE_ACTION_REVIEW_PACK.policy)
    nodes = [node for node in plan.nodes if node.kind == "CHECK"]
    payload = _evidence_execution_payload(nodes, sandbox, [node.id for node in nodes])
    assert {item["source_id"]: item["content"] for item in payload["sources"]} == {item.source_id: item.record.content for item in sources}
    assert set(sandbox.read_source_ids) == {item.source_id for item in sources}
    assert next(item for item in payload["sources"] if item["source_id"] == "payment:q7")["record_fields"]["amount"] == 4000
    assert payload["terminal_submission_contract"]["missing_or_ambiguous"].startswith("submit no terminal binding")


@pytest.mark.parametrize("stage", ["executor", "fine_verifier"])
def test_original_review_scope_reaches_both_execution_phases(monkeypatch, stage):
    request, sources, catalog = control()
    request["task_objective"] = (
        "Review only the proposed transfer and disposal; do not expand this partial "
        "review to unrelated requests or claim portfolio optimality."
    )
    runtime = EvidenceCompilerRuntime(
        SimpleNamespace(settings=SimpleNamespace(llm_model="offline")),
        requirement_pack=EVIDENCE_ACTION_REVIEW_PACK,
    )
    monkeypatch.setattr(runtime, "_run_phase", lambda **_: routing())
    plan, _proposal, _answer = compile_review(
        runtime, manager_request=request, sources=sources, catalog=catalog,
    )
    observed = []
    class PayloadCaptured(RuntimeError):
        pass
    def capture(**kwargs):
        observed.append(kwargs)
        raise PayloadCaptured("Stop before any provider call")
    monkeypatch.setattr(runtime, "_run_phase", capture)
    sandbox = _initial_sandbox(
        plan=plan, prepared_sources=sources, policy_excerpt=EVIDENCE_ACTION_REVIEW_PACK.policy,
    )
    common = dict(plan=plan, sandbox=sandbox, policy_excerpt=EVIDENCE_ACTION_REVIEW_PACK.policy,
                  focus_check_id=[node.id for node in plan.nodes if node.kind == "CHECK"])
    def reject_discarded_legacy_payload(*_args, **_kwargs):
        raise AssertionError("Evidence execution must not build an unused legacy payload")
    if stage == "executor":
        monkeypatch.setattr("app.compiler_runtime.runtime._active_proof_signatures", reject_discarded_legacy_payload)
    with pytest.raises(PayloadCaptured):
        if stage == "executor":
            runtime.execute_plan(prepared_sources=sources, **common)
        else:
            runtime.verify(**common)
    call = observed[0]
    assert call["payload"]["review_objective"] == request["task_objective"]
    assert call["name"] == stage and call.get("thinking_override") is None
    assert (call["max_turns"], call["max_output_tokens"]) == (None, None)


def test_stale_source_stops_before_paid_call():
    request, sources, catalog = control()
    sources[0] = replace(sources[0], record=replace(sources[0].record, content=sources[0].record.content + " changed"))
    def phase(**kwargs):
        raise AssertionError("No paid call may see an unsealed snapshot")
    with pytest.raises(ValueError, match="fingerprint"):
        compile_review(SimpleNamespace(compile_review_route=phase), manager_request=request, sources=sources, catalog=catalog)


def test_model_cannot_bind_an_unknown_source():
    request, sources, catalog = control()
    answer = routing()
    answer.action_bindings[0].source_ids.append("solution.py")
    runtime = SimpleNamespace(compile_review_route=lambda **_: answer, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    with pytest.raises(ValueError, match="only admitted sources"):
        compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)


def test_routing_call_uses_authorized_output_budget():
    request, sources, catalog = control()
    calls = []
    def phase(**kwargs):
        calls.append(kwargs)
        return routing()
    runtime = SimpleNamespace(compile_review_route=phase, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)
    assert len(calls) == 1
    assert set(calls[0]) == {"payload", "output_type"}
    assert [item["id"] for item in calls[0]["payload"]["registered_review_catalog"]["templates"]] == [item["id"] for item in catalog["templates"]]

    runtime = EvidenceCompilerRuntime.__new__(EvidenceCompilerRuntime)
    runtime._run_phase = phase
    runtime.compile_review_route(payload={}, output_type=ReviewRouting)
    assert (calls[-1]["max_turns"], calls[-1]["max_output_tokens"]) == (None, None)


def test_verifier_contract_matches_execution_mode():
    evidence = {"action_contract": {"contract_kind": "ERP_CHECK", "execution_mode": "evidence_review"}}
    resolver = {"action_contract": {"contract_kind": "ERP_CHECK", "execution_mode": "registered_resolver"}}
    legacy = {"action_contract": {"predicate_program": {}}}
    assert _verifier_contracts([evidence]) == []
    assert "registered_resolver" in " ".join(_verifier_contracts([resolver]))
    assert "predicate_program" in " ".join(_verifier_contracts([legacy]))


def test_verifier_output_schema_is_strict(monkeypatch):
    captured = []
    configs = []
    class CapturingAgent:
        def __init__(self, **kwargs):
            captured.append(kwargs["output_type"])
    monkeypatch.setattr("app.compiler_runtime.runtime.Agent", CapturingAgent)
    monkeypatch.setattr("app.compiler_runtime.runtime.build_run_config", lambda *_args, **kwargs:
        configs.append(kwargs) or object())
    monkeypatch.setattr("app.compiler_runtime.runtime.run_agent_sync", lambda *_args, **_kwargs:
        SimpleNamespace(final_output=VerificationBatch(assessments=[]), raw_responses=[]))
    settings = SimpleNamespace(llm_model="offline", llm_temperature=0, llm_thinking_type="high",
        llm_base_url="https://api.commandcode.ai/provider/v1", evidence_reviewer_timeout_seconds=1)
    runtime = EvidenceCompilerRuntime(SimpleNamespace(available=True, settings=settings, calls=[]), settings=settings)
    runtime._run_phase(name="fine_verifier", prompt_file="evidence_verifier.md", payload={},
        output_type=VerificationBatch, max_turns=None)
    assert captured[0].is_strict_json_schema()
    assert configs[0]["disable_timeout"] is True
    assert "timeout_seconds" not in configs[0]


@pytest.mark.parametrize("defect", ["unknown_template", "duplicate_action", "wrong_action_kind", "unresolved"])
def test_bad_routing_still_cannot_become_an_executable_plan(defect):
    request, sources, catalog = control()
    answer = routing()
    if defect == "unknown_template":
        answer.selected_template_ids[0] = "unregistered.v1"
        answer.action_bindings[0].template_id = "unregistered.v1"
    elif defect == "duplicate_action":
        answer.action_bindings.append(answer.action_bindings[0])
    elif defect == "wrong_action_kind":
        answer.action_bindings[0].template_id, answer.action_bindings[1].template_id = (
            answer.action_bindings[1].template_id, answer.action_bindings[0].template_id,
        )
    else:
        answer.unresolved_manager_inputs.append("The governing workflow is ambiguous")
    runtime = SimpleNamespace(compile_review_route=lambda **_: answer, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    with pytest.raises(ValueError):
        compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)


def test_full_catalog_and_sources_reach_router_without_changing_the_plan():
    request, sources, catalog = control()
    baseline = SimpleNamespace(compile_review_route=lambda **_: routing(), requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    original_plan, _, _ = compile_review(baseline, manager_request=request, sources=sources, catalog=catalog)
    full_catalog = load_proof_catalog()
    catalog["templates"].extend(full_catalog["templates"])
    catalog["shared_nodes"].extend(full_catalog["shared_nodes"])
    calls = []
    def phase(**kwargs):
        calls.append(kwargs)
        return routing()
    runtime = SimpleNamespace(compile_review_route=phase, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    plan, _, _ = compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)
    view = calls[0]["payload"]["registered_review_catalog"]
    assert len(view["templates"]) == 8
    assert view == {key: catalog[key] for key in ("templates", "shared_nodes")}
    assert {item["source_id"]: item["content"] for item in calls[0]["payload"]["sources"]} == {
        item.source_id: item.record.content for item in sources
    }
    assert plan.model_dump(mode="json") == original_plan.model_dump(mode="json")
    assert set(ReviewRouting.model_json_schema()["properties"]) == {
        "scenario_id", "selected_template_ids", "action_bindings", "unresolved_manager_inputs",
    }
