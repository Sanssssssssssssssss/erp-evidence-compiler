from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import CompiledProof, ReviewArtifact
from app.compiler_runtime.policy import (
    expand_active_requirements,
    policy_excerpt_for,
    policy_hash,
    case_decision_ready,
)
from app.domain.invoice_requirements import (
    AP_LITE_REQUIREMENTS,
    AP_THREE_WAY_REQUIREMENTS,
    AUTO_DERIVED_COMPILER_REQUIREMENTS,
    COMPILER_AUTHORITY_REQUIREMENTS,
    DYNAMIC_SUPPORT_REQUIREMENTS,
    INVOICE_FIELD_REQUIREMENTS,
    INVOICE_REQUIRED_FIELD_REQUIREMENTS,
    default_requirement_required,
    is_known_requirement,
    requirement_kind,
    requirement_label,
    requirement_owner,
    requirement_premises,
)
from app.state.attachment_manifest import link_manifest_evidence, trusted_sources_for_evidence
from app.state.persistence import PERSISTENCE_LOCK, append_text, atomic_write_text
from app.state.schemas import (
    CasePatch,
    CaseState,
    ConflictRecord,
    EvidenceItem,
    Requirement,
    new_case_state,
)


SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_REQUIREMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
REQUIREMENT_ID_ALIASES = {
    "grn": "goods_receipt_or_service_acceptance",
    "grn_supplier": "goods_receipt_or_service_acceptance",
    "grn_quantity": "goods_receipt_or_service_acceptance",
    "grn_amount": "goods_receipt_or_service_acceptance",
}
SUPPORT_REQUIREMENT_ID_ALIASES = {
    **REQUIREMENT_ID_ALIASES,
    "goods_receipt": "goods_receipt_or_service_acceptance",
    "service_acceptance": "goods_receipt_or_service_acceptance",
    "vendor_record": "vendor_identity",
    "supplier_record": "vendor_identity",
    "vendor_master": "vendor_identity",
    "duplicate_payment_check": "duplicate_payment_screen",
    "duplicate_check": "duplicate_payment_screen",
}
LEGACY_STORED_REQUIREMENT_ALIASES = {
    "goods_receipt": "goods_receipt_or_service_acceptance",
    "vendor_record": "vendor_identity",
    "duplicate_payment_check": "duplicate_payment_screen",
}
OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS = DYNAMIC_SUPPORT_REQUIREMENTS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileBoundaryError(ValueError):
    pass


