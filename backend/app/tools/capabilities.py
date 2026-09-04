from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.checkpoints import RuntimeCheckpointStore
from app.state.persistence import PERSISTENCE_LOCK
from app.state.schemas import Attachment
from app.tools.file_workspace import FileWorkspace, report_paths_for_run


SideEffect = Literal["none", "case_read", "case_write", "report_write", "file_extract"]
Idempotency = Literal["safe", "idempotent", "side_effectful"]
ContextExposure = Literal["summary_only", "artifact_ref", "full_result_allowed"]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadCaseStateInput(ToolInput):
    pass


class ListCaseFilesInput(ToolInput):
    pass


class ReadAttachmentInput(ToolInput):
    name: str = ""
    path: str = ""
    attachment_id: str = ""
    original_ref: str = ""
    max_chars: int = 12000


class WriteCasePatchInput(ToolInput):
    patch: dict[str, Any] = Field(default_factory=dict)


class WriteCaseFileInput(ToolInput):
    relative_path: str = ""
    content: str = ""
    content_ref: str = ""


class RenderPdfInput(ToolInput):
    markdown_path: str = ""
    relative_path: str = ""
    pdf_path: str | None = None


class InspectCompilerRunInput(ToolInput):
    compiler_run_id: str = ""
    after_case_seq: int | None = Field(
        default=None,
        ge=0,
        description="Optional case event cursor. Pass next_case_seq from the previous inspection to receive only newer child events.",
    )


class RecheckCompilerCheckInput(ToolInput):
    compiler_run_id: str = Field(
        default="",
        description="Exact child compiler_run_id returned by inspect_compiler_run; leave blank for the latest child. Never pass a parent run_id.",
    )
    correction_id: str = Field(
        min_length=1,
        max_length=96,
        description="Stable idempotency key for this exact correction action; reuse it when retrying the same action.",
    )
    expected_revision: int = Field(
        ge=1,
        description="Revision returned by inspect_compiler_run; stale revisions are rejected.",
    )
    check_id: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)


class CancelCompilerRunInput(ToolInput):
    compiler_run_id: str = Field(
        default="",
        description="Exact child compiler_run_id returned by inspect_compiler_run; leave blank for the latest child. Never pass a parent run_id.",
    )
    message: str = ""


@dataclass(frozen=True)
class ToolCallContext:
    workspace: FileWorkspace
    case_id: str
    attachments: list[Attachment]
    run_state: Any | None = None
    content_resolver: Any | None = None


ToolHandler = Callable[[ToolCallContext, ToolInput], dict[str, Any]]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    input_model: type[ToolInput]
    handler: ToolHandler
    side_effect: SideEffect
    idempotency: Idempotency
    error_taxonomy: tuple[str, ...]
    context_exposure: ContextExposure
    precondition: str = ""
    postcondition: str = ""
    required_result_keys: tuple[str, ...] = ()

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "side_effect": self.side_effect,
            "idempotency": self.idempotency,
            "context_exposure": self.context_exposure,
            "error_taxonomy": list(self.error_taxonomy),
            "precondition": self.precondition,
            "postcondition": self.postcondition,
        }


