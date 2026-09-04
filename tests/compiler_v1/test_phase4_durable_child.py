from __future__ import annotations

import asyncio
import copy
import json

import pytest
from app.compiler_runtime.runtime import (
    ExecutorSummary,
    _ExecutorConversation,
    _initial_sandbox,
    _sandbox_tools,
    prepare_sources,
)


def test_structured_record_must_be_admitted_before_prepare_sources() -> None:
    with pytest.raises(ValueError, match="must be admitted"):
        prepare_sources(
            [
                {
                    "source_id": "order-1",
                    "source_content": "{}",
                    "record_model": "derived.order_acceptance_facts",
                    "record_revision": "r1",
                }
            ]
        )


def test_initial_correction_keeps_full_registered_payload() -> None:
    from tests.compiler_v1.cases import registered_run_case

    _view, _proposal, policy, _pack, plan, prepared, runtime = registered_run_case()
    sandbox = _initial_sandbox(
        plan=plan,
        prepared_sources=[prepared],
        policy_excerpt=policy.to_policy_excerpt(),
    )
    conversation = _ExecutorConversation(
        checkpoint=sandbox,
        sandbox=copy.deepcopy(sandbox),
        session=object(),
    )
    captured = {}

    def fake_phase(**kwargs):
        captured.update(kwargs)
        return ExecutorSummary(
            completed_check_ids=[],
            unresolved_check_ids=[next(node.id for node in plan.nodes if node.kind == "CHECK")],
            summary="bounded regression probe",
            execution_status="PARTIAL",
        )

    runtime._run_phase = fake_phase
    runtime.execute_plan(
        plan=plan,
        prepared_sources=[prepared],
        policy_excerpt=policy.to_policy_excerpt(),
        sandbox=sandbox,
        focus_check_id=next(node.id for node in plan.nodes if node.kind == "CHECK"),
        runtime_observations=[{"diagnostic_code": "RECHECK"}],
        conversation=conversation,
    )

    assert captured["input_override"] is None
    assert captured["payload"]["proof_plan"]["focused_check"]["action_contract"]
    assert captured["payload"]["source_catalog"][0]["source_id"] == prepared.source_id


def test_registered_submit_rejects_status_used_as_terminal_relation() -> None:
    from tests.compiler_v1.cases import registered_run_case

    _view, _proposal, policy, _pack, plan, prepared, _runtime = registered_run_case()
    check = next(node for node in plan.nodes if node.kind == "CHECK")
    sandbox = _initial_sandbox(
        plan=plan,
        prepared_sources=[prepared],
        policy_excerpt=policy.to_policy_excerpt(),
    )
    tool = next(
        item
        for item in _sandbox_tools(
            sandbox,
            submission_review_by_check={
                check.id: {
                    "terminal_relations": list(check.action_contract.terminal_relations)
                }
            },
            allowed_source_ids=frozenset(check.action_contract.source_refs),
            record_fields_only=True,
        )
        if item.name == "submit_check"
    )
    result = json.loads(
        asyncio.run(
            tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "check_id": check.id,
                        "binding_proposals": [
                            {
                                "id": "binding:invalid-status-relation",
                                "check_id": check.id,
                                "facet_ref": "order_decision",
                                "relation": "SUPPORTED",
                                "term_refs": [
                                    {"kind": "POLICY", "ref_id": "minimum_quantity"}
                                ],
                                "reason": "status is not a registered relation key",
                            }
                        ],
                    }
                ),
            )
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "UNREGISTERED_TERMINAL_RELATION"
    assert not sandbox.binding_proposals