class CaseStore:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = (workspace_root or get_settings().workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def validate_case_id(self, case_id: str) -> str:
        value = str(case_id or "").strip() or "case_001"
        if not SAFE_CASE_ID.match(value) or ".." in value or "/" in value or "\\" in value:
            raise FileBoundaryError(f"Unsafe case_id: {case_id}")
        return value

    def case_dir(self, case_id: str) -> Path:
        safe_id = self.validate_case_id(case_id)
        path = (self.workspace_root / safe_id).resolve()
        if self.workspace_root not in path.parents and path != self.workspace_root:
            raise FileBoundaryError("Case path escapes workspace root")
        return path

    def ensure_case_dirs(self, case_id: str) -> Path:
        root = self.case_dir(case_id)
        for name in ("attachments", "evidence", "reports", "traces"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def resolve_case_path(self, case_id: str, relative_path: str) -> Path:
        root = self.ensure_case_dirs(case_id)
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise FileBoundaryError("Path escapes case workspace")
        return candidate

    def validate_attachment_path(self, case_id: str, path: str) -> Path:
        root = self.ensure_case_dirs(case_id).resolve()
        raw = Path(path).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        if candidate != root and root not in candidate.parents:
            raise FileBoundaryError("Attachment path escapes case workspace")
        return candidate

    def load(self, case_id: str) -> CaseState:
        with PERSISTENCE_LOCK:
            return self._load(case_id)

    def _load(self, case_id: str) -> CaseState:
        root = self.ensure_case_dirs(case_id)
        path = root / "case_state.json"
        if not path.exists():
            state = new_case_state(self.validate_case_id(case_id))
            self.save(state)
            return state
        data = json.loads(path.read_text(encoding="utf-8"))
        migrated = _migrate_case_state_data(data)
        state = CaseState.model_validate(migrated)
        metadata_changed = _normalize_existing_evidence_metadata(state)
        cleanup_changed = _drop_legacy_page_edge_warnings(state)
        before_refresh = state.model_dump(mode="json")
        self._refresh_requirements(state)
        refresh_changed = state.model_dump(mode="json") != before_refresh
        buckets_changed = _refresh_material_buckets(state)
        if migrated != data or metadata_changed or cleanup_changed or refresh_changed or buckets_changed:
            self.save(state)
        return state

    def save(self, state: CaseState) -> None:
        root = self.ensure_case_dirs(state.case_id)
        atomic_write_text(root / "case_state.json", state.model_dump_json(indent=2))

    def next_trace_case_seq(self, case_id: str) -> int:
        with PERSISTENCE_LOCK:
            path = self.resolve_case_path(case_id, "traces/events.jsonl")
            if not path.exists():
                return 1
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1

    def apply_patch(self, case_id: str, patch_data: dict[str, Any] | CasePatch) -> CaseState:
        with PERSISTENCE_LOCK:
            return self._apply_patch(case_id, patch_data)

    def apply_review_patch(
        self,
        case_id: str,
        patch_data: dict[str, Any] | CasePatch,
        review_artifact: dict[str, Any] | ReviewArtifact,
    ) -> CaseState:
        """Atomically persist the evidence patch and its validated model work."""

        with PERSISTENCE_LOCK:
            artifact = (
                review_artifact
                if isinstance(review_artifact, ReviewArtifact)
                else ReviewArtifact.model_validate(review_artifact)
            )
            return self._apply_patch(case_id, patch_data, review_artifact=artifact)

    def _apply_patch(
        self,
        case_id: str,
        patch_data: dict[str, Any] | CasePatch,
        *,
        review_artifact: ReviewArtifact | None = None,
    ) -> CaseState:
        patch = patch_data if isinstance(patch_data, CasePatch) else CasePatch.model_validate(patch_data)
        state = self.load(case_id)
        updates = patch.case_updates.model_dump(exclude_none=True)
        if updates.get("summary") is not None:
            state.summary = str(updates["summary"] or "")
        if updates.get("conversation_summary") is not None:
            state.conversation_summary = str(updates["conversation_summary"] or "")
        if updates.get("case_profile") is not None:
            state.case_profile = _compact_jsonish_dict(updates.get("case_profile") or {}, max_chars=4000)
        if updates.get("next_action_hint") is not None:
            state.next_action_hint = str(updates.get("next_action_hint") or "")[:240]
        if updates.get("reply_brief") is not None:
            state.reply_brief = str(updates.get("reply_brief") or "")[:1200]
        if updates.get("evidence_cards") is not None:
            state.evidence_cards = _compact_jsonish_records(updates.get("evidence_cards") or [], limit=24, max_chars=1800)
        self._apply_requirement_updates(state, updates)
        _ensure_requirement_premises(state)
        if "risk_flags" in updates:
            state.risk_flags = _unique_strings(list(state.risk_flags) + list(updates.get("risk_flags") or []))
        if "next_questions" in updates:
            state.next_questions = _unique_strings(list(updates.get("next_questions") or []))
        if "evidence_items" in updates:
            self._append_evidence_updates(state, updates.get("evidence_items") or [])
        if "add_evidence" in updates:
            self._append_evidence_updates(state, updates.get("add_evidence") or [])
        if review_artifact is not None:
            state.review_artifact = review_artifact
        self._refresh_requirements(state)
        if review_artifact is not None and state.compiled_proof is None:
            raise ValueError("ReviewArtifact does not match the post-patch case snapshot")
        self.save(state)
        self.append_audit(case_id, patch)
        return state

    def _apply_requirement_updates(self, state: CaseState, updates: dict[str, Any]) -> None:
        existing = {item.id: item for item in state.requirements}
        for raw_id in updates.get("remove_requirements") or []:
            requirement_id = _normalize_requirement_id(raw_id)
            if requirement_id in AUTO_DERIVED_COMPILER_REQUIREMENTS:
                continue
            if not requirement_id or requirement_id not in existing:
                continue
            if _requirement_has_evidence(state, requirement_id):
                raise ValueError(f"Cannot remove requirement with evidence: {requirement_id}")
            state.requirements = [item for item in state.requirements if item.id != requirement_id]
            existing.pop(requirement_id, None)

        for raw in updates.get("requirements") or []:
            data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
            requirement_id = _normalize_requirement_id(data.get("id"))
            if not requirement_id:
                continue
            if not is_known_requirement(requirement_id):
                raise ValueError(f"Unknown requirement id: {requirement_id}")
            if requirement_id in AUTO_DERIVED_COMPILER_REQUIREMENTS:
                continue
            if requirement_id in existing:
                current = existing[requirement_id]
                current.label = requirement_label(requirement_id)
                current.kind = requirement_kind(requirement_id)
                current.required = _requirement_required_in_state(state, requirement_id, explicit=True)
                if "guidance" in data:
                    current.guidance = str(data.get("guidance") or "")
            else:
                requirement = Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=requirement_kind(requirement_id),
                    required=default_requirement_required(requirement_id),
                    guidance=str(data.get("guidance") or ""),
                )
                state.requirements.append(requirement)
                existing[requirement_id] = requirement

    def _ensure_requirements_for_evidence(self, state: CaseState, raw_items: list[Any]) -> None:
        existing = {item.id for item in state.requirements}
        support_ids = _support_requirement_ids(raw_items, existing=existing)
        if not support_ids:
            return
        unknown_catalog_ids = sorted(requirement_id for requirement_id in support_ids if not is_known_requirement(requirement_id))
        if unknown_catalog_ids:
            raise ValueError(f"Evidence support references unknown requirements: {unknown_catalog_ids}")
        if _looks_like_wrong_ap_default(state, existing, support_ids):
            state.requirements = []
            existing = set()
        if existing:
            unknown = sorted(requirement_id for requirement_id in support_ids if requirement_id not in existing)
            if unknown:
                added = self._add_optional_dynamic_requirements(state, unknown)
                existing.update(added)
                unknown = [requirement_id for requirement_id in unknown if requirement_id not in added]
            if unknown:
                raise ValueError(f"Evidence support references unknown requirements: {unknown}")
            return
        requirement_ids = _initial_requirement_ids_from_evidence(raw_items, support_ids)
        ap_chain = _has_ap_chain_evidence(raw_items, support_ids)
        for requirement_id in requirement_ids:
            inferred_optional = (
                requirement_id in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS
                and requirement_id not in INVOICE_REQUIRED_FIELD_REQUIREMENTS
            )
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=requirement_kind(requirement_id),
                    required=False
                    if (ap_chain and requirement_id in INVOICE_FIELD_REQUIREMENTS) or inferred_optional
                    else default_requirement_required(requirement_id),
                )
            )

    def _append_evidence_updates(self, state: CaseState, raw_items: list[Any]) -> None:
        self._ensure_requirements_for_evidence(state, raw_items)
        used_ids = {item.id for item in state.evidence_items}
        packet_hashes = {
            str(item.metadata.get("_review_packet_hash") or "")
            for item in state.evidence_items
            if isinstance(item.metadata, dict)
        }
        for raw in raw_items:
            data = _normalize_evidence_data(raw)
            _normalize_evidence_supports_for_state(state, data)
            _normalize_evidence_metadata(data)
            hash_payload = {key: value for key, value in data.items() if key != "created_at"}
            packet_hash = hashlib.sha256(
                json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
            if packet_hash in packet_hashes:
                continue
            data.setdefault("metadata", {})["_review_packet_hash"] = packet_hash
            evidence_id = str(data.get("id") or self.next_evidence_id(state)).strip()
            if evidence_id in used_ids:
                existing = next(item for item in state.evidence_items if item.id == evidence_id)
                if str(existing.metadata.get("_review_packet_hash") or "") == packet_hash:
                    continue
                compiler_hash = str(existing.metadata.get("compiler_source_sha256") or "")
                if compiler_hash and compiler_hash == str(data.get("metadata", {}).get("compiler_source_sha256") or ""):
                    data["id"] = evidence_id
                    data["created_at"] = existing.created_at
                    data.setdefault("metadata", {})["_review_packet_hash"] = packet_hash
                    state.evidence_items[state.evidence_items.index(existing)] = EvidenceItem.model_validate(data)
                    packet_hashes.add(packet_hash)
                    continue
                raise ValueError(f"Duplicate evidence id: {evidence_id}")
            data["id"] = evidence_id
            if not data.get("created_at"):
                data["created_at"] = utc_now()
            state.evidence_items.append(EvidenceItem.model_validate(data))
            used_ids.add(evidence_id)
            packet_hashes.add(packet_hash)

    def next_evidence_id(self, state: CaseState) -> str:
        used_ids = {item.id for item in state.evidence_items}
        index = len(used_ids) + 1
        while f"ev_{index:03d}" in used_ids:
            index += 1
        return f"ev_{index:03d}"

    def _add_optional_dynamic_requirements(self, state: CaseState, requirement_ids: list[str]) -> set[str]:
        added: set[str] = set()
        existing = {item.id for item in state.requirements}
        for raw_id in requirement_ids:
            requirement_id = _normalize_requirement_id(raw_id)
            if requirement_id in existing or requirement_id not in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS:
                continue
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=requirement_kind(requirement_id),
                    required=False,
                )
            )
            existing.add(requirement_id)
            added.add(requirement_id)
        return added

    def append_audit(self, case_id: str, patch: CasePatch) -> None:
        root = self.ensure_case_dirs(case_id)
        record = {
            "ts": utc_now(),
            "patch_type": patch.patch_type,
            "audit_note": patch.audit_note,
            "case_updates": patch.case_updates.model_dump(exclude_none=True),
        }
        append_text(root / "traces" / "case_audit.jsonl", json.dumps(record, ensure_ascii=False) + "\n")

    def _refresh_requirements(self, state: CaseState) -> None:
        _canonicalize_requirement_definitions(state)
        _ensure_requirement_premises(state)
        link_manifest_evidence(self, state.case_id, state)
        trusted_sources = trusted_sources_for_evidence(self, state.case_id, state.evidence_items)
        trusted_evidence_ids = set(trusted_sources)
        _apply_supersession_metadata(state, trusted_evidence_ids)
        state.requirements = [item for item in state.requirements if item.id not in AUTO_DERIVED_COMPILER_REQUIREMENTS]
        active_evidence = _active_evidence_items(state, trusted_evidence_ids)
        active_ids = expand_active_requirements([item.id for item in state.requirements])
        artifact = state.review_artifact
        compiled: CompiledProof | None = None
        if artifact is not None and _artifact_matches_case(
            artifact,
            active_requirement_ids=active_ids,
            active_source_fingerprints=_compiler_source_fingerprints(active_evidence),
        ):
            requiredness = {item.id: item.required for item in state.requirements}
            compiled = compile_review_artifact(
                artifact,
                requirement_requiredness={
                    requirement_id: requiredness.get(
                        requirement_id,
                        default_requirement_required(requirement_id),
                    )
                    for requirement_id in active_ids
                },
            )
        state.compiled_proof = compiled
        if compiled is not None:
            _ensure_compiled_requirements(state, compiled)
        _project_compiled_requirements(state, compiled)
        if artifact is not None:
            state.risk_flags = _unique_strings(
                [item.requirement_id for item in compiled.decisions if item.status == "CONTRADICTED"]
                if compiled
                else []
            )
            state.next_questions = _unique_strings(
                [item.missing_fact for item in compiled.obligations]
                if compiled
                else []
            )
        _refresh_workflow_status(state)


