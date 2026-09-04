from __future__ import annotations

from typing import Any


def compiler_snapshot(checkpoint: Any) -> dict[str, Any]:
    """Return the canonical parent-visible view of one Compiler checkpoint."""

    assessments = {item.check_id: item for item in checkpoint.artifact.assessments}
    completed = set(checkpoint.completed_check_ids)
    checks = []
    for node in checkpoint.artifact.plan.nodes:
        if node.kind != "CHECK":
            continue
        assessment = assessments.get(node.id)
        checks.append(
            {
                "check_id": node.id,
                "statement": node.statement,
                "facet_refs": list(node.facet_refs),
                "policy_refs": list(node.policy_refs),
                "upstream_check_ids": list(node.upstream_check_ids),
                "workflow_status": (
                    "active"
                    if node.id == checkpoint.active_check_id
                    else "completed" if node.id in completed else "pending"
                ),
                "proof_status": assessment.status if assessment is not None else "",
                "reason": assessment.reason if assessment is not None else "",
                "missing_fact": assessment.missing_fact if assessment is not None else "",
            }
        )
    return {
        "compiler_run_id": checkpoint.compiler_run_id,
        "revision": checkpoint.revision,
        "checkpoint_status": checkpoint.status,
        "compile_status": checkpoint.compile_status,
        "semantic_status": checkpoint.semantic_status,
        "active_check_id": checkpoint.active_check_id,
        "completed_check_ids": list(checkpoint.completed_check_ids),
        "total_checks": len(checks),
        "checks": checks,
        "decisions": (
            [item.model_dump(mode="json") for item in checkpoint.proof.decisions]
            if checkpoint.compile_status == "COMMITTED"
            else []
        ),
        "diagnostics": [item.model_dump(mode="json") for item in checkpoint.proof.diagnostics],
        "corrections": [item.model_dump(mode="json") for item in checkpoint.corrections],
        "proof_terms": {
            "claims": len(checkpoint.artifact.evidence_ir.claims),
            "bindings": len(checkpoint.artifact.binding_proposals),
            "witnesses": len(checkpoint.artifact.calculation_witnesses),
        },
        "lineage": {
            "plan_hash": checkpoint.artifact.plan_hash,
            "requirement_pack_id": checkpoint.artifact.requirement_pack_id,
            "requirement_pack_version": checkpoint.artifact.requirement_pack_version,
            "requirement_pack_hash": checkpoint.artifact.requirement_pack_hash,
            "proof_signature_hash": checkpoint.artifact.proof_signature_hash,
            "evidence_snapshot_hash": checkpoint.artifact.evidence_snapshot_hash,
            "source_snapshot_hash": checkpoint.artifact.source_snapshot_hash,
            "proposal_hash": checkpoint.artifact.proposal_hash,
            "policy_hash": checkpoint.artifact.policy_hash,
            "artifact_hash": checkpoint.artifact.artifact_hash,
        },
    }


__all__ = ["compiler_snapshot"]
