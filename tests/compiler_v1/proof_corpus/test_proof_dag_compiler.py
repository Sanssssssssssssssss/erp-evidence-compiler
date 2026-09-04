from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_agent_odoo.capabilities.proof_dag import (
    action_proposal_from_manager_request,
    compile_pack_selection_plan,
    compile_registered_proof_dag,
    compile_task_compiler_plan,
    load_proof_catalog,
    lower_erp_stage_to_proof_plan,
    task_compiler_catalog_view,
)

HERE = Path(__file__).parent
BINDINGS = {
    "proposal_hash": "proposal-fixture-hash",
    "source_snapshot_hash": "snapshot-fixture-hash",
    "policy_hash": "policy-fixture-hash",
}


def _load(name: str) -> dict:
    if name == "proof_templates.json":
        return load_proof_catalog()
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _payload(record_refs: list[str]) -> dict:
    values = {
        "state": "draft",
        "customer_ref": "customer:fixture",
        "vendor_ref": "vendor:fixture",
        "product_code": "PRODUCT",
        "quantity": 1,
        "price_unit": 1,
        "unit_cost": 1,
        "commitment_days": 1,
        "planned_arrival_days": 1,
        "origin_refs": ["origin:fixture"],
        "invoice_role": "regular",
        "order_ref": "sale.order:fixture",
        "amount_untaxed": 1,
        "payment_term": "30 Days",
        "tax_basis": "fixture",
        "currency": "USD",
    }
    return {
        "payload_version": 1,
        "snapshot_revision": "fixture:r1",
        "records": [
            {"record_ref": ref, "record_revision": "fixture:r1", "values": values}
            for ref in record_refs
        ],
    }


def _bindings_for_request(manager_request: dict) -> dict[str, str]:
    return {
        **BINDINGS,
        "proposal_hash": action_proposal_from_manager_request(
            manager_request
        ).proposal_hash,
    }


def test_five_complete_cases_lower_to_one_review_batch_each() -> None:
    catalog = _load("proof_templates.json")
    cases = _load("cases.json")["cases"]
    expected_node_counts = {2032: 3, 2016: 7, 2272: 4, 2053: 6, 2217: 7}

    for case in cases:
        compiled = compile_registered_proof_dag(
            case=case,
            catalog=catalog,
            binding_values=BINDINGS,
        )
        assert len(compiled["nodes"]) == expected_node_counts[case["scenario_number"]]
        assert compiled["logical_executor_invocations"] == 1
        assert compiled["logical_verifier_invocations"] == 1
        assert all("claim_ids" not in node and "verdict" not in node for node in compiled["nodes"])
        checks = [
            check
            for node in compiled["nodes"]
            for check in (
                [node] if node["kind"] == "shared" else node["checks"] + node["post_action_checks"]
            )
        ]
        assert all(check["execution"]["mode"] == "evidence_review" for check in checks)
        assert all(check["execution"]["tool"] == "read_source" for check in checks)
        assert all(check["execution"]["evidence"] for check in checks)
        assert all(
            check["execution"]["operation"] == check["resolver"] for check in checks
        )


def test_catalog_requires_one_valid_recipe_for_every_check() -> None:
    catalog = _load("proof_templates.json")
    checks = [*catalog["shared_nodes"]]
    for template in catalog["templates"]:
        checks.extend(template["checks"])
        checks.extend(template.get("post_action_checks", []))
    assert set(catalog["proof_recipes"]) == {check["id"] for check in checks}

    broken = json.loads(json.dumps(catalog))
    del broken["proof_recipes"]["portfolio_plan_valid"]
    with pytest.raises(ValueError, match="has no registered proof recipe"):
        compile_registered_proof_dag(
            case=_load("cases.json")["cases"][0],
            catalog=broken,
            binding_values=BINDINGS,
        )


def test_lowering_rejects_missing_shared_node_cycle_and_unbound_review() -> None:
    catalog = _load("proof_templates.json")
    case = _load("cases.json")["cases"][1]

    missing_shared = json.loads(json.dumps(case))
    missing_shared["action_dag"]["shared_nodes"].remove("supply_plan_released_checkpoint")
    with pytest.raises(ValueError, match="missing shared proof nodes"):
        compile_registered_proof_dag(case=missing_shared, catalog=catalog, binding_values=BINDINGS)

    cycle = json.loads(json.dumps(case))
    cycle["action_dag"]["edges"].append({"from": ["post_regular_invoices"], "to": "release_sales"})
    with pytest.raises(ValueError, match="acyclic"):
        compile_registered_proof_dag(case=cycle, catalog=catalog, binding_values=BINDINGS)

    with pytest.raises(ValueError, match="requires proposal"):
        compile_registered_proof_dag(case=case, catalog=catalog, binding_values={})


