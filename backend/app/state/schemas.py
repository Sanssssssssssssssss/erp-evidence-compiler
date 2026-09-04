from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.compiler_runtime.models import (
    CompiledProof as RuntimeCompiledProof,
    ReviewArtifact,
)
from app.domain.invoice_requirements import CORE_REQUIREMENTS, DEFAULT_REQUIREMENT_LABELS


CaseStatus = Literal["new", "collecting_materials", "ready_for_report", "report_generated"]
RequirementStatus = Literal["missing", "submitted", "accepted", "weak", "rejected", "conflict", "satisfied"]
RequirementId = str
FieldStatus = Literal["present", "missing", "conflict", "unclear"]
EvidenceType = Literal[
    "invoice",
    "purchase_order",
    "goods_receipt",
    "vendor_record",
    "duplicate_payment_check",
    "process_log",
    "clear_invoice_event",
    "payment_terms",
    "policy_excerpt",
    "bpi_event_log",
    "user_statement",
    "unknown",
]
Credibility = Literal["low", "medium", "high"]
EvidenceSource = Literal["user_message", "attachment", "rag"]
PatchType = Literal["create_case", "add_evidence", "update_case", "no_change"]
SourceTraceability = Literal[
    "original_document",
    "system_export",
    "log_excerpt",
    "user_statement",
    "rag_guidance",
    "unclear",
]
RoleName = Literal["materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"]
ToolName = Literal[
    "read_case_state",
    "read_attachment",
    "list_case_files",
    "write_case_file",
    "render_pdf",
    "inspect_compiler_run",
    "recheck_compiler_check",
    "cancel_compiler_run",
]
SupervisorActionType = Literal[
    "delegate_agent",
    "call_tool",
    "write_case_patch",
    "final_answer",
    "ask_user",
]
SupervisorTarget = Literal[
    "",
    "materials_advisor",
    "evidence_reviewer",
    "case_patch_writer",
    "report_writer",
    "read_case_state",
    "read_attachment",
    "list_case_files",
    "write_case_file",
    "render_pdf",
    "write_case_patch",
    "inspect_compiler_run",
    "recheck_compiler_check",
    "cancel_compiler_run",
]


def _compact_planner_text(value: Any, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    suffix = "...[truncated]"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _compact_plan_progress(value: Any) -> str:
    text = " ".join(str(value or "").split())
    parts = [part.strip() for part in text.split(";") if part.strip()]
    structured = [
        part
        for part in parts
        if part.startswith(("intent=", "route=", "step=", "next=", "feedback="))
    ]
    if structured:
        return "; ".join(structured[:3])
    return _compact_planner_text(text, max_chars=140)


class Requirement(BaseModel):
    id: RequirementId
    label: str = ""
    status: RequirementStatus = "missing"
    evidence_ids: list[str] = Field(default_factory=list)
    kind: Literal["document", "field", "cross_check", "visual", "risk_check"] = "field"
    required: bool = True
    guidance: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: Any) -> Any:
        status = str(value or "").strip().lower()
        if status in {"", "pending", "new"}:
            return "missing"
        return value


class SupportRecord(BaseModel):
    requirement: RequirementId
    support_level: Literal["none", "partial", "full"] = "partial"
    quoted_text: str = ""


class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = ""
    status: FieldStatus = "unclear"
    source_quote: str = ""
    source_locator: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"

    @model_validator(mode="before")
    @classmethod
    def normalize_locator_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "locator" in value:
            data = dict(value)
            locator = data.pop("locator")
            data.setdefault("source_locator", locator)
            return data
        return value


class ConflictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "conflict"
    conflict_type: str = ""
    requirement: RequirementId | None = None
    severity: Literal["low", "medium", "high"] = "medium"
    field: str = ""
    description: str = ""
    details: str = ""
    quoted_text: str = ""
    conflict_with: str = ""
    compared_to: str = ""
    required_follow_up: str = ""
    affected_fields: list[str] = Field(default_factory=list)
    affected_evidence_ids: list[str] = Field(default_factory=list)
    involved_evidence_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_values: dict[str, Any] = Field(default_factory=dict)
    suggested_resolution: str = ""
    resolution_status: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_conflict(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{"):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    pass
            if isinstance(value, str):
                return {"description": text}
        if isinstance(value, dict):
            data = dict(value)
            if "reason" in data and "description" not in data:
                data["description"] = data.pop("reason")
            if isinstance(data.get("details"), dict):
                data["details"] = json.dumps(data["details"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if isinstance(data.get("quoted_text"), list):
                data["quoted_text"] = "\n".join(str(item) for item in data["quoted_text"] if str(item).strip())
            if isinstance(data.get("conflict_with"), list):
                data["conflict_with"] = ", ".join(str(item) for item in data["conflict_with"] if str(item).strip())
            source_values = data.get("source_values")
            if isinstance(source_values, list):
                data["source_values"] = {
                    str(item.get("source") or item.get("doc_id") or index): item.get("value", item)
                    for index, item in enumerate(source_values)
                    if isinstance(item, dict)
                }
            return data
        return value

    def __str__(self) -> str:
        return self.description or self.details or self.conflict_type or self.type


class EvidencePatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    type: EvidenceType | None = None
    evidence_type: EvidenceType | None = None
    reference: str | None = None
    credibility: Credibility = "medium"
    summary: str = ""
    source: EvidenceSource = "user_message"
    content: str = ""
    created_at: str = ""
    review_result: dict[str, Any] = Field(default_factory=dict)
    supports: list[SupportRecord] = Field(default_factory=list)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    quoted_text: list[str] = Field(default_factory=list)
    reviewer_notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    local_source_handle: str = ""

    @field_validator("quoted_text", mode="before")
    @classmethod
    def normalize_string_lists(cls, value: Any) -> Any:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in items:
            if isinstance(item, str):
                text = item.strip()
            else:
                text = json.dumps(item, ensure_ascii=False, default=str)
            if text:
                result.append(text)
        return result


class EvidenceItem(BaseModel):
    id: str
    type: EvidenceType
    credibility: Credibility = "medium"
    summary: str = ""
    source: EvidenceSource = "user_message"
    content: str = ""
    created_at: str = ""
    review_result: dict[str, Any] = Field(default_factory=dict)
    supports: list[SupportRecord] = Field(default_factory=list)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    quoted_text: list[str] = Field(default_factory=list)
    reviewer_notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    local_source_handle: str = ""


class CaseUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    conversation_summary: str | None = None
    case_profile: dict[str, Any] | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    remove_requirements: list[str] = Field(default_factory=list)
    add_evidence: list[EvidencePatchItem] = Field(default_factory=list)
    evidence_items: list[EvidencePatchItem] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)
    next_action_hint: str | None = None
    reply_brief: str | None = None
    evidence_cards: list[dict[str, Any]] | None = None

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_requirements(cls, value: Any) -> Any:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: list[Any] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"id": item, "label": item.replace("_", " ")})
                continue
            if isinstance(item, dict):
                data = dict(item)
                if "id" not in data and "requirement_id" in data:
                    data["id"] = data.get("requirement_id")
                if "label" not in data and "name" in data:
                    data["label"] = data.get("name")
                if "label" not in data and "description" in data:
                    data["label"] = data.get("description")
                normalized.append(data)
                continue
            normalized.append(item)
        return normalized


class CaseState(BaseModel):
    case_id: str
    case_type: str = "invoice_payment_review"
    status: CaseStatus = "new"
    summary: str = ""
    case_profile: dict[str, Any] = Field(default_factory=dict)
    requirements: list[Requirement] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    evidence_cards: list[dict[str, Any]] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    missing_materials: list[str] = Field(default_factory=list)
    weak_materials: list[str] = Field(default_factory=list)
    conflict_materials: list[str] = Field(default_factory=list)
    satisfied_materials: list[str] = Field(default_factory=list)
    conversation_summary: str = ""
    next_questions: list[str] = Field(default_factory=list)
    next_action_hint: str = ""
    reply_brief: str = ""
    review_artifact: ReviewArtifact | None = None
    compiled_proof: RuntimeCompiledProof | None = None


class Attachment(BaseModel):
    name: str = ""
    path: str = ""
    content_type: str = ""


def timestamp_case_id() -> str:
    return f"case_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


class AgentTurnRequest(BaseModel):
    case_id: str = Field(default_factory=timestamp_case_id)
    message: str
    attachments: list[Attachment] = Field(default_factory=list)


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: SupervisorActionType
    target: SupervisorTarget = ""
    input: dict[str, Any] = Field(default_factory=dict)
    case_patch: dict[str, Any] | None = None
    final_answer: str | None = None
    short_plan: list[str] = Field(default_factory=list)
    plan_progress: str = ""
    reason: str = ""
    confidence: float = 0.5

    @field_validator("short_plan", mode="before")
    @classmethod
    def normalize_short_plan(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        compacted = [_compact_planner_text(item, max_chars=36) for item in items[:3]]
        return [item for item in compacted if item]

    @field_validator("plan_progress", mode="before")
    @classmethod
    def normalize_plan_progress(cls, value: Any) -> str:
        return _compact_plan_progress(value)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: Any) -> str:
        return _compact_planner_text(value, max_chars=160)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, number))

    @model_validator(mode="after")
    def validate_target_contract(self) -> "SupervisorDecision":
        if self.action == "call_tool" and self.target == "write_case_patch":
            raise ValueError("write_case_patch must use action='write_case_patch', not action='call_tool'")
        if self.action == "delegate_agent" and self.target not in {"materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"}:
            raise ValueError("delegate_agent requires a specialist target")
        if self.action == "call_tool" and self.target not in {
            "read_case_state",
            "read_attachment",
            "list_case_files",
            "write_case_file",
            "render_pdf",
            "inspect_compiler_run",
            "recheck_compiler_check",
            "cancel_compiler_run",
        }:
            raise ValueError("call_tool requires a tool target")
        if self.action in {"final_answer", "ask_user"} and not str(self.final_answer or "").strip():
            raise ValueError("final_answer and ask_user require non-empty final_answer")
        return self


class PolicyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool = True
    error_type: str = ""
    feedback_to_supervisor: str = ""
    recommended_constraints: list[str] = Field(default_factory=list)
    risk_level: Literal["read", "extract", "local_write", "external_write", "destructive", "privileged"] = "read"
    requires_approval: bool = False
    approval_reason: str = ""
    approval_payload: dict[str, Any] = Field(default_factory=dict)


class EvidenceReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["review"] = "review"
    compile_status: Literal["COMMITTED", "NON_CONVERGED", "INVALID", "CANCELLED"] | None = None
    semantic_status: Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"] | None = None
    source_doc_id: str = ""
    evidence_type: EvidenceType = "unknown"
    credibility: Credibility = "medium"
    extracted_fields: dict[str, ExtractedField] = Field(default_factory=dict)
    extraction_result: dict[str, Any] = Field(default_factory=dict)
    source_traceability: SourceTraceability = "unclear"
    support_level: Literal["none", "partial", "full"] = "none"
    risk_flags: list[str] = Field(default_factory=list)
    should_accept: bool = False
    reason: str = ""
    supports: list[SupportRecord] = Field(default_factory=list)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    evidence_cards: list[dict[str, Any]] = Field(default_factory=list)
    suggested_patch: CaseUpdates = Field(default_factory=CaseUpdates)
    reply_to_user: str = ""

    @field_validator("evidence_type", mode="before")
    @classmethod
    def normalize_multi_document_type(cls, value: Any) -> Any:
        if str(value or "").strip().lower() in {
            "mixed_document_batch",
            "multi_document_review",
            "multi_document_packet",
        }:
            return "unknown"
        return value

    @model_validator(mode="after")
    def require_review_cards_for_new_evidence(self) -> "EvidenceReviewResult":
        if self.mode == "review":
            adds = list(self.suggested_patch.add_evidence or []) + list(self.suggested_patch.evidence_items or [])
            patch_cards = self.suggested_patch.evidence_cards or []
            if adds and not self.evidence_cards and not patch_cards:
                cards = _evidence_cards_from_patch_items(adds)
                if not cards:
                    raise ValueError("mode=review with add_evidence must include evidence_cards")
                self.evidence_cards = cards
                self.suggested_patch.evidence_cards = cards
        return self

    @field_validator("extracted_fields", mode="before")
    @classmethod
    def normalize_extracted_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if _looks_like_field_group(item):
                for child_key, child_item in item.items():
                    if isinstance(child_item, dict):
                        normalized[str(child_key)] = child_item
                    else:
                        normalized[str(child_key)] = {"value": child_item, "status": "present", "source_quote": ""}
                continue
            if isinstance(item, dict):
                normalized[str(key)] = item
            else:
                normalized[str(key)] = {"value": item, "status": "present", "source_quote": ""}
        return normalized

    @field_validator("conflicts", mode="before")
    @classmethod
    def normalize_conflicts(cls, value: Any) -> Any:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: list[Any] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"description": item})
            else:
                normalized.append(item)
        return normalized


class MaterialTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = ""
    requirement: str = ""
    current_status: str = ""
    why_insufficient: str = ""
    owner: str = ""
    format: str = ""
    required_fields: list[str] = Field(default_factory=list)
    impact: str = ""


class MaterialsAdvisorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = ""
    tasks: list[MaterialTask] = Field(default_factory=list)
    missing_materials: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)


class ReportWriterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = ""
    title: str = "final_report"


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch_type: PatchType = "no_change"
    case_updates: CaseUpdates = Field(default_factory=CaseUpdates)
    audit_note: str = ""


class RagEvidence(BaseModel):
    source_id: str
    source_path: str
    source_type: str = "unknown"
    locator: str = ""
    snippet: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    channel: str = "bm25"


class RagResult(BaseModel):
    status: Literal["success", "partial", "not_found"] = "not_found"
    answer_context: str = ""
    evidences: list[RagEvidence] = Field(default_factory=list)
    unsupported_fields: list[str] = Field(default_factory=list)
    reason: str = ""


class AgentTurnResponse(BaseModel):
    case_id: str
    reply: str
    case_state: CaseState
    trace: dict[str, Any] = Field(default_factory=dict)


def default_requirements() -> list[Requirement]:
    return [
        Requirement(id=requirement_id, label=DEFAULT_REQUIREMENT_LABELS[requirement_id])
        for requirement_id in CORE_REQUIREMENTS
    ]


def new_case_state(case_id: str) -> CaseState:
    return CaseState(case_id=case_id, requirements=[], missing_materials=[])


def _evidence_cards_from_patch_items(items: list[EvidencePatchItem]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for item in items:
        data = item.model_dump(mode="json", exclude_none=True)
        doc_type = str(data.get("type") or data.get("evidence_type") or "unknown")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        reference = str(data.get("reference") or metadata.get("document_id") or metadata.get("original_ref") or "").strip()
        summary = _compact_planner_text(data.get("summary") or data.get("content") or "", max_chars=260)
        supports = [
            str(row.get("requirement") or "")
            for row in data.get("supports") or []
            if isinstance(row, dict) and str(row.get("requirement") or "").strip()
        ]
        conflicts = [_conflict_summary(row) for row in data.get("conflicts") or [] if _conflict_summary(row)]
        title_parts = [doc_type.replace("_", " ").strip() or "evidence"]
        if reference:
            title_parts.append(reference)
        card = {
            "title": " - ".join(title_parts),
            "doc_type": doc_type,
            "extracted_summary": summary or f"{doc_type.replace('_', ' ')} evidence captured.",
            "supports": supports[:8],
            "conflicts": conflicts[:5],
            "source_ref": str(metadata.get("original_ref") or metadata.get("dossier_ref") or ""),
            "visual_summary": _compact_planner_text(
                metadata.get("visual_summary") or metadata.get("source_traceability") or "",
                max_chars=220,
            ),
        }
        cards.append({key: value for key, value in card.items() if value not in ("", [], {}, None)})
    return cards


def _looks_like_field_group(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    field_keys = {"value", "status", "source_quote", "source_locator", "locator", "confidence"}
    if field_keys.intersection(value):
        return False
    nested = [item for item in value.values() if isinstance(item, dict)]
    if not nested:
        return False
    return all(field_keys.intersection(item) for item in nested)


def _conflict_summary(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("description") or value.get("details") or value.get("conflict_type") or "")[:180]
    return str(value).strip()[:180]
