from __future__ import annotations

from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.compiler_runtime.models import (
    ActionProposal,
    DecisionProof,
    ProofFreshness,
    _stable_hash,
)


class ActionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_ref: str
    action: str
    status: Literal["SUCCEEDED", "FAILED"]
    before_revision: str
    after_revision: str = ""
    result_ref: str = ""

    @field_validator("record_ref", "action", "before_revision")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("action outcome identity must not be empty")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "ActionOutcome":
        if self.status == "SUCCEEDED" and (
            not self.after_revision.strip() or not self.result_ref.strip()
        ):
            raise ValueError(
                "successful action outcome requires after_revision and result_ref"
            )
        return self


class ActionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str
    requirement_id: str
    proposal_hash: str
    source_snapshot_hash: str
    policy_hash: str
    observed_at: str
    outcomes: tuple[ActionOutcome, ...]
    receipt_hash: str = ""

    @field_validator(
        "receipt_id",
        "requirement_id",
        "proposal_hash",
        "source_snapshot_hash",
        "policy_hash",
        "observed_at",
    )
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("receipt identity fields must not be empty")
        return value

    @model_validator(mode="after")
    def seal(self) -> "ActionReceipt":
        refs = [item.record_ref for item in self.outcomes]
        if not refs or len(refs) != len(set(refs)):
            raise ValueError("ActionReceipt requires one outcome per unique record")
        expected = _stable_hash(self.model_dump(mode="json", exclude={"receipt_hash"}))
        if self.receipt_hash and self.receipt_hash != expected:
            raise ValueError("ActionReceipt receipt_hash does not match its contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


def assess_proof_freshness(
    decision: DecisionProof,
    *,
    current_source_snapshot_hash: str,
    current_policy_hash: str,
    current_proposal_hash: str = "",
    expected_source_revisions: Mapping[str, str] | None = None,
    current_source_revisions: Mapping[str, str] | None = None,
) -> ProofFreshness:
    """Compare a proof receipt with the exact inputs visible before a write."""

    missing = []
    if not decision.source_snapshot_hash.strip():
        missing.append("SOURCE_SNAPSHOT_NOT_BOUND")
    if not current_source_snapshot_hash.strip():
        missing.append("CURRENT_SOURCE_SNAPSHOT_MISSING")
    if not decision.policy_hash.strip():
        missing.append("PROOF_POLICY_NOT_BOUND")
    if not current_policy_hash.strip():
        missing.append("CURRENT_POLICY_MISSING")
    if not decision.proposal_hash.strip():
        missing.append("PROOF_PROPOSAL_NOT_BOUND")
    if not current_proposal_hash.strip():
        missing.append("CURRENT_PROPOSAL_MISSING")
    if not expected_source_revisions or not current_source_revisions:
        missing.append("SOURCE_REVISION_LINEAGE_INCOMPLETE")
    else:
        if set(expected_source_revisions) != set(current_source_revisions):
            missing.append("SOURCE_REVISION_SCOPE_INCOMPLETE")
        missing.extend(
            f"SOURCE_REVISION_MISSING:{source_id}"
            for source_id in sorted(
                set(expected_source_revisions) | set(current_source_revisions)
            )
            if not str(expected_source_revisions.get(source_id) or "").strip()
            or not str(current_source_revisions.get(source_id) or "").strip()
        )
    if missing:
        return ProofFreshness(status="INVALID", reasons=missing)

    changed = []
    if decision.source_snapshot_hash != current_source_snapshot_hash:
        changed.append("SOURCE_SNAPSHOT_CHANGED")
    if decision.policy_hash != current_policy_hash:
        changed.append("POLICY_CHANGED")
    if decision.proposal_hash != current_proposal_hash:
        changed.append("PROPOSAL_CHANGED")
    changed.extend(
        f"SOURCE_REVISION_CHANGED:{source_id}"
        for source_id in sorted(expected_source_revisions)
        if str(expected_source_revisions[source_id])
        != str(current_source_revisions[source_id])
    )
    return ProofFreshness(status="STALE" if changed else "VALID", reasons=changed)


def record_action_receipt(
    decision: DecisionProof,
    proposal: ActionProposal,
    *,
    current_source_snapshot_hash: str,
    current_policy_hash: str,
    current_source_revisions: Mapping[str, str],
    outcomes: Sequence[ActionOutcome],
    observed_at: str,
    receipt_id: str = "",
) -> ActionReceipt:
    if decision.status != "SUPPORTED":
        raise ValueError("Only a SUPPORTED DecisionProof can authorize actions")
    expected_revisions = {
        source_id: str(precondition.get("upstream_revision") or "")
        for source_id, precondition in proposal.expected_preconditions.items()
    }
    freshness = assess_proof_freshness(
        decision,
        current_source_snapshot_hash=current_source_snapshot_hash,
        current_policy_hash=current_policy_hash,
        current_proposal_hash=proposal.proposal_hash,
        expected_source_revisions=expected_revisions,
        current_source_revisions=current_source_revisions,
    )
    if freshness.status != "VALID":
        raise ValueError(
            f"Action execution requires a VALID proof; got {freshness.status}: {freshness.reasons}"
        )

    proposed = {item.record_ref: item.action for item in proposal.actions}
    observed = {item.record_ref: item for item in outcomes}
    if set(observed) != set(proposed):
        raise ValueError("Action outcomes must exactly cover proposal targets")
    for source_id, outcome in observed.items():
        if outcome.action != proposed[source_id]:
            raise ValueError(f"Action outcome changed proposed action for {source_id!r}")
        if outcome.before_revision != str(current_source_revisions[source_id]):
            raise ValueError(f"Action outcome changed pre-write revision for {source_id!r}")

    payload = {
        "requirement_id": decision.requirement_id,
        "proposal_hash": proposal.proposal_hash,
        "source_snapshot_hash": current_source_snapshot_hash,
        "policy_hash": current_policy_hash,
        "observed_at": observed_at,
        "outcomes": [item.model_dump(mode="json") for item in outcomes],
    }
    return ActionReceipt(
        receipt_id=receipt_id or f"action_receipt:{_stable_hash(payload)[:20]}",
        **payload,
    )


__all__ = [
    "ActionOutcome",
    "ActionReceipt",
    "assess_proof_freshness",
    "record_action_receipt",
]
