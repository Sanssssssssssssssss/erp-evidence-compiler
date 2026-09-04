from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.compiler_runtime.freshness import assess_proof_freshness
from app.compiler_runtime.models import ActionProposal, DecisionProof, ProposedAction


def test_action_proposal_is_sealed_and_freshness_is_separate_from_proof_status() -> None:
    proposal = ActionProposal(
        proposal_id="proposal_2032_r1",
        actions=[ProposedAction(record_ref="sale.order:o01", action="cancel")],
        target_record_refs=["sale.order:o01"],
        expected_preconditions={
            "sale.order:o01": {
                "upstream_revision": "2026-08-28 10:00:00",
                "state": "draft",
            }
        },
    )
    decision = DecisionProof(
        requirement_id="order_acceptance_plan_valid",
        root_node_id="check.o01",
        status="SUPPORTED",
        plan_hash="plan-hash",
        source_snapshot_hash="source-hash-r1",
        evidence_snapshot_hash="evidence-hash",
        proposal_hash=proposal.proposal_hash,
        policy_hash="policy-hash-r1",
        stop_reason="supported",
    )

    assert proposal.proposal_hash == proposal.content_hash()
    assert assess_proof_freshness(
        decision,
        current_source_snapshot_hash="source-hash-r1",
        current_proposal_hash=proposal.proposal_hash,
        current_policy_hash="policy-hash-r1",
        expected_source_revisions={"sale.order:o01": "2026-08-28 10:00:00"},
        current_source_revisions={"sale.order:o01": "2026-08-28 10:00:00"},
    ).status == "VALID"
    stale = assess_proof_freshness(
        decision,
        current_source_snapshot_hash="source-hash-r2",
        current_proposal_hash=proposal.proposal_hash,
        current_policy_hash="policy-hash-r1",
        expected_source_revisions={"sale.order:o01": "2026-08-28 10:00:00"},
        current_source_revisions={"sale.order:o01": "2026-08-28 10:00:00"},
    )
    assert stale.status == "STALE"
    assert stale.reasons == ["SOURCE_SNAPSHOT_CHANGED"]