def _ensure_compiled_requirements(state: CaseState, compiled: CompiledProof) -> None:
    existing = {item.id for item in state.requirements}
    for requirement_id in sorted({item.requirement_id for item in compiled.decisions} - existing):
        state.requirements.append(
            Requirement(
                id=requirement_id,
                label=requirement_label(requirement_id),
                kind=requirement_kind(requirement_id),
                required=default_requirement_required(requirement_id),
            )
        )


def _project_compiled_requirements(
    state: CaseState,
    compiled: CompiledProof | None,
) -> None:
    decisions = {item.requirement_id: item for item in compiled.decisions} if compiled else {}
    results = {item.node_id: item for item in compiled.node_results} if compiled else {}
    for requirement in state.requirements:
        decision = decisions.get(requirement.id)
        root = results.get(decision.root_node_id) if decision else None
        if root is None:
            requirement.status = "missing"
            requirement.evidence_ids = []
            continue
        requirement.evidence_ids = _decision_source_ids(decision, results)
        evidence_leaf = requirement_owner(requirement.id) == "evidence"
        requirement.status = {
            "SUPPORTED": "accepted" if evidence_leaf else "satisfied",
            "CONTRADICTED": "conflict",
            "NOT_FOUND": "weak" if requirement.evidence_ids else "missing",
        }[decision.status]


