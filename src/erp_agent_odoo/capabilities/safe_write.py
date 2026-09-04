from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from app.compiler_runtime.freshness import (
    ActionOutcome,
    ActionReceipt,
    assess_proof_freshness,
    record_action_receipt,
)
from app.compiler_runtime.models import ActionProposal, ProposedAction, _stable_hash
from app.compiler_runtime.requirement_pack import RequirementPack
from app.compiler_runtime.runtime import (
    CompilerRunCheckpoint,
    _validate_checkpoint_proof_closure,
)


def _mcp_result(raw: bytes) -> Mapping[str, object]:
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise TypeError("MCP action result must be a JSON object")
    envelope_error = payload.get("isError") is True
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(structured, Mapping):
        payload = structured
    nested = payload.get("result")
    if isinstance(nested, Mapping) and ("success" in nested or "error" in nested):
        payload = nested
    if not isinstance(payload, Mapping):
        raise TypeError("MCP action result must be a JSON object")
    if envelope_error:
        return {"success": False, "error": "MCP CallToolResult isError=true"}
    return payload


def _read_result(result_ref: str) -> bytes:
    prefix, digest, path = result_ref.split(":", 2)
    raw = Path(path).read_bytes()
    if prefix != "sha256" or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Stored MCP action result failed integrity verification")
    return raw