def test_pack_selection_expands_only_registered_shared_obligations() -> None:
    catalog = _load("proof_templates.json")
    plan = compile_pack_selection_plan(
        selected_template_ids=[
            "sales_order_release.v1",
            "purchase_order_release.v1",
            "customer_invoice_post.v1",
        ],
        catalog=catalog,
    )
    assert plan["template_ids"] == [
        "sales_order_release.v1",
        "purchase_order_release.v1",
        "customer_invoice_post.v1",
    ]
    assert {item["id"] for item in plan["shared_nodes"]} == {
        "portfolio_plan_valid",
        "lineage_closure_valid",
        "supply_plan_released_checkpoint",
    }
    with pytest.raises(ValueError, match="unknown templates"):
        compile_pack_selection_plan(
            selected_template_ids=["model_invented.v1"], catalog=catalog
        )


def test_task_compiler_view_and_lowerer_exclude_unrelated_shared_guards() -> None:
    catalog = _load("proof_templates.json")
    manager_request = {
        "scenario_id": "2272_easy_repair_plan_easy",
        "proposal_id": "manager:2272:repair:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:broken"],
            },
            {
                "action_id": "release_replacement_plan",
                "action_kind": "purchase.order.button_confirm",
                "stage": "replacement_release",
                "target_record_refs": ["purchase.order:replacement"],
            },
        ],
    }
    for action in manager_request["actions"]:
        action["action_payload"] = _payload(action["target_record_refs"])
    view = task_compiler_catalog_view(manager_request=manager_request, catalog=catalog)
    assert {item["id"] for item in view["templates"]} == {
        "cancel_and_repair_supply.v1",
        "purchase_order_release.v1",
    }
    assert {item["id"] for item in view["shared_nodes"]} == {
        "portfolio_plan_valid",
        "lineage_closure_valid",
    }

    output = {
        "scenario_id": manager_request["scenario_id"],
        "selected_template_ids": [
            "cancel_and_repair_supply.v1",
            "purchase_order_release.v1",
        ],
        "action_bindings": [
            {
                "proposal_action_id": "cancel_broken_po",
                "template_id": "cancel_and_repair_supply.v1",
            },
            {
                "proposal_action_id": "release_replacement_plan",
                "template_id": "purchase_order_release.v1",
            },
        ],
        "dependencies": [
            {
                "source_ids": ["portfolio_plan_valid"],
                "target_id": "cancel_broken_po",
            },
            {
                "source_ids": [
                    "cancel_broken_po",
                    "portfolio_plan_valid",
                    "lineage_closure_valid",
                ],
                "target_id": "release_replacement_plan",
            },
        ],
        "unresolved_manager_inputs": [],
    }
    with pytest.raises(ValueError, match="cannot supply registered dependencies"):
        compile_task_compiler_plan(
            compiler_output=output,
            manager_request=manager_request,
            catalog=catalog,
            binding_values=_bindings_for_request(manager_request),
        )