def _decision_source_ids(decision: Any, results: dict[str, Any]) -> list[str]:
    check_ids = (
        decision.supporting_check_ids
        if decision.status == "SUPPORTED"
        else decision.contradicting_check_ids
        if decision.status == "CONTRADICTED"
        else decision.unresolved_check_ids
    )
    return _unique_strings(
        [source_id for check_id in check_ids for source_id in getattr(results.get(check_id), "source_ids", [])]
    )


def _artifact_matches_case(
    artifact: ReviewArtifact,
    *,
    active_requirement_ids: list[str],
    active_source_fingerprints: dict[str, str],
) -> bool:
    if artifact.plan_hash != artifact.plan.content_hash():
        return False
    if artifact.evidence_snapshot_hash != artifact.evidence_ir.content_hash():
        return False
    if set(artifact.plan.active_requirement_ids) != set(active_requirement_ids):
        return False
    active_evidence_ids = set(active_source_fingerprints)
    if set(artifact.evidence_ir.source_ids) != active_evidence_ids:
        return False
    if set(artifact.evidence_ir.source_fingerprints) != active_evidence_ids:
        return False
    if artifact.evidence_ir.source_fingerprints != active_source_fingerprints:
        return False
    expected_policy = policy_excerpt_for(active_requirement_ids)
    return artifact.policy_hash == policy_hash(expected_policy)


def _refresh_workflow_status(state: CaseState) -> None:
    buckets = _material_buckets(state)
    state.missing_materials = buckets["missing_materials"]
    state.weak_materials = buckets["weak_materials"]
    state.conflict_materials = buckets["conflict_materials"]
    state.satisfied_materials = buckets["satisfied_materials"]
    if state.evidence_items and state.requirements and case_decision_ready(state):
        state.status = "ready_for_report"
    elif state.evidence_items:
        state.status = "collecting_materials"
    else:
        state.status = "new"


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, default=str).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
def _unique_conflicts(values: Any) -> list[ConflictRecord]:
    seen: set[str] = set()
    result: list[ConflictRecord] = []
    for value in values if isinstance(values, list) else [values]:
        conflict = ConflictRecord.model_validate(value)
        key = conflict.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(conflict)
    return result

