"""Tau tools for one durable Evidence Compiler child run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_WRITE_LOCK = Lock()


def _manifest() -> dict[str, Any]:
    manifest_path = os.getenv("ERP_COMPILER_SOURCE_MANIFEST", "").strip()
    if not manifest_path:
        raise ValueError("ERP_COMPILER_SOURCE_MANIFEST is required for a new review")
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"sources": payload}


def _resolve_sources(source_refs: list[str]) -> list[dict[str, Any]]:
    items = _manifest().get("sources")
    if not isinstance(items, list):
        raise ValueError("Source manifest must be a list or contain a sources list")
    by_id = {
        str(item.get("source_id") or "").strip(): dict(item)
        for item in items
        if isinstance(item, Mapping) and str(item.get("source_id") or "").strip()
    }
    missing = sorted(set(source_refs) - set(by_id))
    if missing:
        raise ValueError(f"Source refs are not admitted by the manifest: {missing}")
    resolved = []
    for source_id in source_refs:
        source = by_id[source_id]
        content = str(source.get("source_content") or "")
        if not content.strip():
            raise ValueError(f"Admitted source {source_id!r} has no text content")
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        supplied = str(source.get("source_fingerprint") or "").strip()
        if supplied and supplied != fingerprint:
            raise ValueError(f"Admitted source {source_id!r} changed after manifest creation")
        source.update(
            {
                "source_id": source_id,
                "source_fingerprint": fingerprint,
                "already_persisted": True,
            }
        )
        resolved.append(source)
    return resolved


def _resolve_action_proposal(proposal_ref: str) -> Any:
    from app.compiler_runtime.models import ActionProposal

    proposals = _manifest().get("proposals") or []
    proposal = next(
        (
            item
            for item in proposals
            if isinstance(item, Mapping)
            and str(item.get("proposal_id") or "").strip() == proposal_ref
        ),
        None,
    )
    if proposal is None:
        raise ValueError(f"Proposal ref is not admitted by the manifest: {proposal_ref!r}")
    return ActionProposal.model_validate(proposal)


def _manager_request_from_proposal(proposal: Any) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    if proposal.supersedes_proposal_id:
        raise ValueError("ERP review does not yet admit superseding proposals")
    for action in proposal.actions:
        if not action.action_id or not action.stage:
            raise ValueError("Every ERP action requires action_id and stage")
        if set(action.arguments) != {"record_revision", "values"}:
            raise ValueError(
                f"ERP action {action.action_id!r} requires only record_revision and values"
            )
        revision = str(action.arguments["record_revision"] or "").strip()
        values = action.arguments["values"]
        if not revision or not isinstance(values, Mapping):
            raise ValueError(f"ERP action {action.action_id!r} has no sealed record payload")
        item = grouped.setdefault(
            action.action_id,
            {
                "action_id": action.action_id,
                "action_kind": action.action,
                "stage": action.stage,
                "target_record_refs": [],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": proposal.proposal_hash,
                    "records": [],
                },
            },
        )
        if item["action_kind"] != action.action or item["stage"] != action.stage:
            raise ValueError(f"ERP action id {action.action_id!r} mixes action semantics")
        item["target_record_refs"].append(action.record_ref)
        item["action_payload"]["records"].append(
            {
                "record_ref": action.record_ref,
                "record_revision": revision,
                "values": dict(values),
            }
        )
    request = {
        "scenario_id": proposal.proposal_id,
        "proposal_id": proposal.proposal_id,
        "actions": list(grouped.values()),
    }
    from erp_agent_odoo.capabilities.proof_dag import action_proposal_from_manager_request

    if action_proposal_from_manager_request(request).proposal_hash != proposal.proposal_hash:
        raise ValueError("ERP Manager proposal changed while building the child request")
    return request


def _completed_action_ids_from_sources(
    proposal: Any,
    prepared_sources: list[Any],
    *,
    policy_hash: str,
) -> set[str]:
    """Project trusted action receipts into the next immutable review stage."""

    from app.compiler_runtime.freshness import ActionReceipt

    proposed = {item.record_ref: item for item in proposal.actions}
    observed: dict[str, Any] = {}
    successful: dict[str, Any] = {}
    for source in prepared_sources:
        if source.record.provenance.get("role") != "action_receipt":
            continue
        receipt = ActionReceipt.model_validate(json.loads(source.record.content))
        if (
            receipt.requirement_id != "erp_action_plan_valid"
            or receipt.proposal_hash != proposal.proposal_hash
            or receipt.policy_hash != policy_hash
        ):
            raise ValueError("Action receipt differs from the admitted ERP review contract")
        for outcome in receipt.outcomes:
            action = proposed.get(outcome.record_ref)
            if action is None or outcome.action != action.action:
                raise ValueError("Action receipt names an action outside the sealed proposal")
            if outcome.before_revision != str(action.arguments["record_revision"]):
                raise ValueError("Action receipt before_revision differs from the proposal")
            prior = observed.get(outcome.record_ref)
            if prior is not None and prior != outcome:
                raise ValueError("Conflicting action receipts were admitted")
            observed[outcome.record_ref] = outcome
            if outcome.status != "SUCCEEDED":
                continue
            successful[outcome.record_ref] = outcome

    records_by_action: dict[str, set[str]] = {}
    for action in proposal.actions:
        records_by_action.setdefault(action.action_id, set()).add(action.record_ref)
    return {
        action_id
        for action_id, record_refs in records_by_action.items()
        if record_refs <= set(successful)
    }


def _run_compiler(
    request: dict[str, Any],
    checkpoint_payload: dict[str, Any] | None,
    run_id: str,
    run_dir: Path,
    checkpoint_sink: Any,
    progress_sink: Any,
) -> dict[str, Any]:
    """Run the only admitted source-bound Compiler path."""

    from app.compiler_runtime.models import ActionProposal
    from app.compiler_runtime.requirement_pack import registered_requirement_pack
    from app.compiler_runtime.runtime import (
        CompilerRunCheckpoint,
        EvidenceCompilerRuntime,
        prepare_sources,
    )
    from app.config import get_settings
    from app.llm import LlmClient
    from erp_agent_odoo.evidence_review import compile_review

    settings = get_settings()
    requirement_pack = registered_requirement_pack(
        request["requirement_pack_id"],
        request["requirement_pack_version"],
    )
    if requirement_pack.content_hash != request["requirement_pack_hash"]:
        raise ValueError("Registered RequirementPack changed after this run was admitted")
    action_proposal = (
        ActionProposal.model_validate(request["action_proposal"])
        if request.get("action_proposal")
        else None
    )
    if action_proposal is None:
        raise ValueError("A durable evidence review requires one admitted action proposal")
    checkpoint = (
        CompilerRunCheckpoint.model_validate(checkpoint_payload)
        if checkpoint_payload is not None
        else None
    )
    prepared_sources = [] if checkpoint is not None else prepare_sources(request["sources"])
    llm = LlmClient(settings)
    runtime = EvidenceCompilerRuntime(
        llm,
        settings=settings,
        requirement_pack=requirement_pack,
        progress_sink=progress_sink,
        executor_session_db_path=run_dir / "executor-sessions.sqlite",
    )
    proof_plan = None
    try:
        if checkpoint is None:
            source_fingerprints = {
                item.record.source_id: str(item.metadata["source_fingerprint"])
                for item in prepared_sources
            }
            instruction_hashes = [
                source_fingerprints[item.record.source_id]
                for item in prepared_sources
                if item.record.provenance.get("role") == "instruction"
            ]
            if len(instruction_hashes) != 1:
                raise ValueError("ERP review requires exactly one admitted instruction source")
            completed_action_ids = _completed_action_ids_from_sources(
                action_proposal,
                prepared_sources,
                policy_hash=instruction_hashes[0],
            )
            manager_request = _manager_request_from_proposal(action_proposal)
            manager_request["task_objective"] = request["task_objective"]
            proof_plan, compiled_proposal, frontend = compile_review(
                runtime,
                manager_request=manager_request,
                sources=prepared_sources,
                catalog=request["catalog"],
                completed_action_ids=completed_action_ids,
            )
            if compiled_proposal.proposal_hash != action_proposal.proposal_hash:
                raise ValueError("Task Compiler changed the admitted action proposal")
            _write_json(run_dir / "task-compiler-output.json", frontend)
            _write_json(run_dir / "proof-plan.json", proof_plan.model_dump(mode="json"))
        result = runtime.run(
            task_objective="" if proof_plan is not None else request["task_objective"],
            active_requirement_ids=request["active_requirement_ids"],
            prepared_sources=prepared_sources,
            policy_excerpt=request["policy_excerpt"],
            requirement_requiredness=request["requirement_requiredness"],
            compiler_run_id=run_id,
            checkpoint=checkpoint,
            checkpoint_sink=lambda item: checkpoint_sink(item.model_dump(mode="json")),
            action_proposal=action_proposal,
            proof_plan=proof_plan,
        )
    finally:
        model_log = run_dir / "model-calls.jsonl"
        model_log.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK, model_log.open("a", encoding="utf-8", newline="\n") as stream:
            for call in llm.calls:
                stream.write(
                    json.dumps(call.to_debug_dict(), ensure_ascii=False, default=str) + "\n"
                )
    return {
        "status": "completed",
        "checkpoint": result.checkpoint.model_dump(mode="json") if result.checkpoint else None,
        "model_calls": [call.to_dict() for call in llm.calls],
    }


def _recheck_compiler(
    checkpoint_payload: dict[str, Any],
    *,
    correction_id: str,
    expected_revision: int,
    check_id: str,
    message: str,
) -> dict[str, Any]:
    from app.compiler_runtime.requirement_pack import registered_requirement_pack
    from app.compiler_runtime.runtime import (
        CompilerCorrection,
        CompilerRunCheckpoint,
        revise_compiler_checkpoint,
    )

    checkpoint = CompilerRunCheckpoint.model_validate(checkpoint_payload)
    requirement_pack = registered_requirement_pack(
        checkpoint.requirement_pack_id,
        checkpoint.requirement_pack_version,
    )
    correction = CompilerCorrection(
        correction_id=correction_id,
        kind="RECHECK",
        target_check_id=check_id,
        message=message,
    )
    existing = next(
        (item for item in checkpoint.corrections if item.correction_id == correction_id),
        None,
    )
    if existing is not None:
        if existing != correction:
            raise ValueError(f"Correction id {correction_id!r} was reused with different input")
        return checkpoint_payload
    if checkpoint.revision != expected_revision:
        raise ValueError(
            f"Stale compiler revision: expected {expected_revision}, current {checkpoint.revision}"
        )
    return revise_compiler_checkpoint(
        checkpoint,
        correction,
        requirement_pack=requirement_pack,
    ).model_dump(mode="json")


def _safe_id(value: str, *, label: str) -> str:
    value = value.strip()
    if not _RUN_ID.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _run_dir(tau: Any, run_id: str) -> Path:
    session_id = _safe_id(str(tau.context.session_id or ""), label="parent session id")
    configured = os.getenv("ERP_COMPILER_RUN_ROOT", "").strip()
    if not configured:
        raise ValueError("ERP_COMPILER_RUN_ROOT is required")
    root = Path(configured)
    return root / session_id / _safe_id(run_id, label="compiler run id")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    from app.state.persistence import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path.name)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid object in {path.name}")
    return value


def _last_event_seq(path: Path) -> int:
    if not path.is_file():
        return 0
    # ponytail: linear scan; add an index only if real child traces make this slow.
    last = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            last = max(last, int(json.loads(line).get("event_seq") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return last


def _resume_block(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        pause = event.get("payload", {}).get("pause") if event.get("kind") == "child_paused" else None
        return pause if isinstance(pause, dict) and pause.get("status") == "frontier_rolled_back" else None
    return None


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        stream.flush()


def _checkpoint_summary(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    checkpoint = checkpoint or {}
    return {
        "revision": int(checkpoint.get("revision") or 0),
        "checkpoint_status": str(checkpoint.get("status") or "missing"),
        "active_check_id": str(checkpoint.get("active_check_id") or ""),
        "completed_check_ids": list(checkpoint.get("completed_check_ids") or []),
    }


def _proof_snapshot(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    if not checkpoint or "artifact" not in checkpoint or "proof" not in checkpoint:
        return _checkpoint_summary(checkpoint)
    from app.compiler_runtime.runtime import CompilerRunCheckpoint
    from app.compiler_runtime.snapshot import compiler_snapshot

    return compiler_snapshot(CompilerRunCheckpoint.model_validate(checkpoint))


def _result(details: dict[str, Any]) -> Any:
    from tau_agent.messages import TextContent
    from tau_agent.tools import AgentToolResult

    visible = {
        key: details[key]
        for key in (
            "status",
            "compiler_run_id",
            "revision",
            "checkpoint_status",
            "active_check_id",
            "completed_check_ids",
            "total_checks",
            "checks",
            "decisions",
            "diagnostics",
            "proof_terms",
            "lineage",
            "pause",
            "event",
            "events",
            "next_event_seq",
            "next_action",
            "error",
        )
        if key in details
    }
    return AgentToolResult(
        content=[TextContent(text=json.dumps(visible, ensure_ascii=False, sort_keys=True))],
        details=details,
    )


def setup(tau: Any) -> None:
    """Register the minimal start/resume and inspect tools."""

    from tau_agent.tools import AgentTool

    async def evidence_reviewer(
        tool_call_id: str,
        arguments: Mapping[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> Any:
        del tool_call_id
        requested_run_id = str(arguments.get("compiler_run_id") or "").strip()
        run_id = _safe_id(requested_run_id, label="compiler run id") if requested_run_id else f"compiler_{uuid4().hex[:12]}"
        directory = _run_dir(tau, run_id)
        request_path = directory / "request.json"
        checkpoint_path = directory / "checkpoint.json"
        event_path = directory / "events.jsonl"

        if requested_run_id:
            request = _read_json(request_path)
            checkpoint = _read_json(checkpoint_path) if checkpoint_path.is_file() else None
            if checkpoint and checkpoint.get("status") in {"completed", "cancelled"}:
                return _result(
                    {
                        "status": "not_resumable",
                        "compiler_run_id": run_id,
                        **_checkpoint_summary(checkpoint),
                        "next_action": "inspect_evidence_review",
                    }
                )
            if (pause := _resume_block(event_path)) is not None:
                return _result(
                    {
                        "status": "requires_revision",
                        "compiler_run_id": run_id,
                        **_checkpoint_summary(checkpoint),
                        "pause": pause,
                        "next_action": "recheck_evidence_review",
                    }
                )
        else:
            task_objective = str(arguments.get("task_objective") or "").strip()
            proposal_ref = str(arguments.get("proposal_ref") or "").strip()
            source_refs = list(
                dict.fromkeys(
                    str(item).strip()
                    for item in arguments.get("source_refs") or []
                    if str(item).strip()
                )
            )
            if not task_objective or not proposal_ref or not source_refs:
                raise ValueError(
                    "A new ERP review requires task_objective, proposal_ref, and source_refs"
                )
            from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK
            from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog

            pack = EVIDENCE_ACTION_REVIEW_PACK
            active_requirement_ids = ["erp_action_plan_valid"]
            action_proposal = _resolve_action_proposal(proposal_ref)
            request = {
                "task_objective": task_objective,
                "requirement_pack_id": pack.pack_id,
                "requirement_pack_version": pack.version,
                "requirement_pack_hash": pack.content_hash,
                "active_requirement_ids": active_requirement_ids,
                "sources": _resolve_sources(source_refs),
                "catalog": load_proof_catalog(),
                "policy_excerpt": pack.policy_excerpt_for(active_requirement_ids),
                "requirement_requiredness": {"erp_action_plan_valid": True},
                "action_proposal": action_proposal.model_dump(mode="json"),
            }
            checkpoint = None
            _write_json(request_path, request)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        next_seq = _last_event_seq(event_path)

        def queue_event(kind: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
            nonlocal next_seq
            next_seq += 1
            event = {
                "event_seq": next_seq,
                "compiler_run_id": run_id,
                "kind": kind,
                "summary": summary,
                "payload": payload,
            }
            _append_event(event_path, event)
            loop.call_soon_threadsafe(queue.put_nowait, event)
            return event

        def checkpoint_sink(payload: dict[str, Any]) -> None:
            if str(payload.get("compiler_run_id") or "") != run_id:
                raise ValueError("Compiler checkpoint run id changed")
            _write_json(checkpoint_path, payload)
            queue_event(
                "checkpoint_saved",
                _checkpoint_summary(payload),
                "Evidence Compiler checkpoint saved.",
            )

        def progress_sink(kind: str, payload: dict[str, Any], summary: str) -> bool:
            queue_event(kind, payload, summary)
            cancelled = bool(signal is not None and signal.is_cancelled())
            return cancelled or str(payload.get("status") or "") in {
                "plan_ready",
                "frontier_rolled_back",
            }

        async def publish(event: dict[str, Any]) -> None:
            await tau.append_entry("evidence_compiler", event)
            if on_update is not None:
                on_update(
                    _result(
                        {
                            "status": "running",
                            "compiler_run_id": run_id,
                            "event_seq": event["event_seq"],
                            "event": event,
                            "next_action": "wait_for_child_checkpoint",
                        }
                    )
                )

        child = asyncio.create_task(
            asyncio.to_thread(
                _run_compiler,
                request,
                checkpoint,
                run_id,
                directory,
                checkpoint_sink,
                progress_sink,
            )
        )
        while not child.done():
            try:
                await publish(await asyncio.wait_for(queue.get(), timeout=0.05))
            except TimeoutError:
                continue
        while not queue.empty():
            await publish(queue.get_nowait())

        try:
            outcome = await child
            checkpoint = _read_json(checkpoint_path)
            details = {
                "status": str(outcome.get("status") or "completed"),
                "compiler_run_id": run_id,
                **_proof_snapshot(checkpoint),
                "next_action": "inspect_evidence_review",
            }
        except Exception as exc:
            checkpoint = _read_json(checkpoint_path) if checkpoint_path.is_file() else None
            pause = getattr(exc, "payload", None)
            details = {
                "status": "paused" if isinstance(pause, Mapping) else "error",
                "compiler_run_id": run_id,
                **_checkpoint_summary(checkpoint),
                "next_action": "inspect_evidence_review",
            }
            if isinstance(pause, Mapping):
                details["pause"] = dict(pause)
                details["next_action"] = (
                    "recheck_evidence_review"
                    if pause.get("status") == "frontier_rolled_back"
                    else "inspect_evidence_review"
                )
            else:
                details["error"] = {"type": type(exc).__name__, "message": str(exc)}
            event = queue_event(
                "child_paused" if isinstance(pause, Mapping) else "child_error",
                {key: value for key, value in details.items() if key != "review_result"},
                "Evidence Compiler paused at a durable boundary."
                if isinstance(pause, Mapping)
                else "Evidence Compiler failed; the latest checkpoint remains resumable.",
            )
            await publish(event)
        return _result(details)

    async def inspect_evidence_review(
        tool_call_id: str,
        arguments: Mapping[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> Any:
        del tool_call_id, signal, on_update
        run_id = _safe_id(str(arguments.get("compiler_run_id") or ""), label="compiler run id")
        directory = _run_dir(tau, run_id)
        checkpoint = _read_json(directory / "checkpoint.json") if (directory / "checkpoint.json").is_file() else None
        after = max(0, int(arguments.get("after_event_seq") or 0))
        events = []
        event_path = directory / "events.jsonl"
        if event_path.is_file():
            for line in event_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(event.get("event_seq") or 0) > after:
                    events.append(event)
        details = {
            "status": "found" if checkpoint is not None else "checkpoint_missing",
            "compiler_run_id": run_id,
            **_proof_snapshot(checkpoint),
            "events": events[-12:],
            "next_event_seq": max(
                [after, *(int(item.get("event_seq") or 0) for item in events)]
            ),
            "next_action": (
                "recheck_evidence_review"
                if _resume_block(event_path) is not None
                else "evidence_reviewer"
                if checkpoint is not None and checkpoint.get("status") == "running"
                else "stop"
            ),
        }
        return _result(details)

    async def recheck_evidence_review(
        tool_call_id: str,
        arguments: Mapping[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> Any:
        del tool_call_id, signal, on_update
        run_id = _safe_id(str(arguments.get("compiler_run_id") or ""), label="compiler run id")
        directory = _run_dir(tau, run_id)
        checkpoint_path = directory / "checkpoint.json"
        checkpoint = _recheck_compiler(
            _read_json(checkpoint_path),
            correction_id=_safe_id(
                str(arguments.get("correction_id") or ""),
                label="correction id",
            ),
            expected_revision=int(arguments.get("expected_revision") or 0),
            check_id=str(arguments.get("check_id") or "").strip(),
            message=str(arguments.get("message") or "").strip(),
        )
        _write_json(checkpoint_path, checkpoint)
        event_path = directory / "events.jsonl"
        event = {
            "event_seq": _last_event_seq(event_path) + 1,
            "compiler_run_id": run_id,
            "kind": "checkpoint_revised",
            "summary": "Manager requested a bounded CHECK replay.",
            "payload": {
                **_checkpoint_summary(checkpoint),
                "check_id": str(arguments.get("check_id") or "").strip(),
                "correction_id": str(arguments.get("correction_id") or "").strip(),
            },
        }
        _append_event(event_path, event)
        await tau.append_entry("evidence_compiler", event)
        return _result(
            {
                "status": "revision_ready",
                "compiler_run_id": run_id,
                **_checkpoint_summary(checkpoint),
                "resume_input": {"compiler_run_id": run_id},
                "next_action": "evidence_reviewer",
            }
        )

    tau.register_tool(
        AgentTool(
            name="evidence_reviewer",
            label="Evidence reviewer",
            description=(
                "Start a durable evidence review, or resume the exact saved child by compiler_run_id. "
                "For a new ERP run pass task_objective, proposal_ref, and admitted source_refs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "compiler_run_id": {"type": "string"},
                    "task_objective": {"type": "string"},
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "proposal_ref": {"type": "string"},
                },
                "additionalProperties": False,
            },
            execute_fn=evidence_reviewer,
            execution_mode="sequential",
            prompt_snippet=(
                "For high-risk ERP actions pass only task_objective, proposal_ref, and source_refs. "
                "Resume the same compiler_run_id after PLAN_READY; "
                "after CHECK_RETRY_EXHAUSTED inspect and revise it before resuming. Never restart with fresh sources."
            ),
        )
    )
    tau.register_tool(
        AgentTool(
            name="inspect_evidence_review",
            label="Inspect evidence review",
            description="Read the latest durable Compiler checkpoint and ordered child events.",
            parameters={
                "type": "object",
                "required": ["compiler_run_id"],
                "properties": {
                    "compiler_run_id": {"type": "string"},
                    "after_event_seq": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            execute_fn=inspect_evidence_review,
            execution_mode="sequential",
            prompt_snippet=(
                "Use after every Compiler pause or completion to inspect the saved Proof Plan, CHECK states, "
                "DecisionProof results, diagnostics, lineage, and ordered child events before acting."
            ),
        )
    )
    tau.register_tool(
        AgentTool(
            name="recheck_evidence_review",
            label="Recheck evidence review",
            description=(
                "Create one idempotent checkpoint revision that replays a named CHECK and its downstream CHECKs."
            ),
            parameters={
                "type": "object",
                "required": [
                    "compiler_run_id",
                    "correction_id",
                    "expected_revision",
                    "check_id",
                    "message",
                ],
                "properties": {
                    "compiler_run_id": {"type": "string"},
                    "correction_id": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "check_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
            execute_fn=recheck_evidence_review,
            execution_mode="sequential",
            prompt_snippet=(
                "Use only after inspecting the exact run and revision; then resume the returned compiler_run_id."
            ),
        )
    )
    tau.add_prompt_guideline(
        "Treat evidence_reviewer as a durable child: inspect every pause; resume PLAN_READY unchanged, "
        "but never resume CHECK_RETRY_EXHAUSTED without a checkpoint revision."
    )
