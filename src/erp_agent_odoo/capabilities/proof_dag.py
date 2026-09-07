"""Lower an admitted action-DAG description without manufacturing proof terms."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

_BINDINGS = {"proposal_hash", "source_snapshot_hash", "policy_hash"}
_EVIDENCE_SOURCES = {
    "MANAGER_PROPOSAL": {"STRUCTURED"},
    "BOUND_SOURCE": {"STRUCTURED", "MODEL_QUOTE", "READ_AND_EXTRACT"},
    "LIVE_ODOO": {"RUNTIME_TOOL"},
    "UPSTREAM_CHECK": {"UPSTREAM"},
}


def load_proof_catalog(catalog_path: str | Path | None = None) -> dict[str, Any]:
    from erp_agent_odoo.tax_invoice import TEMPLATE_ID, tax_invoice_enabled

    path = Path(catalog_path) if catalog_path else Path(__file__).with_name("proof_templates.json")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not tax_invoice_enabled():
        catalog["templates"] = [t for t in catalog.get("templates", []) if t["id"] != TEMPLATE_ID]
        for check_id in ("tax_invoice_registry_verified", "tax_invoice_fields_match_receipt"):
            catalog.get("proof_recipes", {}).pop(check_id, None)
    return catalog


def _compile_check(check: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the registered evidence recipe; the model never invents this contract."""

    check_id = str(check["id"])
    recipes = catalog.get("proof_recipes")
    if not isinstance(recipes, Mapping) or check_id not in recipes:
        raise ValueError(f"CHECK {check_id!r} has no registered proof recipe")
    recipe = recipes[check_id]
    if not isinstance(recipe, Mapping) or recipe.get("resolver") != check.get("resolver"):
        raise ValueError(f"CHECK {check_id!r} proof recipe resolver differs")
    evidence = recipe.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"CHECK {check_id!r} proof recipe requires evidence groups")
    group_ids: set[str] = set()
    for group in evidence:
        if not isinstance(group, Mapping):
            raise TypeError(f"CHECK {check_id!r} has an invalid evidence group")
        group_id = str(group.get("group_id") or "").strip()
        source = str(group.get("source") or "")
        method = str(group.get("method") or "")
        facts = group.get("facts")
        if not group_id or group_id in group_ids:
            raise ValueError(f"CHECK {check_id!r} evidence group ids must be unique")
        if source not in _EVIDENCE_SOURCES or method not in _EVIDENCE_SOURCES[source]:
            raise ValueError(f"CHECK {check_id!r} has an invalid evidence acquisition")
        if not isinstance(facts, list) or not facts or len(facts) != len(set(facts)) or any(
            not isinstance(fact, str) or not fact.strip() for fact in facts
        ):
            raise ValueError(f"CHECK {check_id!r} evidence facts must be unique strings")
        source_role = str(group.get("source_role") or "").strip()
        if (source == "BOUND_SOURCE") != bool(source_role):
            raise ValueError(f"CHECK {check_id!r} BOUND_SOURCE requires source_role only")
        group_ids.add(group_id)
    return {
        **dict(check),
        "execution": {
            "mode": recipe.get("execution_mode", "evidence_review"),
            "requires_calculation": recipe.get("requires_calculation", check["resolver"] == "deterministic_arithmetic"),
            **({"numeric_decision": recipe["numeric_decision"]} if recipe.get("numeric_decision") is not None else {}),
            "tool": "read_source" if recipe.get("execution_mode", "evidence_review") == "evidence_review" else "run_registered_check",
            "operation": str(check["resolver"]),
            "evidence": [dict(item) for item in evidence],
            "result": {
                "true": "CHECK_SATISFIED",
                "false": "CHECK_VIOLATED",
            },
        },
    }