def _compact_jsonish_dict(value: Any, *, max_chars: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    return {"summary": text[: max_chars - 16] + "...[truncated]"}


def _compact_jsonish_records(value: Any, *, limit: int, max_chars: int) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    records: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        text = json.dumps(item, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            records.append(item)
        else:
            records.append({"summary": text[: max_chars - 16] + "...[truncated]"})
    return records


def _material_buckets(state: CaseState) -> dict[str, list[str]]:
    buckets = {
        "missing_materials": [],
        "weak_materials": [],
        "conflict_materials": [],
        "satisfied_materials": [],
    }
    for requirement in state.requirements:
        if not requirement.required and requirement.status in {"missing", "weak"}:
            continue
        if requirement.status == "missing":
            buckets["missing_materials"].append(requirement.id)
        elif requirement.status == "conflict":
            buckets["conflict_materials"].append(requirement.id)
        elif requirement.status in {"accepted", "satisfied"}:
            buckets["satisfied_materials"].append(requirement.id)
        else:
            buckets["weak_materials"].append(requirement.id)
    return {key: _unique_strings(value) for key, value in buckets.items()}


def _active_evidence_items(state: CaseState, trusted_evidence_ids: set[str]) -> list[EvidenceItem]:
    superseded = _superseded_evidence_ids(state.evidence_items, trusted_evidence_ids)
    return [
        item
        for item in state.evidence_items
        if item.id in trusted_evidence_ids and item.id not in superseded
    ]


def _compiler_source_fingerprints(items: list[EvidenceItem]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        declared = str(metadata.get("compiler_source_sha256") or "").strip()
        content = str(item.content or "") or "\n".join(str(value) for value in item.quoted_text)
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not declared:
            result[item.id] = actual
        elif declared == actual:
            result[item.id] = declared
        else:
            result[item.id] = hashlib.sha256(f"{declared}:{actual}".encode("utf-8")).hexdigest()
    return result


def _is_advisory_memory_evidence(item: EvidenceItem) -> bool:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    boundary = str(metadata.get("boundary") or review_result.get("boundary") or "").lower()
    truth_status = str(metadata.get("truth_status") or review_result.get("truth_status") or "").lower()
    source_ref = str(metadata.get("source_ref") or review_result.get("source_ref") or "").lower()
    if item.source == "rag":
        return True
    if boundary == "memory_hint_only_not_case_truth":
        return True
    if truth_status == "advisory":
        return True
    return "memory_hint_only_not_case_truth" in source_ref


def _superseded_evidence_ids(items: list[EvidenceItem], trusted_evidence_ids: set[str]) -> set[str]:
    return set(_supersession_map(items, trusted_evidence_ids))


def _apply_supersession_metadata(state: CaseState, trusted_evidence_ids: set[str]) -> bool:
    superseded_by = _supersession_map(state.evidence_items, trusted_evidence_ids)
    if not superseded_by:
        return False
    changed = False
    for index, item in enumerate(list(state.evidence_items)):
        new_id = superseded_by.get(item.id)
        if not new_id:
            continue
        data = item.model_dump()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if metadata.get("review_stage") == "superseded" and metadata.get("superseded_by_evidence_id") == new_id:
            continue
        metadata["review_stage"] = "superseded"
        metadata["superseded_by_evidence_id"] = new_id
        data["metadata"] = metadata
        state.evidence_items[index] = EvidenceItem.model_validate(data)
        changed = True
    return changed


def _supersession_map(items: list[EvidenceItem], trusted_evidence_ids: set[str]) -> dict[str, str]:
    counts = Counter(item.id for item in items)
    by_id = {item.id: item for item in items if counts[item.id] == 1}
    candidates: dict[str, list[str]] = {}
    for item in items:
        if counts[item.id] != 1 or not _can_supersede_evidence(item, trusted_evidence_ids):
            continue
        for old_id in _metadata_supersedes_ids(item.metadata):
            old = by_id.get(old_id)
            if old is not None and old.id != item.id and old.type == item.type:
                candidates.setdefault(old_id, []).append(item.id)
    return {old_id: replacements[0] for old_id, replacements in candidates.items() if len(replacements) == 1}


def _can_supersede_evidence(item: EvidenceItem, trusted_evidence_ids: set[str]) -> bool:
    if item.id not in trusted_evidence_ids or item.source != "attachment" or item.credibility == "low":
        return False
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    if review_result.get("should_accept") is not True or _is_advisory_memory_evidence(item):
        return False
    if str(metadata.get("classification") or "").lower() != "business_evidence":
        return False
    if str(metadata.get("review_stage") or "").lower() != "corrected":
        return False
    return bool(_metadata_supersedes_ids(metadata))


def _metadata_supersedes_ids(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    values = _string_list(metadata.get("supersedes_evidence_id"))
    values.extend(_string_list(metadata.get("supersedes_evidence_ids")))
    return _unique_strings(values)


def _refresh_material_buckets(state: CaseState) -> bool:
    before = (
        list(state.missing_materials),
        list(getattr(state, "weak_materials", []) or []),
        list(getattr(state, "conflict_materials", []) or []),
        list(getattr(state, "satisfied_materials", []) or []),
    )
    buckets = _material_buckets(state)
    state.missing_materials = buckets["missing_materials"]
    state.weak_materials = buckets["weak_materials"]
    state.conflict_materials = buckets["conflict_materials"]
    state.satisfied_materials = buckets["satisfied_materials"]
    after = (
        list(state.missing_materials),
        list(state.weak_materials),
        list(state.conflict_materials),
        list(state.satisfied_materials),
    )
    return before != after


def _normalize_evidence_data(raw: Any) -> dict[str, Any]:
    data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
    if not data.get("type") and data.get("evidence_type"):
        data["type"] = data.get("evidence_type")
    if not data.get("summary") and data.get("reference"):
        data["summary"] = str(data.get("reference"))
    data["conflicts"] = _unique_conflicts(data.get("conflicts") or [])
    data["quoted_text"] = _string_list(data.get("quoted_text"))
    data["supports"] = _normalize_support_records(data.get("supports"))
    if _is_cross_case_evidence(data):
        data["supports"] = []
        data["conflicts"] = _unique_conflicts(
            data["conflicts"] + ["Cross-case or mixed-case evidence is recorded but not linked to active requirements."]
        )
    if _is_prompt_injection_evidence(data):
        _quarantine_prompt_injection_evidence(data)
    return data


def _is_prompt_injection_evidence(data: dict[str, Any]) -> bool:
    metadata = _metadata_dict(data)
    classification = str(metadata.get("classification") or "").strip().lower()
    if classification in {"prompt_injection", "quarantined"}:
        return True
    extracted = metadata.get("extracted_fields")
    if isinstance(extracted, dict) and "injection_detected" in extracted:
        return True
    text = " ".join(
        _jsonish_text(value)
        for value in (
            data.get("summary"),
            data.get("content"),
            data.get("reviewer_notes"),
            data.get("conflicts"),
            data.get("risk_flags"),
            data.get("review_result"),
        )
        if value
    ).lower()
    markers = (
        "ignore previous",
        "ignore all previous",
        "disregard previous",
        "system prompt",
        "developer message",
        "directly approve",
        "指令污染",
        "越权执行",
        "忽略前文",
        "忽略规则",
        "直接批准",
    )
    return any(marker in text for marker in markers)


def _quarantine_prompt_injection_evidence(data: dict[str, Any]) -> None:
    metadata = _metadata_dict(data)
    original_ref = metadata.get("original_ref") or metadata.get("source_locator") or data.get("source") or ""
    metadata["classification"] = "quarantined"
    metadata["extracted_fields"] = {
        "injection_detected": {
            "value": True,
            "status": "present",
            "source_quote": "[redacted prompt injection]",
            "source_locator": str(original_ref or "attachment text"),
            "confidence": "high",
        },
        "document_confidence": {
            "value": "low",
            "status": "present",
            "source_quote": "quarantined source",
            "source_locator": str(original_ref or "attachment text"),
            "confidence": "high",
        },
    }
    data["type"] = "unknown"
    data["credibility"] = "low"
    data["summary"] = "污染材料中的业务字段已整体隔离"
    data["content"] = "该附件包含可疑指令文本；其中的业务字段不会用于满足任何 requirement。"
    data["supports"] = []
    data["quoted_text"] = ["[redacted prompt injection]"]
    data["reviewer_notes"] = "该来源已隔离；业务字段、金额、供应商、日期等内容不进入证据支持链。"
    data["conflicts"] = _unique_conflicts(["prompt_injection_quarantine"])
    data["metadata"] = metadata


def _normalize_existing_evidence_metadata(state: CaseState) -> bool:
    changed = False
    for index, item in enumerate(list(state.evidence_items)):
        data = item.model_dump()
        if _normalize_evidence_metadata(data):
            state.evidence_items[index] = EvidenceItem.model_validate(data)
            changed = True
    return changed


def _normalize_evidence_metadata(data: dict[str, Any]) -> bool:
    metadata = _metadata_dict(data)
    if not metadata:
        return False
    existing = metadata.get("field_inventory")
    rows: list[dict[str, Any]] = []
    existing_field_keys: set[str] = set()
    if isinstance(existing, list):
        for row in existing:
            if not isinstance(row, dict) or not _is_curated_field_inventory_row(row):
                continue
            field_key = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(row.get("field") or row.get("requirement") or "").lower(),
            ).strip("_")
            if field_key and _field_already_covered_in_inventory(field_key, existing_field_keys):
                continue
            rows.append(dict(row))
            if field_key:
                existing_field_keys.add(field_key)
    seen = {str(row.get("crop_path") or "") for row in rows if str(row.get("crop_path") or "")}
    seen_fields = set(existing_field_keys)

    def add(row: Any, *, field: str = "", secondary: bool = False) -> None:
        if not isinstance(row, dict):
            return
        if not _is_curated_field_inventory_row(row, field=field):
            return
        field_key = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(field or row.get("field") or row.get("requirement") or "").lower(),
        ).strip("_")
        if secondary and field_key and _field_already_covered_in_inventory(field_key, seen_fields):
            return
        crop_path = str(row.get("crop_path") or "").strip()
        if not crop_path or crop_path in seen:
            return
        record = dict(row)
        if field and not record.get("field"):
            record["field"] = field
        if not record.get("source_quote") and record.get("quote"):
            record["source_quote"] = record.get("quote")
        if not record.get("source_quote") and record.get("text"):
            record["source_quote"] = record.get("text")
        if not record.get("value") and record.get("source_quote"):
            record["value"] = record.get("source_quote")
        if not record.get("locator") and record.get("block_or_table_or_region"):
            page = str(record.get("page") or "").strip()
            block = str(record.get("block_or_table_or_region") or "").strip()
            record["locator"] = f"page {page} {block}".strip()
        if not record.get("proof_label"):
            record["proof_label"] = record.get("proves") or record.get("proof") or record.get("claim") or f"{record.get('field') or 'field'} source crop"
        record.setdefault("crop_status", "cropped")
        rows.append(record)
        seen.add(crop_path)
        if field_key:
            seen_fields.add(field_key)

    extracted = metadata.get("extracted_fields")
    if isinstance(extracted, dict):
        for field, value in extracted.items():
            add(value, field=str(field))
    for key in ("evidence_chain", "claim_to_source_refs"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for row in values:
            add(row, secondary=True)
    if rows and rows != existing:
        metadata["field_inventory"] = rows
        data["metadata"] = metadata
        return True
    return False


def _is_curated_field_inventory_row(row: dict[str, Any], *, field: str = "") -> bool:
    field_name = str(field or row.get("field") or row.get("requirement") or "").strip()
    if not field_name:
        return False
    crop_id = str(row.get("crop_id") or "").strip()
    name = f"{field_name} {crop_id}".lower()
    if "_context" in name or "[truncated]" in name or "[截断]" in name:
        return False
    if re.fullmatch(r"p\d+_b\d+", crop_id.lower()) and not field_name:
        return False
    if "page_number" in name:
        return False
    known = {
        "invoice_number",
        "supplier",
        "buyer",
        "invoice_date",
        "amount_total",
        "currency_tax",
        "currency",
        "tax_amount",
        "line_items_product_title",
        "signature_or_authorized_signatory",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
        "source_traceability",
        "template_match",
        "bank_details",
    }
    normalized = re.sub(r"[^a-z0-9_]+", "_", field_name.lower()).strip("_")
    if normalized in known:
        return True
    proof = str(row.get("proof_label") or row.get("proves") or row.get("proof") or row.get("claim") or "")
    return bool(proof.strip()) and "context" not in proof.lower()


def _field_already_covered_in_inventory(field_key: str, seen_fields: set[str]) -> bool:
    if field_key in seen_fields:
        return True
    aliases = {
        "currency": {"currency_tax"},
        "tax_amount": {"currency_tax"},
        "tax_details": {"currency_tax"},
        "visual_signature_mark": {"signature_or_authorized_signatory"},
    }
    return bool(aliases.get(field_key, set()).intersection(seen_fields))


def _normalize_requirement_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = REQUIREMENT_ID_ALIASES.get(text, text)
    if not SAFE_REQUIREMENT_ID.match(text) or ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"Unsafe requirement id: {value}")
    return text


def _normalize_support_records(value: Any) -> list[Any]:
    items = value if isinstance(value, list) else ([] if value is None else [value])
    normalized: list[Any] = []
    for item in items:
        data = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item or {})
        requirement_id = _normalize_requirement_id(data.get("requirement"))
        if not requirement_id:
            continue
        data["requirement"] = requirement_id
        normalized.append(data)
    return normalized


def _normalize_evidence_supports_for_state(state: CaseState, data: dict[str, Any]) -> None:
    existing = {item.id for item in state.requirements}
    supports = data.get("supports")
    if not isinstance(supports, list):
        return
    supports = [
        support
        for support in supports
        if not (
            isinstance(support, dict)
            and _normalize_requirement_id(support.get("requirement"))
            in COMPILER_AUTHORITY_REQUIREMENTS
        )
    ]
    data["supports"] = supports
    if not existing:
        return
    for support in supports:
        if not isinstance(support, dict):
            continue
        support["requirement"] = _support_requirement_id_for_existing(support.get("requirement"), existing)


def _support_requirement_id_for_existing(value: Any, existing: set[str]) -> str:
    requirement_id = _normalize_requirement_id(value)
    alias = SUPPORT_REQUIREMENT_ID_ALIASES.get(requirement_id, requirement_id)
    if alias in existing and requirement_id not in existing:
        return alias
    return requirement_id


def _core_requirement_for_evidence(data: dict[str, Any]) -> str:
    review = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    evidence_type = str(data.get("type") or review.get("evidence_type") or "").strip()
    return {
        "invoice": "invoice",
        "purchase_order": "purchase_order",
        "goods_receipt": "goods_receipt_or_service_acceptance",
        "service_acceptance": "goods_receipt_or_service_acceptance",
        "vendor_record": "vendor_identity",
        "duplicate_payment_check": "duplicate_payment_screen",
    }.get(evidence_type, "")


def _support_requirement_ids(raw_items: list[Any], *, existing: set[str] | None = None) -> list[str]:
    result: list[str] = []
    existing_ids = existing or set()
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        for support in data.get("supports") or []:
            support_data = support.model_dump(exclude_none=True) if hasattr(support, "model_dump") else dict(support or {})
            requirement_id = (
                _support_requirement_id_for_existing(support_data.get("requirement"), existing_ids)
                if existing_ids
                else _normalize_requirement_id(support_data.get("requirement"))
            )
            if requirement_id and requirement_id not in COMPILER_AUTHORITY_REQUIREMENTS:
                result.append(requirement_id)
    return _unique_strings(result)


def _drop_legacy_page_edge_warnings(state: CaseState) -> bool:
    changed = False

    def keep(value: Any) -> bool:
        text = str(value or "").lower()
        legacy_false_warnings = (
            "content_near_page_edge",
            "页面边缘内容接近",
            "manifest状态weak",
            "正文存在大字段截断",
            "中间明细区域未完整可见",
            "商品明细截断",
        )
        return not any(marker in text for marker in legacy_false_warnings)

    cleaned_risks = [risk for risk in state.risk_flags if keep(risk)]
    if cleaned_risks != state.risk_flags:
        state.risk_flags = cleaned_risks
        changed = True

    for index, item in enumerate(list(state.evidence_items)):
        data = item.model_dump()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        item_changed = False
        quality_notes = metadata.get("quality_notes")
        if isinstance(quality_notes, list):
            cleaned_notes = [note for note in quality_notes if keep(note)]
            if cleaned_notes != quality_notes:
                metadata["quality_notes"] = cleaned_notes
                item_changed = True
        visual_check = metadata.get("visual_check")
        if isinstance(visual_check, dict):
            page_integrity = visual_check.get("page_integrity")
            if isinstance(page_integrity, dict):
                warnings = page_integrity.get("warnings")
                if isinstance(warnings, list):
                    cleaned_warnings = [warning for warning in warnings if keep(warning)]
                    if cleaned_warnings != warnings:
                        page_integrity["warnings"] = cleaned_warnings
                        if not cleaned_warnings and page_integrity.get("status") == "warning":
                            page_integrity["status"] = "ok"
                        item_changed = True
        notes = str(data.get("reviewer_notes") or "")
        if not keep(notes):
            cleaned = re.sub(r"[^。；;\n]*content_near_page_edge[^。；;\n]*[。；;]?", "", notes)
            cleaned = re.sub(r"[^。；;\n]*页面边缘内容接近[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*Manifest状态weak[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*正文存在大字段截断[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*中间明细区域未完整可见[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*商品明细截断[^。；;\n]*[。；;]?", "", cleaned)
            data["reviewer_notes"] = cleaned.strip()
            item_changed = True
        if item_changed:
            data["metadata"] = metadata
            state.evidence_items[index] = EvidenceItem.model_validate(data)
            changed = True
    return changed


def _metadata_dict(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _initial_requirement_ids_from_evidence(raw_items: list[Any], support_ids: list[str]) -> list[str]:
    has_invoice_evidence = False
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        evidence_type = str(data.get("type") or data.get("evidence_type") or "").strip()
        if evidence_type == "invoice":
            has_invoice_evidence = True
    if _has_ap_chain_evidence(raw_items, support_ids):
        canonical_supports = [_canonical_ap_requirement_id(item) for item in support_ids]
        return _unique_strings(list(INVOICE_FIELD_REQUIREMENTS) + list(AP_LITE_REQUIREMENTS) + canonical_supports)
    if has_invoice_evidence or any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids):
        return _unique_strings(list(INVOICE_FIELD_REQUIREMENTS) + list(support_ids))
    return support_ids


def _has_ap_chain_evidence(raw_items: list[Any], support_ids: list[str]) -> bool:
    ap_ids = _ap_requirement_ids(raw_items, support_ids)
    return "invoice" in ap_ids and len(ap_ids) >= 3


def _ap_requirement_ids(
    raw_items: list[Any],
    support_ids: list[str],
    *,
    state: CaseState | None = None,
) -> set[str]:
    result = {_canonical_ap_requirement_id(item) for item in support_ids}
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        result.add(_canonical_ap_requirement_id(_core_requirement_for_evidence(data)))
    if state is not None:
        for item in state.evidence_items:
            result.add(_canonical_ap_requirement_id(_core_requirement_for_evidence(item.model_dump())))
            for support in item.supports:
                result.add(_canonical_ap_requirement_id(support.requirement))
    return {item for item in result if item in set(AP_LITE_REQUIREMENTS)}


def _canonical_ap_requirement_id(value: Any) -> str:
    requirement_id = _normalize_requirement_id(value)
    return SUPPORT_REQUIREMENT_ID_ALIASES.get(requirement_id, requirement_id)


def _looks_like_wrong_ap_default(state: CaseState, existing: set[str], support_ids: list[str]) -> bool:
    if state.evidence_items:
        return False
    ap_defaults = set(AP_THREE_WAY_REQUIREMENTS) | set(AP_LITE_REQUIREMENTS)
    if not existing or not existing.issubset(ap_defaults):
        return False
    has_invoice_field_support = any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids)
    has_ap_support = any(_canonical_ap_requirement_id(item) in AP_LITE_REQUIREMENTS for item in support_ids)
    return has_invoice_field_support and not has_ap_support