async def execute_proposal_once(
    checkpoint: CompilerRunCheckpoint,
    proposal: ActionProposal,
    *,
    requirement_pack: RequirementPack,
    current_policy_hash: str,
    read_snapshot: Callable[[], Awaitable[tuple[str, Mapping[str, str]]]],
    read_action_state: Callable[[ProposedAction], Awaitable[tuple[str, bool]]],
    execute_action: Callable[[ProposedAction, str], Awaitable[bytes]],
    idempotency_db: Path,
    receipt_dir: Path,
    observed_at: str,
) -> ActionReceipt:
    """Execute a proven proposal once, with a last-moment read before every action."""

    if (
        checkpoint.status != "completed"
        or checkpoint.compile_status != "COMMITTED"
        or checkpoint.semantic_status != "SUPPORTED"
    ):
        raise ValueError(
            "Action execution requires a completed COMMITTED/SUPPORTED checkpoint"
        )
    if (
        checkpoint.requirement_pack_id != requirement_pack.pack_id
        or checkpoint.requirement_pack_version != requirement_pack.version
        or checkpoint.requirement_pack_hash != requirement_pack.content_hash
    ):
        raise ValueError("Compiler checkpoint requirement pack changed")
    if (
        checkpoint.action_proposal != proposal
        or checkpoint.artifact.proposal_hash != proposal.proposal_hash
    ):
        raise ValueError("Compiler checkpoint proposal changed")
    source_ids = {
        str(item.get("source_id") or "").strip() for item in checkpoint.source_snapshot
    }
    if not source_ids or source_ids != set(checkpoint.artifact.evidence_ir.source_ids):
        raise ValueError("Compiler checkpoint source closure is incomplete")
    check_ids = {
        node.id for node in checkpoint.artifact.plan.nodes if node.kind == "CHECK"
    }
    if set(checkpoint.completed_check_ids) != check_ids:
        raise ValueError("Compiler checkpoint has incomplete CHECK closure")
    requiredness = {
        requirement_id: requirement_pack.default_required(requirement_id)
        for requirement_id in checkpoint.artifact.plan.active_requirement_ids
    }
    _validate_checkpoint_proof_closure(
        checkpoint,
        requirement_requiredness=requiredness,
        requirement_pack=requirement_pack,
    )
    decisions = [
        decision
        for decision in checkpoint.proof.decisions
        if decision.status == "SUPPORTED"
    ]
    if len(decisions) != 1:
        raise ValueError("Action execution requires exactly one SUPPORTED decision")
    decision = decisions[0]
    if (
        not current_policy_hash.strip()
        or current_policy_hash != decision.policy_hash
    ):
        raise ValueError("Current policy does not match the authorized proof")

    expected = {
        ref: str(precondition.get("upstream_revision") or "")
        for ref, precondition in proposal.expected_preconditions.items()
    }
    if set(expected) != source_ids:
        raise ValueError("Action targets must exactly cover the proven source closure")

    idempotency_db.parent.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(idempotency_db) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_executions (
                idempotency_key TEXT PRIMARY KEY,
                proposal_hash TEXT NOT NULL,
                record_ref TEXT NOT NULL,
                action TEXT NOT NULL,
                before_revision TEXT NOT NULL,
                status TEXT NOT NULL,
                result_ref TEXT NOT NULL DEFAULT ''
            )
            """
        )
        existing = {
            row[0]: row
            for row in connection.execute(
                "SELECT idempotency_key, record_ref, action, before_revision, status, result_ref "
                "FROM action_executions WHERE proposal_hash = ?",
                (proposal.proposal_hash,),
            )
        }

    expected_keys = [
        _stable_hash(
            {
                "proposal_hash": proposal.proposal_hash,
                "record_ref": action.record_ref,
                "action": action.action,
                "before_revision": expected[action.record_ref],
            }
        )
        for action in proposal.actions
    ]
    if not set(existing).issubset(expected_keys) or [
        key for key in expected_keys if key in existing
    ] != expected_keys[: len(existing)]:
        raise ValueError("Stored action executions do not match the proposal prefix")

    snapshot_hash = decision.source_snapshot_hash
    revisions = expected
    if not existing:
        snapshot_hash, current_revisions = await read_snapshot()
        current_revisions = dict(current_revisions)
        freshness = assess_proof_freshness(
            decision,
            current_source_snapshot_hash=snapshot_hash,
            current_policy_hash=current_policy_hash,
            current_proposal_hash=proposal.proposal_hash,
            expected_source_revisions=expected,
            current_source_revisions=current_revisions,
        )
        if freshness.status != "VALID":
            raise ValueError(
                "Action execution requires a VALID proof; "
                f"got {freshness.status}: {freshness.reasons}"
            )

    outcomes = []
    for index, action in enumerate(proposal.actions, start=1):
        before_revision = expected[action.record_ref]
        key = expected_keys[index - 1]
        current_revision, applied = await read_action_state(action)
        prior = existing.get(key)
        if prior is not None:
            _, record_ref, prior_action, prior_revision, status, result_ref = prior
            if (
                record_ref != action.record_ref
                or prior_action != action.action
                or prior_revision != before_revision
            ):
                raise ValueError("Stored action execution identity changed")
            if status in {"UNCERTAIN", "SUCCEEDED"} and result_ref:
                raw_result = _mcp_result(_read_result(result_ref))
                if (
                    raw_result.get("success") is True
                    and current_revision != before_revision
                    and applied
                ):
                    if status != "SUCCEEDED":
                        with sqlite3.connect(idempotency_db) as connection:
                            connection.execute(
                                "UPDATE action_executions SET status = 'SUCCEEDED' "
                                "WHERE idempotency_key = ?",
                                (key,),
                            )
                    outcomes.append(
                        ActionOutcome(
                            record_ref=action.record_ref,
                            action=action.action,
                            status="SUCCEEDED",
                            before_revision=before_revision,
                            after_revision=current_revision,
                            result_ref=result_ref,
                        )
                    )
                    continue
            raise ValueError(f"Action replay blocked: {action.record_ref}")

        if current_revision != before_revision or applied:
            raise ValueError(
                f"Action target became STALE before write: {action.record_ref}"
            )

        try:
            with sqlite3.connect(idempotency_db) as connection:
                connection.execute(
                    "INSERT INTO action_executions VALUES (?, ?, ?, ?, ?, 'UNCERTAIN', '')",
                    (
                        key,
                        proposal.proposal_hash,
                        action.record_ref,
                        action.action,
                        before_revision,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Action replay blocked: {action.record_ref}") from exc

        result_ref = ""
        status = "UNCERTAIN"
        try:
            raw = await execute_action(action, before_revision)
            if not isinstance(raw, bytes) or not raw:
                raise TypeError("MCP action adapter must return non-empty raw bytes")
            digest = hashlib.sha256(raw).hexdigest()
            result_path = receipt_dir / f"{index:02d}-{digest[:16]}.json"
            result_path.write_bytes(raw)
            result_ref = f"sha256:{digest}:{result_path.resolve()}"
            raw_result = _mcp_result(raw)
            after_revision, applied = await read_action_state(action)
            if raw_result.get("success") is not True:
                if after_revision == before_revision and not applied:
                    status = "FAILED"
                raise RuntimeError(
                    f"MCP action failed: {raw_result.get('error') or raw_result}"
                )
            if after_revision == before_revision or not applied:
                raise RuntimeError(
                    f"Odoo action outcome is UNCERTAIN: {action.record_ref}"
                )
            outcome = ActionOutcome(
                record_ref=action.record_ref,
                action=action.action,
                status="SUCCEEDED",
                before_revision=before_revision,
                after_revision=after_revision,
                result_ref=result_ref,
            )
            status = "SUCCEEDED"
        finally:
            with sqlite3.connect(idempotency_db) as connection:
                connection.execute(
                    "UPDATE action_executions SET status = ?, result_ref = ? WHERE idempotency_key = ?",
                    (status, result_ref, key),
                )
        outcomes.append(outcome)

    return record_action_receipt(
        decision,
        proposal,
        current_source_snapshot_hash=snapshot_hash,
        current_policy_hash=current_policy_hash,
        current_source_revisions=revisions,
        outcomes=outcomes,
        observed_at=observed_at,
    )


__all__ = ["execute_proposal_once"]