def test_2272_revision_one_keeps_typed_cancel_frontier_only() -> None:
    catalog = _load("proof_templates.json")
    manager_request = {
        "scenario_id": "2272_easy_repair_plan_easy",
        "proposal_id": "manager:2272:repair:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:broken"],
                "action_payload": _payload(["purchase.order:broken"]),
            },
            {
                "action_id": "release_replacement_plan",
                "action_kind": "purchase.order.button_confirm",
                "stage": "replacement_release",
                "target_record_refs": ["purchase.order:replacement"],
                "action_payload": _payload(["purchase.order:replacement"]),
            },
        ],
    }
    compiler_output = {
        "scenario_id": manager_request["scenario_id"],
        "selected_template_ids": [
            "cancel_and_repair_supply.v1",
            "purchase_order_release.v1",
        ],
        "action_bindings": [
            {
                "proposal_action_id": "cancel_broken_po",
                "template_id": "cancel_and_repair_supply.v1",
                "source_ids": ["source:instruction", "source:scenario"],
            },
            {
                "proposal_action_id": "release_replacement_plan",
                "template_id": "purchase_order_release.v1",
                "source_ids": ["source:instruction", "source:scenario"],
            },
        ],
        "unresolved_manager_inputs": [],
    }
    executor_plan = compile_task_compiler_plan(
        compiler_output=compiler_output,
        manager_request=manager_request,
        catalog=catalog,
        binding_values=_bindings_for_request(manager_request),
    )

    plan = lower_erp_stage_to_proof_plan(executor_plan, revision=1)
    checks = [node for node in plan.nodes if node.kind == "CHECK"]
    contracts = [node.action_contract for node in checks]

    assert {item.local_check_id for item in contracts} == {
        "target_is_exact_disrupted_commitment",
        "target_is_fresh_and_natively_cancellable",
        "cancellation_scope_is_exact",
    }
    assert {item.owner_action_id for item in contracts} == {"cancel_broken_po"}
    assert all(item.compiler_revision == 1 for item in contracts)
    assert all(item.execution_check_instance_id == node.id for item, node in zip(contracts, checks))
    assert not any("release_replacement_plan" in item.logical_check_id for item in contracts)
    assert not any(item.check_kind == "shared" for item in contracts)

    next_plan = lower_erp_stage_to_proof_plan(
        executor_plan,
        revision=2,
        completed_action_ids={"cancel_broken_po"},
    )
    next_contracts = [
        node.action_contract for node in next_plan.nodes if node.kind == "CHECK"
    ]
    assert {item.owner_action_id for item in next_contracts if item.check_kind == "action"} == {
        "release_replacement_plan"
    }
    assert {
        item.local_check_id for item in next_contracts if item.check_kind == "post_action"
    } == {"disrupted_commitment_is_cancelled"}
    assert "replacement_avoids_broken_path" in {
        item.local_check_id for item in next_contracts
    }
    replacement = next(
        item
        for item in next_contracts
        if item.local_check_id == "target_is_unique_fresh_draft_po"
    )
    assert any(
        "disrupted_commitment_is_cancelled" in check_id
        for check_id in replacement.upstream_logical_check_ids
    )


@pytest.mark.parametrize("confirmation_stage", ["confirm", "release"])
def test_disposition_keeps_common_checks_for_confirm_and_cancel(confirmation_stage) -> None:
    catalog = _load("proof_templates.json")
    manager_request = {
        "scenario_id": "disposition-fixture",
        "proposal_id": "manager:disposition:r1",
        "actions": [
            {
                "action_id": "confirm_order",
                "action_kind": "sale.order.action_confirm",
                "stage": confirmation_stage,
                "target_record_refs": ["sale.order:confirm"],
                "action_payload": _payload(["sale.order:confirm"]),
            },
            {
                "action_id": "cancel_order",
                "action_kind": "sale.order.action_cancel",
                "stage": "cancel",
                "target_record_refs": ["sale.order:cancel"],
                "action_payload": _payload(["sale.order:cancel"]),
            },
        ],
    }
    compiler_output = {
        "scenario_id": manager_request["scenario_id"],
        "selected_template_ids": ["sales_order_disposition.v1"],
        "action_bindings": [
            {
                "proposal_action_id": action["action_id"],
                "template_id": "sales_order_disposition.v1",
                "source_ids": ["source:fixture"],
            }
            for action in manager_request["actions"]
        ],
        "unresolved_manager_inputs": [],
    }
    executor_plan = compile_task_compiler_plan(
        compiler_output=compiler_output,
        manager_request=manager_request,
        catalog=catalog,
        binding_values=_bindings_for_request(manager_request),
    )
    proof_plan = lower_erp_stage_to_proof_plan(executor_plan)
    by_action = {}
    for node in proof_plan.nodes:
        if node.kind == "CHECK":
            by_action.setdefault(node.action_contract.owner_action_id, set()).add(
                node.action_contract.local_check_id
            )

    common = {
        "target_is_unique_fresh_reviewable_so",
        "request_identity_matches_order",
        "acceptance_policy_is_admitted",
        "acceptance_predicates_match_requested_action",
        "proposal_matches_policy_outcome",
    }
    assert by_action["confirm_order"] == common | {
        "confirm_payload_and_supply_are_valid"
    }
    assert by_action["cancel_order"] == common | {
        "cancel_target_is_unlocked_and_scope_exact"
    }

    missing_stage = json.loads(json.dumps(manager_request))
    del missing_stage["actions"][0]["stage"]
    with pytest.raises(ValueError, match="requires one of the registered stages"):
        compile_task_compiler_plan(
            compiler_output=compiler_output,
            manager_request=missing_stage,
            catalog=catalog,
            binding_values=_bindings_for_request(missing_stage),
        )