def _requirement_has_evidence(state: CaseState, requirement_id: str) -> bool:
    for item in state.evidence_items:
        for support in item.supports:
            if support.requirement == requirement_id:
                return True
    return any(requirement_id in item.evidence_ids for item in state.requirements if item.id == requirement_id)


def _canonicalize_requirement_definitions(state: CaseState) -> None:
    for requirement in state.requirements:
        if not is_known_requirement(requirement.id):
            continue
        requirement.label = requirement_label(requirement.id)
        requirement.kind = requirement_kind(requirement.id)
        requirement.required = _requirement_required_in_state(
            state,
            requirement.id,
            current_required=requirement.required,
        )


def _ensure_requirement_premises(state: CaseState) -> None:
    existing = {item.id: item for item in state.requirements}
    for conclusion in list(state.requirements):
        if conclusion.id in AUTO_DERIVED_COMPILER_REQUIREMENTS:
            continue
        for requirement_id in requirement_premises(conclusion.id):
            current = existing.get(requirement_id)
            if current is not None:
                if conclusion.required:
                    current.required = True
                continue
            current = Requirement(
                id=requirement_id,
                label=requirement_label(requirement_id),
                kind=requirement_kind(requirement_id),
                required=conclusion.required,
            )
            state.requirements.append(current)
            existing[requirement_id] = current