def task_compiler_catalog_view(
    *, manager_request: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Expose only packs and shared nodes that can protect Manager actions."""

    actions = list(manager_request.get("actions") or [])
    if not actions:
        raise ValueError("Task Compiler requires at least one Manager action")
    templates = []
    shared_ids: set[str] = set()
    covered_action_ids: set[str] = set()
    for template in catalog["templates"]:
        compatible = [
            action
            for action in actions
            if action["action_kind"] in template["protects"]
            and (
                not action.get("stage")
                or not template.get("applicable_stages")
                or action["stage"] in template["applicable_stages"]
            )
        ]
        if not compatible:
            continue
        templates.append(template)
        covered_action_ids.update(str(action["action_id"]) for action in compatible)
        required = template.get("requires_shared", [])
        if isinstance(required, dict):
            for action in compatible:
                shared_ids.update(required.get(str(action.get("stage") or ""), []))
        else:
            shared_ids.update(required)
    action_ids = {str(action["action_id"]) for action in actions}
    if missing := action_ids - covered_action_ids:
        raise ValueError(f"no registered template protects Manager actions: {sorted(missing)}")
    shared = {item["id"]: item for item in catalog["shared_nodes"]}
    if missing := shared_ids - set(shared):
        raise ValueError(f"templates require unknown shared nodes: {sorted(missing)}")
    return {
        "templates": templates,
        "shared_nodes": [item for item in catalog["shared_nodes"] if item["id"] in shared_ids],
    }


def compile_pack_selection_plan(
    *, selected_template_ids: list[str], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Expand a model's registered-template selection without trusting its prose."""

    if not selected_template_ids or len(selected_template_ids) != len(
        set(selected_template_ids)
    ):
        raise ValueError("pack selection requires unique registered templates")
    templates = {item["id"]: item for item in catalog["templates"]}
    unknown = set(selected_template_ids) - set(templates)
    if unknown:
        raise ValueError(f"unknown templates: {sorted(unknown)}")
    shared_ids = {
        item
        for template_id in selected_template_ids
        for required in [templates[template_id].get("requires_shared", [])]
        for item in (
            {entry for values in required.values() for entry in values}
            if isinstance(required, dict)
            else required
        )
    }
    shared = {item["id"]: item for item in catalog["shared_nodes"]}
    if missing := shared_ids - set(shared):
        raise ValueError(f"templates require unknown shared nodes: {sorted(missing)}")
    return {
        "template_ids": list(selected_template_ids),
        "templates": [templates[item] for item in selected_template_ids],
        "shared_nodes": [shared[item] for item in shared if item in shared_ids],
    }


def compile_registered_proof_dag(
    *,
    case: Mapping[str, Any],
    catalog: Mapping[str, Any],
    binding_values: Mapping[str, str],
) -> dict[str, Any]:
    """Expand registered templates into one dependency-checked review batch."""

    if set(binding_values) != _BINDINGS or any(
        not str(binding_values[key]).strip() for key in _BINDINGS
    ):
        raise ValueError("proof DAG requires proposal, source snapshot, and policy hashes")
    action_dag = case["action_dag"]
    if set(action_dag["bindings"]) != _BINDINGS:
        raise ValueError("case authorization bindings differ from the registered contract")

    templates = {item["id"]: item for item in catalog["templates"]}
    shared = {item["id"]: item for item in catalog["shared_nodes"]}
    selected = set(case["templates"])
    unknown_templates = selected - set(templates)
    if unknown_templates:
        raise ValueError(f"unknown templates: {sorted(unknown_templates)}")

    nodes: dict[str, dict[str, Any]] = {}
    for node_id in action_dag["shared_nodes"]:
        if node_id not in shared:
            raise ValueError(f"unknown shared proof node: {node_id}")
        nodes[node_id] = {
            "id": node_id,
            "kind": "shared",
            **_compile_check(shared[node_id], catalog),
        }

    for action in action_dag["nodes"]:
        node_id = str(action["id"])
        template_id = str(action["template"])
        if node_id in nodes:
            raise ValueError(f"duplicate proof node: {node_id}")
        if template_id not in selected:
            raise ValueError(f"action uses an unselected template: {template_id}")
        template = templates[template_id]
        required = template.get("requires_shared", [])
        if isinstance(required, dict):
            stage = str(action.get("stage") or "")
            required = required.get(stage, []) if stage else {
                item for values in required.values() for item in values
            }
        missing = set(required) - set(action_dag["shared_nodes"])
        if missing:
            raise ValueError(f"{node_id} is missing shared proof nodes: {sorted(missing)}")
        nodes[node_id] = {
            "id": node_id,
            "kind": "action_gate",
            "template_id": template_id,
            "stage": action.get("stage"),
            "targets": action["targets"],
            "source_ids": list(action.get("source_ids", [])),
            "action_payload": action.get("action_payload"),
            "checks": [_compile_check(check, catalog) for check in template["checks"]],
            "post_action_checks": [
                _compile_check(check, catalog)
                for check in template.get("post_action_checks", [])
            ],
        }

    graph = {node_id: set() for node_id in nodes}
    for edge in action_dag["edges"]:
        if not isinstance(edge, Mapping) or not edge.get("from") or not edge.get("to"):
            raise ValueError("proof DAG edges must be structured from/to records")
        source_ids = [str(item) for item in edge["from"]]
        target_id = str(edge["to"])
        unknown = set(source_ids + [target_id]) - set(nodes)
        if unknown:
            raise ValueError(f"proof DAG edge references unknown nodes: {sorted(unknown)}")
        required_postconditions = set(edge.get("required_postconditions", []))
        available_postconditions = {
            check["id"]
            for source_id in source_ids
            for check in nodes[source_id].get("post_action_checks", [])
        }
        if not required_postconditions <= available_postconditions:
            raise ValueError(
                "proof DAG edge requires unavailable postconditions: "
                f"{sorted(required_postconditions - available_postconditions)}"
            )
        graph[target_id].update(source_ids)
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("proof DAG must be acyclic") from exc

    return {
        "scenario_id": case["scenario_id"],
        "bindings": dict(binding_values),
        "nodes": [nodes[node_id] for node_id in order],
        "edges": list(action_dag["edges"]),
        "logical_executor_invocations": 1,
        "logical_verifier_invocations": 1,
    }


def compile_task_compiler_plan(
    *,
    compiler_output: Mapping[str, Any],
    manager_request: Mapping[str, Any],
    catalog: Mapping[str, Any],
    binding_values: Mapping[str, str],
) -> dict[str, Any]:
    """Lower a model's routing decisions around Manager-owned action facts."""

    scenario_id = str(manager_request["scenario_id"])
    if compiler_output.get("scenario_id") != scenario_id:
        raise ValueError("Task Compiler changed the scenario")

    actions = {item["action_id"]: item for item in manager_request["actions"]}
    if len(actions) != len(manager_request["actions"]):
        raise ValueError("Manager action ids must be unique")
    bindings = {
        item["proposal_action_id"]: item["template_id"]
        for item in compiler_output["action_bindings"]
    }
    if len(bindings) != len(compiler_output["action_bindings"]) or set(bindings) != set(
        actions
    ):
        raise ValueError("Task Compiler must bind every Manager action exactly once")

    selected = list(compiler_output["selected_template_ids"])
    selection = compile_pack_selection_plan(
        selected_template_ids=selected,
        catalog=catalog,
    )
    templates = {item["id"]: item for item in selection["templates"]}
    if set(bindings.values()) != set(selected):
        raise ValueError("Selected templates differ from action bindings")
    for action_id, template_id in bindings.items():
        action = actions[action_id]
        template = templates[template_id]
        if action["action_kind"] not in template["protects"]:
            raise ValueError(f"Template {template_id!r} cannot protect {action_id!r}")
        stage = action.get("stage")
        applicable_stages = template.get("applicable_stages", [])
        if applicable_stages and not stage:
            raise ValueError(
                f"Manager action {action_id!r} requires one of the registered stages: "
                f"{applicable_stages}"
            )
        if stage and applicable_stages and stage not in applicable_stages:
            raise ValueError(
                f"Template {template_id!r} does not apply to stage {stage!r} "
                f"for {action_id!r}"
            )
        payload = action.get("action_payload")
        if not isinstance(payload, Mapping) or payload.get("payload_version") != 1:
            raise ValueError(f"Manager action {action_id!r} requires payload version 1")
        if not str(payload.get("snapshot_revision") or "").strip():
            raise ValueError(f"Manager action {action_id!r} requires a snapshot revision")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError(f"Manager action {action_id!r} requires concrete records")
        if not all(isinstance(record, Mapping) for record in records):
            raise ValueError(f"Manager action {action_id!r} has invalid records")
        record_refs = [str(record.get("record_ref") or "") for record in records]
        targets = list(action["target_record_refs"])
        if (
            not all(record_refs)
            or len(record_refs) != len(set(record_refs))
            or len(targets) != len(set(targets))
            or set(record_refs) != set(targets)
        ):
            raise ValueError(f"Manager action {action_id!r} payload targets differ")
        required_fields = template.get("required_payload_fields", [])
        if isinstance(required_fields, Mapping):
            required_fields = required_fields.get(action["action_kind"], [])
        for record in records:
            if not str(record.get("record_revision") or "").strip():
                raise ValueError(f"Manager action {action_id!r} has an unversioned record")
            values = record.get("values")
            if not isinstance(values, Mapping):
                raise ValueError(f"Manager action {action_id!r} has no record values")
            if missing := {
                field for field in required_fields if values.get(field) is None
            }:
                raise ValueError(
                    f"Manager action {action_id!r} record {record['record_ref']!r} "
                    f"is missing payload fields: {sorted(missing)}"
                )

    if "dependencies" in compiler_output:
        raise ValueError("Task Compiler cannot supply registered dependencies")
    edges = _registered_dependencies(
        actions=actions,
        bindings=bindings,
        templates=templates,
        shared_nodes={item["id"]: item for item in selection["shared_nodes"]},
    )
    incoming = {
        node_id: {
            source_id
            for edge in edges
            if edge["to"] == node_id
            for source_id in edge["from"]
        }
        for node_id in actions
    }
    selected_shared_ids = {item["id"] for item in selection["shared_nodes"]}
    action_nodes = []
    for action_id, action in actions.items():
        template = templates[bindings[action_id]]
        required = template.get("requires_shared", [])
        if isinstance(required, dict):
            required = required.get(str(action.get("stage") or ""), [])
        required = set(required)
        actual_shared = incoming[action_id] & selected_shared_ids
        if not required <= actual_shared:
            raise ValueError(f"Action {action_id!r} is missing registered prerequisites")
        if unexpected := actual_shared - required:
            raise ValueError(
                f"Action {action_id!r} has unregistered shared prerequisites: "
                f"{sorted(unexpected)}"
            )
        action_nodes.append(
            {
                "id": action_id,
                "template": bindings[action_id],
                "targets": list(action["target_record_refs"]),
                "source_ids": list(
                    next(
                        item.get("source_ids", [])
                        for item in compiler_output["action_bindings"]
                        if item["proposal_action_id"] == action_id
                    )
                ),
                "action_payload": action["action_payload"],
                **({"stage": action["stage"]} if action.get("stage") else {}),
            }
        )

    action_proposal = action_proposal_from_manager_request(manager_request)
    if binding_values.get("proposal_hash") != action_proposal.proposal_hash:
        raise ValueError("Manager proposal hash differs from its canonical sealed payload")

    compiled = compile_registered_proof_dag(
        case={
            "scenario_id": scenario_id,
            "templates": selected,
            "action_dag": {
                "bindings": sorted(_BINDINGS),
                "shared_nodes": [item["id"] for item in selection["shared_nodes"]],
                "nodes": action_nodes,
                "edges": edges,
            },
        },
        catalog=catalog,
        binding_values=binding_values,
    )
    return {
        **compiled,
        "manager_proposal_id": manager_request["proposal_id"],
        "action_proposal": action_proposal.model_dump(mode="json"),
        "unresolved_manager_inputs": list(
            compiler_output.get("unresolved_manager_inputs") or []
        ),
    }


def action_proposal_from_manager_request(manager_request: Mapping[str, Any]) -> Any:
    """Seal the Manager payload once; later phases may reference but not rewrite it."""

    from app.compiler_runtime.models import ActionProposal, ProposedAction

    actions = []
    target_refs = []
    preconditions = {}
    for item in manager_request.get("actions") or []:
        payload = item.get("action_payload") or {}
        records = payload.get("records") or []
        for record in records:
            record_ref = str(record["record_ref"])
            revision = str(record["record_revision"])
            target_refs.append(record_ref)
            preconditions[record_ref] = {"upstream_revision": revision}
            actions.append(
                ProposedAction(
                    action_id=str(item["action_id"]),
                    stage=str(item.get("stage") or ""),
                    record_ref=record_ref,
                    action=str(item["action_kind"]),
                    arguments={
                        "record_revision": revision,
                        "values": dict(record["values"]),
                    },
                )
            )
    return ActionProposal(
        proposal_id=str(manager_request["proposal_id"]),
        actions=actions,
        target_record_refs=target_refs,
        expected_preconditions=preconditions,
    )


def _registered_dependencies(
    *,
    actions: Mapping[str, Mapping[str, Any]],
    bindings: Mapping[str, str],
    templates: Mapping[str, Mapping[str, Any]],
    shared_nodes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    incoming: dict[str, set[str]] = {}
    postconditions: dict[str, set[str]] = {}
    for action_id, action in actions.items():
        template = templates[bindings[action_id]]
        required = template.get("requires_shared", [])
        if isinstance(required, Mapping):
            required = required.get(str(action.get("stage") or ""), [])
        incoming.setdefault(action_id, set()).update(required)

        stage_rules = template.get("stage_prerequisites", {}).get(
            str(action.get("stage") or ""), []
        )
        for rule in stage_rules:
            candidates = {
                candidate_id
                for candidate_id, candidate in actions.items()
                if bindings[candidate_id] == rule["template_id"]
                and str(candidate.get("stage") or "") == rule["stage"]
            }
            if not candidates:
                raise ValueError(
                    f"Action {action_id!r} is missing registered stage prerequisite "
                    f"{rule['template_id']!r}/{rule['stage']!r}"
                )
            incoming[action_id].update(candidates)
            if candidates:
                postconditions.setdefault(action_id, set()).update(
                    rule.get("required_postconditions", [])
                )

    for shared_id, shared in shared_nodes.items():
        action_kinds = set(shared.get("established_by_action_kinds", []))
        if action_kinds:
            incoming[shared_id] = {
                action_id
                for action_id, action in actions.items()
                if action["action_kind"] in action_kinds
            }
            for action_id in incoming[shared_id]:
                postconditions.setdefault(shared_id, set()).update(
                    templates[bindings[action_id]]
                    .get("postconditions_for_shared", {})
                    .get(shared_id, [])
                )

    return [
        {
            "from": sorted(prerequisites),
            "to": dependent_id,
            **(
                {"required_postconditions": sorted(postconditions[dependent_id])}
                if postconditions.get(dependent_id)
                else {}
            ),
        }
        for dependent_id, prerequisites in incoming.items()
        if prerequisites
    ]


def lower_erp_stage_to_proof_plan(
    executor_plan: Mapping[str, Any],
    *,
    revision: int = 1,
    completed_action_ids: set[str] | frozenset[str] = frozenset(),
    source_fingerprints: Mapping[str, str] | None = None,
    source_records: Mapping[str, Any] | None = None,
) -> Any:
    """Retain the active ERP DAG frontier as typed CHECK nodes in ``ProofPlan``."""

    from app.compiler_runtime.models import (
        ActionProposal,
        ERPProposalRecord,
        ERPReviewContract,
        ProofNode,
        ProofPlan,
        RegisteredResolverProgram,
    )
    from app.compiler_runtime.requirement_pack import ODOO_ERP_ACTION_PLAN_PACK, EVIDENCE_ACTION_REVIEW_PACK

    if executor_plan.get("unresolved_manager_inputs"):
        raise ValueError("ERP stage still has unresolved Manager inputs")
    actions = [node for node in executor_plan["nodes"] if node["kind"] == "action_gate"]
    if not actions:
        raise ValueError("ERP stage requires at least one action gate")
    action_proposal = ActionProposal.model_validate(executor_plan["action_proposal"])
    bindings = dict(executor_plan["bindings"])
    if action_proposal.proposal_hash != bindings["proposal_hash"]:
        raise ValueError("ERP stage proposal changed after Task Compiler sealing")

    nodes_by_id = {str(node["id"]): node for node in executor_plan["nodes"]}
    action_ids = {str(node["id"]) for node in actions}
    completed = {str(item) for item in completed_action_ids}
    unknown_completed = completed - action_ids
    if unknown_completed:
        raise ValueError(f"unknown completed ERP actions: {sorted(unknown_completed)}")

    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes_by_id}
    blocked: set[str] = set()
    for edge in executor_plan["edges"]:
        target = str(edge["to"])
        sources = {str(item) for item in edge["from"]}
        incoming[target].update(sources)
        source_actions = sources & action_ids
        if not source_actions <= completed:
            blocked.add(target)
    changed = True
    while changed:
        changed = False
        for edge in executor_plan["edges"]:
            target = str(edge["to"])
            if target not in blocked and blocked.intersection(str(item) for item in edge["from"]):
                blocked.add(target)
                changed = True

    active_actions = {
        node_id for node_id in action_ids if node_id not in completed and node_id not in blocked
    }
    if not active_actions:
        raise ValueError("ERP stage has no active action frontier")

    needed_nodes = set(active_actions)
    pending = list(active_actions)
    while pending:
        node_id = pending.pop()
        for dependency in incoming.get(node_id, set()):
            if dependency in blocked or dependency in needed_nodes:
                continue
            needed_nodes.add(dependency)
            if dependency not in completed:
                pending.append(dependency)

    proposal_records = [
        ERPProposalRecord(
            action_id=item.action_id,
            stage=item.stage,
            action_kind=item.action,
            record_ref=item.record_ref,
            record_revision=str(item.arguments["record_revision"]),
            values=dict(item.arguments["values"]),
        )
        for item in action_proposal.actions
    ]
    records_by_action = {
        action_id: [record for record in proposal_records if record.action_id == action_id]
        for action_id in action_ids
    }
    all_source_refs = sorted(
        {
            str(source_id)
            for action in actions
            for source_id in action.get("source_ids") or []
        }
    )

    specs: list[dict[str, Any]] = []
    numeric_groups: dict[tuple[str, str], list[str]] = {}
    logical_ids_by_node: dict[str, list[str]] = {}
    for node_id in [str(node["id"]) for node in executor_plan["nodes"] if str(node["id"]) in needed_nodes]:
        node = nodes_by_id[node_id]
        if node["kind"] == "shared":
            checks = [
                {
                    "id": node_id,
                    "observation": node["observation"],
                    "resolver": node["resolver"],
                    "execution": node["execution"],
                }
            ]
            template_id = "shared"
            owner_action_id = f"shared:{node_id}"
            stage = ""
            check_kind = "shared"
            action_kind = "shared"
            targets = [record.record_ref for record in proposal_records]
            records = proposal_records
            source_refs = all_source_refs
        else:
            stage = str(node.get("stage") or "")
            completed_action = node_id in completed
            checks = (
                list(node.get("post_action_checks") or [])
                if completed_action
                else [
                    check
                    for check in node["checks"]
                    if not check.get("applicable_stages")
                    or stage in check["applicable_stages"]
                ]
            )
            template_id = str(node["template_id"])
            owner_action_id = node_id
            check_kind = "post_action" if completed_action else "action"
            action_kind = next(
                item.action for item in action_proposal.actions if item.action_id == node_id
            )
            targets = [str(item) for item in node["targets"]]
            records = records_by_action[node_id]
            source_refs = [str(item) for item in node.get("source_ids") or []]

        logical_ids = []
        entries = []
        for check in checks:
            decision = check["execution"].get("numeric_decision")
            for record in records if decision and decision.get("steps") else [None]:
                source = (source_records or {}).get(record.record_ref) if record and record.record_ref in source_refs else None
                fields = source.record_fields if source and source.record_model == decision["record_model"] else {}
                condition = check.get("enabled_field")
                if condition and fields and fields.get(condition) is False:
                    continue
                mode = check.get("invoice_mode")
                if mode and fields and fields.get("mode") in {"fixed_amount", "percentage"} and fields["mode"] != mode:
                    continue
                entries.append((check, record))
        for check, record in entries:
            local_check_id = str(check["id"])
            logical_id = f"{owner_action_id}:{template_id}:{local_check_id}"
            decision = check["execution"].get("numeric_decision")
            if record is not None:
                logical_id += f":{record.record_ref}"
            if check.get("numeric_group") == "eligibility":
                decision = {**decision, "true_status": "CONTRADICTED" if action_kind == "sale.order.action_cancel" else "SUPPORTED"}
                numeric_groups.setdefault((owner_action_id, record.record_ref), []).append(logical_id)
            logical_ids.append(logical_id)
            specs.append(
                {
                    "logical_check_id": logical_id,
                    "template_id": template_id,
                    "local_check_id": local_check_id,
                    "owner_action_id": owner_action_id,
                    "stage": stage,
                    "check_kind": check_kind,
                    "action_kind": action_kind,
                    "target_record_refs": [record.record_ref] if record is not None else targets,
                    "proposal_records": [record] if record is not None else records,
                    "source_refs": source_refs,
                    "resolver_id": str(check["resolver"]),
                    "resolver_evidence": list(check["execution"]["evidence"]),
                    "execution_mode": check["execution"].get("mode", "registered_resolver"),
                    "requires_calculation": check["execution"].get("requires_calculation", False),
                    "numeric_decision": decision,
                    "statement": str(check["observation"]),
                    "upstream_node_ids": sorted(incoming.get(node_id, set()) & needed_nodes),
                }
            )
        logical_ids_by_node[node_id] = logical_ids

    modes = {spec["execution_mode"] for spec in specs}
    if len(modes) != 1:
        raise ValueError("A revision cannot mix evidence review and legacy resolver contracts")
    requirement_pack = EVIDENCE_ACTION_REVIEW_PACK if modes == {"evidence_review"} else ODOO_ERP_ACTION_PLAN_PACK
    contracts = []
    for spec in specs:
        action_upstream = sorted(
            logical_id
            for node_id in spec.pop("upstream_node_ids")
            for logical_id in logical_ids_by_node.get(node_id, [])
        )
        statement = spec.pop("statement")
        resolver_id = spec.pop("resolver_id")
        resolver_evidence = spec.pop("resolver_evidence")
        if spec["execution_mode"] == "evidence_review":
            # Data consumers name producers; action order is not a CHECK input.
            requested_producers = {
                producer for group in resolver_evidence
                for producer in group.get("upstream_checks", [])
            }
            # A required observed postcondition is a real guard, even when
            # independent evidence-reading CHECKs otherwise have no data edge.
            upstream_logical_ids = [
                logical_id for logical_id in action_upstream
                if any(item["logical_check_id"] == logical_id and item["check_kind"] == "post_action" for item in specs)
            ]
            for producer in sorted(requested_producers):
                matches = [
                    item["logical_check_id"] for item in specs
                    if item["local_check_id"] == producer
                    and (item["owner_action_id"] == spec["owner_action_id"]
                         or item["check_kind"] == "shared")
                ]
                if len(matches) != 1:
                    raise ValueError(f"CHECK {spec['local_check_id']} requires one producer {producer!r}, found {len(matches)}")
                upstream_logical_ids.extend(matches)
        else:
            upstream_logical_ids = action_upstream
        contract = ERPReviewContract(
            **spec,
            compiler_revision=revision,
            proposal_hash=action_proposal.proposal_hash,
            source_snapshot_hash=(
                hashlib.sha256(json.dumps(
                    {ref: source_fingerprints[ref] for ref in spec["source_refs"]},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest() if source_fingerprints is not None
                else str(bindings["source_snapshot_hash"])
            ),
            policy_source_hash=str(bindings["policy_hash"]),
            requirement_pack_hash=requirement_pack.content_hash,
            upstream_logical_check_ids=upstream_logical_ids,
            resolver_program=RegisteredResolverProgram(
                resolver_id=resolver_id,
                evidence=resolver_evidence,
            ),
        )
        contracts.append((contract, statement))

    instance_by_logical = {
        contract.logical_check_id: contract.execution_check_instance_id
        for contract, _statement in contracts
    }
    checks = [
        ProofNode(
            id=contract.execution_check_instance_id,
            kind="CHECK",
            statement=statement,
            upstream_check_ids=[
                instance_by_logical[logical_id]
                for logical_id in contract.upstream_logical_check_ids
            ],
            requirement_refs=["erp_action_plan_valid"],
            facet_refs=["complete_action_plan"],
            action_contract=contract,
        )
        for contract, statement in contracts
    ]
    if not checks:
        raise ValueError("ERP stage produced no executable CHECKs")
    plan_nodes: list[Any] = list(checks)
    grouped_ids = set()
    for (owner, target), logical_ids in numeric_groups.items():
        ids = [instance_by_logical[item] for item in logical_ids]
        action_kind = next(record.action_kind for record in proposal_records if record.action_id == owner)
        plan_nodes.append(ProofNode(id=f"eligibility:r{revision}:{owner}:{target}",
            kind="ANY" if action_kind == "sale.order.action_cancel" else "ALL", depends_on=ids))
        grouped_ids.update(ids)
    # An explicit all-disabled screening policy cannot become an empty positive gate.
    for node in actions:
        if any(check.get("numeric_group") == "eligibility" for check in node["checks"]) and node["id"] in active_actions:
            if any((node["id"], record.record_ref) not in numeric_groups for record in records_by_action[node["id"]]):
                raise ValueError("Screening requires at least one registered active eligibility predicate")
    roots = [node.id for node in plan_nodes if node.id not in grouped_ids]
    root_id = roots[0]
    if len(roots) > 1:
        root_id = f"all:erp-stage:r{revision}:{bindings['proposal_hash'][:16]}"
        plan_nodes.append(
            ProofNode(id=root_id, kind="ALL", depends_on=roots)
        )
    return ProofPlan(
        plan_id=f"plan:erp-stage:r{revision}:{bindings['proposal_hash'][:16]}",
        objective="Review the current immutable ERP action stage before execution.",
        active_requirement_ids=["erp_action_plan_valid"],
        policy_refs=[],
        roots={"erp_action_plan_valid": root_id},
        nodes=plan_nodes,
    )


__all__ = [
    "action_proposal_from_manager_request",
    "compile_pack_selection_plan",
    "compile_registered_proof_dag",
    "compile_task_compiler_plan",
    "load_proof_catalog",
    "lower_erp_stage_to_proof_plan",
    "task_compiler_catalog_view",
]