def test_task_compiler_plan_binds_manager_actions_before_lowering() -> None:
    catalog = _load("proof_templates.json")
    manager_request = {
        "scenario_id": "2016_easy_03_buy_only_fixed_downpayment",
        "proposal_id": "manager:2016:r1",
        "actions": [
            {
                "action_id": "release_sales",
                "action_kind": "sale.order.action_confirm",
                "stage": "release",
                "target_record_refs": ["so:c01", "so:c02", "so:c03", "so:c04"],
            },
            {
                "action_id": "release_purchase",
                "action_kind": "purchase.order.button_confirm",
                "stage": "release",
                "target_record_refs": ["po:finished:01"],
            },
            {
                "action_id": "post_downpayments",
                "action_kind": "account.move.action_post",
                "stage": "downpayment",
                "target_record_refs": ["invoice:c02:dp", "invoice:c03:dp", "invoice:c04:dp"],
            },
            {
                "action_id": "post_regular_only_invoice",
                "action_kind": "account.move.action_post",
                "stage": "regular",
                "target_record_refs": ["invoice:c01:regular"],
            },
            {
                "action_id": "post_balance_invoices",
                "action_kind": "account.move.action_post",
                "stage": "regular",
                "target_record_refs": [
                    "invoice:c02:regular",
                    "invoice:c03:regular",
                    "invoice:c04:regular",
                ],
            },
        ],
    }
    for action in manager_request["actions"]:
        action["action_payload"] = _payload(action["target_record_refs"])
    compiler_output = {
        "scenario_id": manager_request["scenario_id"],
        "selected_template_ids": [
            "sales_order_release.v1",
            "purchase_order_release.v1",
            "customer_invoice_post.v1",
        ],
        "action_bindings": [
            {
                "proposal_action_id": "release_sales",
                "template_id": "sales_order_release.v1",
                "source_ids": ["source:fixture"],
            },
            {
                "proposal_action_id": "release_purchase",
                "template_id": "purchase_order_release.v1",
                "source_ids": ["source:fixture"],
            },
            {
                "proposal_action_id": "post_downpayments",
                "template_id": "customer_invoice_post.v1",
                "source_ids": ["source:fixture"],
            },
            {
                "proposal_action_id": "post_regular_only_invoice",
                "template_id": "customer_invoice_post.v1",
                "source_ids": ["source:fixture"],
            },
            {
                "proposal_action_id": "post_balance_invoices",
                "template_id": "customer_invoice_post.v1",
                "source_ids": ["source:fixture"],
            },
        ],
        "unresolved_manager_inputs": [],
    }

    plan = compile_task_compiler_plan(
        compiler_output=compiler_output,
        manager_request=manager_request,
        catalog=catalog,
        binding_values=_bindings_for_request(manager_request),
    )
    assert len(plan["nodes"]) == 8
    assert plan["manager_proposal_id"] == "manager:2016:r1"
    assert plan["logical_executor_invocations"] == 1
    assert plan["logical_verifier_invocations"] == 1

    missing_payload = json.loads(json.dumps(manager_request))
    del missing_payload["actions"][0]["action_payload"]
    with pytest.raises(ValueError, match="requires payload version 1"):
        compile_task_compiler_plan(
            compiler_output=compiler_output,
            manager_request=missing_payload,
            catalog=catalog,
            binding_values=_bindings_for_request(manager_request),
        )

    wrong_pack = json.loads(json.dumps(compiler_output))
    wrong_pack["action_bindings"][0]["template_id"] = "purchase_order_release.v1"
    with pytest.raises(ValueError, match="Selected templates differ|cannot protect"):
        compile_task_compiler_plan(
            compiler_output=wrong_pack,
            manager_request=manager_request,
            catalog=catalog,
            binding_values=_bindings_for_request(manager_request),
        )

    from app.compiler_runtime.requirement_pack import ODOO_ERP_ACTION_PLAN_PACK
    from app.compiler_runtime.signatures import PlanConformanceGate

    proof_plan = lower_erp_stage_to_proof_plan(plan)
    PlanConformanceGate(ODOO_ERP_ACTION_PLAN_PACK.proof_signatures).validate(proof_plan)
    assert proof_plan.active_requirement_ids == ["erp_action_plan_valid"]
    checks = [node for node in proof_plan.nodes if node.kind == "CHECK"]
    assert {node.action_contract.owner_action_id for node in checks if node.action_contract.check_kind == "action"} == {
        "release_sales",
        "release_purchase",
    }
    assert all(node.action_contract is not None for node in checks)
    assert all(node.action_contract.contract_kind == "ERP_CHECK" for node in checks)
    assert all(node.action_contract.resolver_program.evidence for node in checks)
    assert all(
        node.action_contract.resolver_program.tool_name == "run_registered_check"
        for node in checks
    )
    assert all(node.id == node.action_contract.execution_check_instance_id for node in checks)
    assert all("action_payload" not in node.statement for node in checks)
    assert len({node.action_contract.logical_check_id for node in checks}) == len(checks)