def _requirement_required_in_state(
    state: CaseState,
    requirement_id: str,
    *,
    explicit: bool = False,
    current_required: bool | None = None,
) -> bool:
    active_ids = {item.id for item in state.requirements}
    ap_ids = active_ids.intersection(AP_LITE_REQUIREMENTS)
    if requirement_id in INVOICE_FIELD_REQUIREMENTS and "invoice" in ap_ids and len(ap_ids) >= 3:
        return False
    if any(item.required and requirement_id in requirement_premises(item.id) for item in state.requirements):
        return True
    if explicit:
        return default_requirement_required(requirement_id)
    optional_premise = any(
        not item.required and requirement_id in requirement_premises(item.id)
        for item in state.requirements
    )
    if current_required is False and (
        requirement_id in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS or optional_premise
    ):
        return False
    return default_requirement_required(requirement_id)


def _jsonish_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)


def _string_list(value: Any) -> list[str]:
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


def _is_cross_case_evidence(data: dict[str, Any]) -> bool:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    classification = str(metadata.get("classification") or "").strip().lower()
    if classification in {"cross_case_sample", "mixed_case_document", "out_of_scope_reference"}:
        return True
    review_result = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    text = " ".join(
        _jsonish_text(value)
        for value in (
            data.get("summary"),
            data.get("content"),
            data.get("reviewer_notes"),
            data.get("conflicts"),
            review_result.get("reason"),
        )
        if value
    ).lower()
    markers = (
        "cross_case",
        "mixed_case",
        "cross-case",
        "mixed-case",
        "out-of-scope",
        "另案",
        "跨案",
        "误传文档",
        "不属于当前",
        "不同case",
        "不同 case",
        "另一个case",
        "另一个 case",
        "独立case",
        "独立 case",
        "新case",
        "新 case",
        "separate case",
        "not the same case",
    )
    return any(marker in text for marker in markers)