def _read_case_state(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    _ = payload
    return context.workspace.read_case_state(context.case_id)


def _read_attachment(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, ReadAttachmentInput)
    return context.workspace.read_attachment(
        context.case_id,
        context.attachments,
        name=data.name,
        path=data.path,
        attachment_id=data.attachment_id,
        original_ref=data.original_ref,
        max_chars=data.max_chars,
        session_id=str(getattr(context.run_state, "session_id", "") or ""),
        turn_id=str(getattr(context.run_state, "turn_id", "") or ""),
        run_id=str(getattr(context.run_state, "run_id", "") or ""),
    )


def _write_case_patch(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, WriteCasePatchInput)
    observability = getattr(context.run_state, "observability", {})
    artifact = observability.get("_pending_review_artifact") if isinstance(observability, dict) else None
    return context.workspace.write_case_patch(
        context.case_id,
        data.patch,
        review_artifact=artifact if isinstance(artifact, dict) else None,
    )


def _list_case_files(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    _ = payload
    return context.workspace.list_case_files(context.case_id)


def _write_case_file(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, WriteCaseFileInput)
    relative_path = data.relative_path
    content = data.content
    if data.content_ref.startswith("last_role:report_writer") or _looks_like_report_path(relative_path):
        relative_path, _ = report_paths_for_run(_started_at(context.run_state))
    if data.content_ref:
        if not context.content_resolver or context.run_state is None:
            raise ValueError("content_ref requires a content resolver and run_state")
        content = context.content_resolver(context.case_id, context.run_state, data.content_ref)
    return context.workspace.write_case_file(context.case_id, relative_path, content)


def _render_pdf(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, RenderPdfInput)
    markdown_path = data.markdown_path or data.relative_path
    pdf_path = data.pdf_path
    if _looks_like_report_path(markdown_path) or _looks_like_report_path(str(pdf_path or "")):
        markdown_path, pdf_path = report_paths_for_run(_started_at(context.run_state))
    return context.workspace.render_pdf(context.case_id, markdown_path, pdf_path)


def _inspect_compiler_run(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, InspectCompilerRunInput)
    parent_run_id, checkpoint = _load_compiler(context, data.compiler_run_id)
    return _compiler_snapshot(
        context,
        parent_run_id,
        checkpoint,
        after_case_seq=data.after_case_seq,
    )


def _recheck_compiler_check(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    from app.compiler_runtime.runtime import CompilerCorrection, revise_compiler_checkpoint

    data = _as(payload, RecheckCompilerCheckInput)
    correction = CompilerCorrection(
        correction_id=data.correction_id,
        kind="RECHECK",
        target_check_id=data.check_id,
        message=data.message,
        evidence_refs=data.evidence_refs,
    )
    with PERSISTENCE_LOCK:
        parent_run_id, checkpoint = _load_compiler(context, data.compiler_run_id)
        existing = next(
            (item for item in checkpoint.corrections if item.correction_id == correction.correction_id),
            None,
        )
        if existing is not None:
            if existing != correction:
                raise ValueError(f"Correction id {correction.correction_id!r} was reused with different input")
            revised = checkpoint
        else:
            if checkpoint.revision != data.expected_revision:
                raise ValueError(
                    f"Stale compiler revision: expected {data.expected_revision}, current {checkpoint.revision}"
                )
            _validate_evidence_refs(checkpoint, data.evidence_refs)
            revised = revise_compiler_checkpoint(checkpoint, correction)
            RuntimeCheckpointStore(context.workspace.store).save_compiler(
                case_id=context.case_id,
                run_id=parent_run_id,
                compiler_run_id=revised.compiler_run_id,
                payload=revised.model_dump(mode="json"),
            )
        observability = getattr(context.run_state, "observability", None)
        if isinstance(observability, dict):
            observability.pop("_pending_review_artifact", None)
    result = _compiler_snapshot(context, parent_run_id, revised)
    result.update(
        {
            "status": "revision_exists" if existing is not None else "revision_created",
            "next_action": "evidence_reviewer" if revised.status == "running" else "inspect_compiler_run",
            "resume_input": {"compiler_run_id": revised.compiler_run_id},
        }
    )
    return result


def _validate_evidence_refs(checkpoint: Any, evidence_refs: list[str]) -> None:
    admitted: set[str] = set()
    ref_keys = {"source_id", "attachment_id", "original_ref", "extraction_ref", "source_doc_id"}
    for source in checkpoint.source_snapshot:
        for values in (source, source.get("provenance") or {}, source.get("metadata") or {}):
            admitted.update(
                str(value)
                for key, value in values.items()
                if key in ref_keys and isinstance(value, str) and value
            )
    unknown = sorted(set(evidence_refs) - admitted)
    if unknown:
        raise ValueError(f"Evidence refs are not admitted in this compiler run: {unknown}")


def _cancel_compiler_run(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    from app.compiler_runtime.runtime import CompilerCorrection, revise_compiler_checkpoint

    data = _as(payload, CancelCompilerRunInput)
    with PERSISTENCE_LOCK:
        parent_run_id, checkpoint = _load_compiler(context, data.compiler_run_id)
        cancelled = revise_compiler_checkpoint(
            checkpoint,
            CompilerCorrection(kind="CANCEL", message=data.message),
        )
        RuntimeCheckpointStore(context.workspace.store).save_compiler(
            case_id=context.case_id,
            run_id=parent_run_id,
            compiler_run_id=cancelled.compiler_run_id,
            payload=cancelled.model_dump(mode="json"),
        )
    observability = getattr(context.run_state, "observability", None)
    if isinstance(observability, dict):
        observability.pop("_pending_review_artifact", None)
    result = _compiler_snapshot(context, parent_run_id, cancelled)
    result["status"] = "cancelled"
    return result


def _load_compiler(
    context: ToolCallContext,
    compiler_run_id: str,
) -> tuple[str, Any]:
    from app.compiler_runtime.runtime import CompilerRunCheckpoint

    parent_run_id, payload = RuntimeCheckpointStore(context.workspace.store).latest_compiler(
        context.case_id,
        compiler_run_id.strip(),
    )
    return parent_run_id, CompilerRunCheckpoint.model_validate(payload)


def _compiler_snapshot(
    context: ToolCallContext,
    parent_run_id: str,
    checkpoint: Any,
    *,
    after_case_seq: int | None = None,
) -> dict[str, Any]:
    from app.compiler_runtime.snapshot import compiler_snapshot

    recent_events = _compiler_events(
        context,
        parent_run_id,
        checkpoint.compiler_run_id,
        after_case_seq=after_case_seq,
    )
    return {
        "case_id": context.case_id,
        **compiler_snapshot(checkpoint),
        "status": checkpoint.status,
        "completed_checks": len(checkpoint.completed_check_ids),
        "recent_events": recent_events,
        "next_case_seq": max(
            [after_case_seq or 0, *(int(item["case_seq"]) for item in recent_events)]
        ),
    }


def _compiler_events(
    context: ToolCallContext,
    parent_run_id: str,
    compiler_run_id: str,
    *,
    after_case_seq: int | None = None,
) -> list[dict[str, Any]]:
    path = context.workspace.store.resolve_case_path(context.case_id, "traces/events.jsonl")
    if not path.exists():
        return []
    # The Manager preview preserves at most 12 list items; keep cursor pages lossless.
    selected: deque[dict[str, Any]] = deque(maxlen=12)
    compiler_stages = {"task_compiler", "executor", "fine_verifier", "proof_kernel"}
    # ponytail: case-local linear scan; add an event index only if trace size proves this slow.
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            case_seq = int(event.get("case_seq") or 0)
            if after_case_seq is not None and case_seq <= after_case_seq:
                continue
            stage = str(payload.get("stage") or payload.get("role") or "")
            belongs = str(payload.get("compiler_run_id") or "") == compiler_run_id
            if not belongs and not (
                str(event.get("run_id") or "") == parent_run_id and stage in compiler_stages
            ):
                continue
            selected.append(
                {
                    "seq": int(event.get("run_seq") or event.get("seq") or 0),
                    "case_seq": case_seq,
                    "event_id": str(event.get("event_id") or ""),
                    "ts": str(event.get("ts") or ""),
                    "kind": str(event.get("kind") or ""),
                    "stage": stage,
                    "status": str(payload.get("status") or ""),
                    "check_id": str(payload.get("check_id") or ""),
                    "action": str(payload.get("action") or event.get("summary") or ""),
                    "public_reason": str(payload.get("public_reason") or ""),
                    "diagnostic_code": str(
                        payload.get("diagnostic_code") or payload.get("hook_code") or ""
                    ),
                }
            )
            if after_case_seq is not None and len(selected) == selected.maxlen:
                break
    return list(selected)


def _as(payload: ToolInput, model: type[ToolInput]) -> Any:
    if not isinstance(payload, model):
        raise TypeError(f"Expected {model.__name__}, got {type(payload).__name__}")
    return payload


def _started_at(run_state: Any | None) -> str:
    return str(getattr(run_state, "started_at", "") or "")


def _looks_like_report_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/").lower()
    if not text:
        return True
    name = text.rsplit("/", 1)[-1]
    if not text.startswith("reports/"):
        return False
    if name in {"final_report.md", "final_report.pdf", "report.md", "report.pdf", "manager_report.md", "manager_report.pdf"}:
        return True
    return name.startswith("final_report_") and (name.endswith(".md") or name.endswith(".pdf"))


TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "read_case_state": ToolCapability(
        name="read_case_state",
        input_model=ReadCaseStateInput,
        handler=_read_case_state,
        side_effect="case_read",
        idempotency="safe",
        error_taxonomy=("case_missing", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="case_id must resolve to the current workspace",
        postcondition="returns the current CaseState snapshot",
        required_result_keys=("case_id",),
    ),
    "read_attachment": ToolCapability(
        name="read_attachment",
        input_model=ReadAttachmentInput,
        handler=_read_attachment,
        side_effect="file_extract",
        idempotency="side_effectful",
        error_taxonomy=(
            "attachment_missing",
            "unsupported_attachment_type",
            "attachment_decode_error",
            "document_open_error",
            "ocr_unavailable",
            "unsafe_workspace_path",
        ),
        context_exposure="artifact_ref",
        precondition="attachment must be declared on this turn or resolvable from the case manifest",
        postcondition="stores original refs, extraction artifacts, previews, and manifest rows",
        required_result_keys=("case_id", "attachments", "attachment_manifest"),
    ),
    "write_case_patch": ToolCapability(
        name="write_case_patch",
        input_model=WriteCasePatchInput,
        handler=_write_case_patch,
        side_effect="case_write",
        idempotency="side_effectful",
        error_taxonomy=("patch_validation_error", "patch_input_too_large", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="must be invoked internally after a successful case_patch_writer observation",
        postcondition="persists a reduced CaseState update",
        required_result_keys=("case_id",),
    ),
    "list_case_files": ToolCapability(
        name="list_case_files",
        input_model=ListCaseFilesInput,
        handler=_list_case_files,
        side_effect="case_read",
        idempotency="safe",
        error_taxonomy=("case_missing", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="case_id must resolve to the current workspace",
        postcondition="returns relative case file paths",
        required_result_keys=("case_id", "files"),
    ),
    "write_case_file": ToolCapability(
        name="write_case_file",
        input_model=WriteCaseFileInput,
        handler=_write_case_file,
        side_effect="report_write",
        idempotency="side_effectful",
        error_taxonomy=("report_execution_wording", "unsafe_workspace_path", "content_ref_missing"),
        context_exposure="summary_only",
        precondition="relative_path must stay inside the case workspace; content_ref requires runtime resolver",
        postcondition="writes a case-local file and returns its relative path",
        required_result_keys=("case_id", "relative_path", "path"),
    ),
    "render_pdf": ToolCapability(
        name="render_pdf",
        input_model=RenderPdfInput,
        handler=_render_pdf,
        side_effect="report_write",
        idempotency="side_effectful",
        error_taxonomy=("render_source_missing", "pdf_render_error", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="markdown_path must point to a case-local Markdown report",
        postcondition="writes a case-local PDF and returns render metadata",
        required_result_keys=("case_id", "markdown_path", "pdf_path"),
    ),
    "inspect_compiler_run": ToolCapability(
        name="inspect_compiler_run",
        input_model=InspectCompilerRunInput,
        handler=_inspect_compiler_run,
        side_effect="case_read",
        idempotency="safe",
        error_taxonomy=("compiler_run_missing", "compiler_checkpoint_invalid"),
        context_exposure="summary_only",
        precondition="a compiler checkpoint must exist in the current case",
        postcondition="returns the latest child compiler_run_id, CHECK state, proof decisions, diagnostics, corrections, and bounded operational events without raw prompts or hidden reasoning",
        required_result_keys=("case_id", "compiler_run_id", "revision", "checks"),
    ),
    "recheck_compiler_check": ToolCapability(
        name="recheck_compiler_check",
        input_model=RecheckCompilerCheckInput,
        handler=_recheck_compiler_check,
        side_effect="case_write",
        idempotency="idempotent",
        error_taxonomy=(
            "compiler_run_missing",
            "compiler_checkpoint_invalid",
            "check_missing",
            "stale_revision",
            "evidence_ref_not_admitted",
        ),
        context_exposure="summary_only",
        precondition="the target CHECK must belong to the named compiler run; expected_revision must match the inspected revision",
        postcondition="creates a new revision for one CHECK in the named or latest child and returns the exact compiler_run_id that evidence_reviewer must resume; never use a parent run_id; it cannot set proof status",
        required_result_keys=("case_id", "compiler_run_id", "revision", "resume_input"),
    ),
    "cancel_compiler_run": ToolCapability(
        name="cancel_compiler_run",
        input_model=CancelCompilerRunInput,
        handler=_cancel_compiler_run,
        side_effect="case_write",
        idempotency="side_effectful",
        error_taxonomy=("compiler_run_missing", "compiler_checkpoint_invalid"),
        context_exposure="summary_only",
        precondition="the named compiler run must exist in the current case",
        postcondition="creates a cancelled revision without mutating committed case proof",
        required_result_keys=("case_id", "compiler_run_id", "revision", "status"),
    ),
}
