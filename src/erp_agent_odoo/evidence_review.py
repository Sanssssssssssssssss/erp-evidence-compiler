"""Source-bound Compiler entry point. No Manager, Odoo client or benchmark code."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.compiler_runtime.runtime import EvidenceCompilerRuntime, PreparedSource
from erp_agent_odoo.capabilities.proof_dag import (
    action_proposal_from_manager_request,
    compile_task_compiler_plan,
    lower_erp_stage_to_proof_plan,
)


class ActionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_action_id: str
    template_id: str
    source_ids: list[str]


class ReviewRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    selected_template_ids: list[str]
    action_bindings: list[ActionBinding]
    unresolved_manager_inputs: list[str]


def compile_review(
    runtime: EvidenceCompilerRuntime,
    *,
    manager_request: Mapping[str, Any],
    sources: Sequence[PreparedSource],
    catalog: Mapping[str, Any],
    completed_action_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[Any, Any, dict[str, Any]]:
    """One model selection over complete sources, then validated DAG expansion."""
    source_by_id = {item.source_id: item for item in sources}
    if not sources or len(source_by_id) != len(sources):
        raise ValueError("Review requires distinct frozen sources")
    fingerprints = {key: hashlib.sha256(item.record.content.encode()).hexdigest() for key, item in source_by_id.items()}
    if any(item.source_fingerprint != fingerprints[key] for key, item in source_by_id.items()):
        raise ValueError("Frozen source content differs from its fingerprint")
    policy_ids = {key for key, item in source_by_id.items() if item.record.provenance.get("role") == "instruction"}
    if len(policy_ids) != 1:
        raise ValueError("Identify one admitted policy/instruction source")
    proposal = action_proposal_from_manager_request(manager_request)
    output = runtime.compile_review_route(
        payload={
            "manager_request": dict(manager_request),
            "registered_review_catalog": {key: catalog[key] for key in ("templates", "shared_nodes")},
            "sources": [{"source_id": key, "kind": item.record.kind, "role": item.record.provenance.get("role", ""), "content": item.record.content} for key, item in source_by_id.items()],
        },
        output_type=ReviewRouting,
    )
    for binding in output.action_bindings:
        scope = set(binding.source_ids)
        if not policy_ids <= scope <= set(source_by_id):
            raise ValueError("Routing must retain policy and only admitted sources")
    expanded = compile_task_compiler_plan(
        compiler_output=output.model_dump(mode="json"),
        manager_request=manager_request,
        catalog=catalog,
        binding_values={
            "proposal_hash": proposal.proposal_hash,
            "source_snapshot_hash": hashlib.sha256(json.dumps(fingerprints, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "policy_hash": fingerprints[next(iter(policy_ids))],
        },
    )
    plan = lower_erp_stage_to_proof_plan(
        expanded,
        revision=1 + len(completed_action_ids),
        completed_action_ids=completed_action_ids,
        source_fingerprints=fingerprints,
    )
    plan = type(plan).model_validate({
        **plan.model_dump(mode="json"),
        "objective": manager_request.get("task_objective", plan.objective),
    })
    if any(node.action_contract.requirement_pack_hash != runtime.requirement_pack.content_hash for node in plan.nodes if node.kind == "CHECK"):
        raise ValueError("Runtime requirement pack does not match the compiled execution mode")
    return plan, proposal, {"routing": output.model_dump(mode="json"), "expanded_plan": expanded}