def _migrate_case_state_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    migrated = dict(data)
    requirements = migrated.get("requirements")
    if isinstance(requirements, list):
        merged: dict[str, dict[str, Any]] = {}
        for item in requirements:
            row = _migrate_requirement_data(item)
            if not isinstance(row, dict):
                continue
            requirement_id = str(row.get("id") or "").strip()
            current = merged.setdefault(requirement_id, row)
            if current is not row:
                current["required"] = bool(current.get("required", True) or row.get("required", True))
                current["evidence_ids"] = _unique_strings([
                    *(current.get("evidence_ids") or []),
                    *(row.get("evidence_ids") or []),
                ])
        migrated["requirements"] = list(merged.values())
    evidence_items = migrated.get("evidence_items")
    if isinstance(evidence_items, list):
        migrated["evidence_items"] = [_migrate_evidence_data(item) for item in evidence_items]
    compiled = migrated.get("compiled_proof")
    if isinstance(compiled, dict):
        if isinstance(compiled.get("node_results"), list) and isinstance(compiled.get("decisions"), list):
            migrated["compiled_proof"] = {
                key: compiled.get(key) if isinstance(compiled.get(key), list) else []
                for key in ("node_results", "decisions", "obligations", "diagnostics")
            }
        else:
            migrated["compiled_proof"] = None
    artifact = migrated.get("review_artifact")
    if isinstance(artifact, dict) and "plan" not in artifact:
        migrated["review_artifact"] = None
    return migrated


def _migrate_requirement_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    migrated = dict(data)
    requirement_id = str(migrated.get("id") or "").strip()
    migrated["id"] = LEGACY_STORED_REQUIREMENT_ALIASES.get(requirement_id, requirement_id)
    status = str(migrated.get("status") or "").strip()
    legacy_status_map = {
        "partial": "submitted",
        "complete": "satisfied",
        "completed": "satisfied",
    }
    if status in legacy_status_map:
        migrated["status"] = legacy_status_map[status]
    return migrated


def _migrate_evidence_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    migrated = dict(data)
    supports = []
    for item in migrated.get("supports") or []:
        if not isinstance(item, dict):
            supports.append(item)
            continue
        row = dict(item)
        requirement_id = str(row.get("requirement") or "").strip()
        row["requirement"] = LEGACY_STORED_REQUIREMENT_ALIASES.get(requirement_id, requirement_id)
        supports.append(row)
    migrated["supports"] = supports
    conflicts = []
    for item in migrated.get("conflicts") or []:
        if not isinstance(item, dict):
            conflicts.append(item)
            continue
        row = dict(item)
        requirement_id = str(row.get("requirement") or "").strip()
        if requirement_id:
            row["requirement"] = LEGACY_STORED_REQUIREMENT_ALIASES.get(requirement_id, requirement_id)
        if isinstance(row.get("affected_fields"), list):
            row["affected_fields"] = [
                LEGACY_STORED_REQUIREMENT_ALIASES.get(str(value), str(value))
                for value in row["affected_fields"]
            ]
        conflicts.append(row)
    migrated["conflicts"] = conflicts
    return migrated


def _unique_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[str] = set()
    result: list[EvidenceItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        result.append(item)
    return result
