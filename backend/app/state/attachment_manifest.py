from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.state.persistence import PERSISTENCE_LOCK, atomic_write_text


MANIFEST_RELATIVE_PATH = "attachments/attachment_manifest.json"
MANIFEST_VERSION = "attachment_manifest_v1"
CONTEXT_ITEM_LIMIT = 18
REF_STATUSES = {"active", "weak"}
EXCLUDED_STATUSES = {"excluded", "quarantined", "error"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_attachment_manifest(store: Any, case_id: str) -> dict[str, Any]:
    path = store.resolve_case_path(case_id, MANIFEST_RELATIVE_PATH)
    if not path.exists():
        return _empty_manifest(case_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_manifest(case_id)
    if not isinstance(data, dict):
        return _empty_manifest(case_id)
    data.setdefault("version", MANIFEST_VERSION)
    data.setdefault("case_id", case_id)
    data.setdefault("attachments", [])
    return data


def save_attachment_manifest(store: Any, case_id: str, manifest: dict[str, Any]) -> None:
    path = store.resolve_case_path(case_id, MANIFEST_RELATIVE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["version"] = MANIFEST_VERSION
    manifest["case_id"] = case_id
    manifest["updated_at"] = utc_now()
    atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2, default=str))


def upsert_manifest_read_items(
    store: Any,
    case_id: str,
    items: list[dict[str, Any]],
    *,
    session_id: str = "",
    turn_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    with PERSISTENCE_LOCK:
        return _upsert_manifest_read_items(
            store,
            case_id,
            items,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
        )


def _upsert_manifest_read_items(
    store: Any,
    case_id: str,
    items: list[dict[str, Any]],
    *,
    session_id: str = "",
    turn_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    manifest = load_attachment_manifest(store, case_id)
    existing = {str(item.get("attachment_id") or ""): item for item in manifest.get("attachments") or []}
    by_ref = {str(item.get("original_ref") or ""): item for item in manifest.get("attachments") or [] if item.get("original_ref")}
    updated_ids: list[str] = []
    for item in items:
        entry = _entry_from_read_item(store, case_id, item, session_id=session_id, turn_id=turn_id, run_id=run_id)
        current = existing.get(entry["attachment_id"]) or by_ref.get(str(entry.get("original_ref") or ""))
        if current:
            entry["evidence_ids"] = _unique_strings(list(current.get("evidence_ids") or []) + list(entry.get("evidence_ids") or []))
            entry["summary"] = str(current.get("summary") or entry.get("summary") or "")
            entry["key_facts"] = list(current.get("key_facts") or entry.get("key_facts") or [])
            entry["risks"] = _unique_strings(list(current.get("risks") or []) + list(entry.get("risks") or []))
            entry["status"] = _merge_status(str(current.get("status") or ""), str(entry.get("status") or ""))
            entry["reason"] = str(current.get("reason") or entry.get("reason") or "")
            for key in (
                "extraction_ref",
                "extraction_sha256",
                "field_inventory",
                "page_summaries",
                "quality_notes",
                "extraction_methods",
                "visual_regions",
                "visual_check",
                "block_crops",
                "table_count",
            ):
                if not entry.get(key) and current.get(key):
                    entry[key] = current.get(key)
            manifest["attachments"].remove(current)
        manifest["attachments"].append(entry)
        item["attachment_id"] = entry["attachment_id"]
        item["manifest_status"] = entry["status"]
        updated_ids.append(entry["attachment_id"])
    manifest["attachments"] = sorted(manifest.get("attachments") or [], key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    save_attachment_manifest(store, case_id, manifest)
    return {
        "manifest_ref": MANIFEST_RELATIVE_PATH,
        "updated_attachment_ids": updated_ids,
        "status_counts": _status_counts(manifest.get("attachments") or []),
    }


def update_manifest_summaries(
    store: Any,
    case_id: str,
    *,
    artifact_ref: str,
    summaries: list[dict[str, Any]],
) -> None:
    with PERSISTENCE_LOCK:
        _update_manifest_summaries(store, case_id, artifact_ref=artifact_ref, summaries=summaries)


def _update_manifest_summaries(
    store: Any,
    case_id: str,
    *,
    artifact_ref: str,
    summaries: list[dict[str, Any]],
) -> None:
    if not summaries:
        return
    manifest = load_attachment_manifest(store, case_id)
    changed = False
    for summary in summaries:
        entry = _find_manifest_entry(manifest, summary)
        if not entry:
            continue
        entry["artifact_ref"] = artifact_ref
        entry["summary"] = _brief(summary.get("summary"), 700)
        entry["key_facts"] = _brief_list(summary.get("key_facts"), 12, 220)
        summary_risks = _source_quality_notes(summary.get("risks"))
        entry["risks"] = _unique_strings(_brief_list(summary_risks, 8, 220) + list(entry.get("risks") or []))
        limitations = _source_quality_notes(summary.get("missing_items"))
        if limitations:
            entry["limitations"] = _brief_list(limitations, 8, 220)
            if entry.get("status") == "active":
                entry["status"] = "weak"
        entry["updated_at"] = utc_now()
        changed = True
    if changed:
        save_attachment_manifest(store, case_id, manifest)


def link_manifest_evidence(store: Any, case_id: str, case_state: Any) -> None:
    with PERSISTENCE_LOCK:
        _link_manifest_evidence(store, case_id, case_state)


def trusted_sources_for_evidence(
    store: Any,
    case_id: str,
    evidence_items: list[Any],
) -> dict[str, dict[str, Any]]:
    """Return runtime-owned source text from a direct, precise attachment binding."""

    manifest = load_attachment_manifest(store, case_id)
    result: dict[str, dict[str, Any]] = {}
    evidence_id_counts = Counter(str(getattr(item, "id", "") or "") for item in evidence_items)
    for evidence in evidence_items:
        evidence_id = str(getattr(evidence, "id", "") or "")
        if not evidence_id or evidence_id_counts[evidence_id] != 1:
            continue
        metadata = getattr(evidence, "metadata", {}) or {}
        entry = _find_precise_manifest_entry(manifest, _evidence_match_probes(evidence, metadata))
        if entry is None or str(entry.get("status") or "") not in REF_STATUSES:
            continue
        original_ref = str(entry.get("original_ref") or "")
        expected_digest = str(entry.get("sha256") or "")
        if not original_ref or not expected_digest:
            continue
        try:
            source_path = store.resolve_case_path(case_id, original_ref)
        except Exception:
            continue
        if not source_path.is_file() or _source_sha256(store, case_id, original_ref, {}) != expected_digest:
            continue
        texts = _trusted_source_texts(store, case_id, entry, source_path)
        if texts:
            result[evidence_id] = {
                "texts": texts,
                "sha256": expected_digest,
                "original_ref": original_ref,
                "extraction_sha256": str(entry.get("extraction_sha256") or ""),
            }
    return result


def _link_manifest_evidence(store: Any, case_id: str, case_state: Any) -> None:
    manifest = load_attachment_manifest(store, case_id)
    changed = False
    for evidence in getattr(case_state, "evidence_items", []) or []:
        metadata = getattr(evidence, "metadata", {}) or {}
        probes = _evidence_match_probes(evidence, metadata)
        has_precise_identity = _has_precise_attachment_identity(probes)
        entry = (
            _find_precise_manifest_entry(manifest, probes)
            if has_precise_identity
            else _find_manifest_entry(manifest, probes)
        )
        if not entry and not has_precise_identity:
            entry = _find_manifest_entry_by_evidence_text(manifest, evidence, metadata)
        if not entry:
            continue
        evidence_id = str(getattr(evidence, "id", "") or "")
        if evidence_id:
            entry["evidence_ids"] = _unique_strings(list(entry.get("evidence_ids") or []) + [evidence_id])
        new_status = _status_from_evidence(evidence, metadata)
        if new_status:
            entry["status"] = _merge_status(str(entry.get("status") or ""), new_status)
        reason = _reason_from_evidence(evidence, metadata)
        if reason:
            entry["reason"] = reason
        entry["updated_at"] = utc_now()
        changed = True
    if changed:
        save_attachment_manifest(store, case_id, manifest)


def _trusted_source_texts(store: Any, case_id: str, entry: dict[str, Any], source_path: Path) -> tuple[str, ...]:
    values: list[str] = []
    extraction_ref = str(entry.get("extraction_ref") or "")
    if extraction_ref:
        expected_digest = str(entry.get("extraction_sha256") or "")
        try:
            dossier_path = store.resolve_case_path(case_id, extraction_ref)
            if not expected_digest or hashlib.sha256(dossier_path.read_bytes()).hexdigest() != expected_digest:
                raise OSError("untrusted extraction dossier")
            dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            dossier = {}
        if isinstance(dossier, dict) and str(dossier.get("attachment_id") or "") == str(entry.get("attachment_id") or ""):
            values.extend(str(dossier.get(key) or "") for key in ("full_text", "body_markdown"))
            for key, field in (
                ("pages", "text"),
                ("blocks", "text"),
                ("tables", "markdown"),
                ("tables", "csv"),
                ("field_inventory", "source_quote"),
                ("block_crops", "text"),
            ):
                values.extend(
                    str(row.get(field) or "")
                    for row in dossier.get(key) or []
                    if isinstance(row, dict)
                )
    if source_path.suffix.lower() in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}:
        try:
            values.append(source_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return tuple(_unique_strings(values))


def attachment_manifest_for_context(store: Any, case_id: str, *, limit: int = CONTEXT_ITEM_LIMIT) -> dict[str, Any]:
    manifest = load_attachment_manifest(store, case_id)
    attachments = [item for item in manifest.get("attachments") or [] if isinstance(item, dict)]
    visible = sorted(attachments, key=_context_sort_key)[:limit]
    return {
        "manifest_ref": MANIFEST_RELATIVE_PATH,
        "version": manifest.get("version", MANIFEST_VERSION),
        "updated_at": manifest.get("updated_at", ""),
        "status_counts": _status_counts(attachments),
        "attachments": [_context_item(item) for item in visible],
    }


def resolve_manifest_attachment(store: Any, case_id: str, *, attachment_id: str = "", original_ref: str = "") -> dict[str, Any]:
    manifest = load_attachment_manifest(store, case_id)
    entry = _find_manifest_entry(manifest, {"attachment_id": attachment_id, "original_ref": original_ref})
    if not entry:
        raise FileNotFoundError(f"Attachment manifest entry not found: attachment_id={attachment_id}, original_ref={original_ref}")
    ref = str(entry.get("original_ref") or "")
    if not ref:
        raise FileNotFoundError(f"Attachment manifest entry has no original_ref: {entry.get('attachment_id')}")
    path = store.resolve_case_path(case_id, ref)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Attachment original file is missing: {ref}")
    return {
        "attachment_id": entry.get("attachment_id", ""),
        "name": entry.get("name") or path.name,
        "path": str(path),
        "content_type": entry.get("content_type", ""),
        "original_ref": ref,
        "manifest_status": entry.get("status", ""),
    }


def manifest_status_for_ref(store: Any, case_id: str, original_ref: str) -> str:
    if not original_ref:
        return ""
    manifest = load_attachment_manifest(store, case_id)
    entry = _find_manifest_entry(manifest, {"original_ref": original_ref})
    return str((entry or {}).get("status") or "")


def _empty_manifest(case_id: str) -> dict[str, Any]:
    now = utc_now()
    return {"version": MANIFEST_VERSION, "case_id": case_id, "attachments": [], "created_at": now, "updated_at": now}


def _entry_from_read_item(
    store: Any,
    case_id: str,
    item: dict[str, Any],
    *,
    session_id: str,
    turn_id: str,
    run_id: str,
) -> dict[str, Any]:
    original_ref = str(item.get("original_ref") or "")
    sha256 = _source_sha256(store, case_id, original_ref, item)
    attachment_id = _attachment_id(original_ref or str(item.get("path") or item.get("name") or ""), sha256)
    status, reason = _initial_status(item)
    return {
        "attachment_id": attachment_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "name": str(item.get("name") or ""),
        "path": str(item.get("path") or ""),
        "source_path": str(item.get("source_path") or item.get("path") or ""),
        "content_type": str(item.get("content_type") or ""),
        "original_ref": original_ref,
        "preview_paths": [str(path) for path in item.get("preview_paths") or [] if str(path)],
        "content_kind": str(item.get("content_kind") or ""),
        "extraction_method": str(item.get("extraction_method") or ""),
        "extraction_methods": [str(value) for value in item.get("extraction_methods") or [] if str(value)],
        "extraction_ref": str(item.get("extraction_ref") or ""),
        "extraction_sha256": str(item.get("extraction_sha256") or ""),
        "field_inventory": _brief_records(item.get("field_inventory"), 24),
        "page_summaries": _brief_records(item.get("page_summaries"), 8),
        "quality_notes": _brief_list(item.get("quality_notes"), 12, 220),
        "visual_regions": _brief_records(item.get("visual_regions"), 12),
        "visual_check": _compact_visual_check(item.get("visual_check")),
        "block_crops": _brief_records(item.get("block_crops"), 48),
        "table_count": int(item.get("table_count") or 0),
        "line_items": _brief_records(item.get("line_items"), 200),
        "line_item_count": int(item.get("line_item_count") or 0),
        "line_item_pages": [int(value) for value in item.get("line_item_pages") or [] if str(value).isdigit()][:12],
        "sha256": sha256,
        "chars": int(item.get("chars") or 0),
        "context_delivery": {
            "content_is_excerpt_for_model_context": bool(item.get("truncated")),
            "full_text_preserved_in_extraction_ref": bool(item.get("extraction_ref")),
        },
        "warnings": _brief_list(item.get("warnings"), 8, 220),
        "visual_notes": _brief_list(item.get("visual_notes"), 6, 220),
        "summary": _fallback_summary(item),
        "key_facts": _fallback_key_facts(item),
        "risks": _fallback_risks(item),
        "status": status,
        "reason": reason,
        "evidence_ids": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def _initial_status(item: dict[str, Any]) -> tuple[str, str]:
    name = str(item.get("name") or "").lower()
    content = str(item.get("content") or "").lower()
    warnings = " ".join(str(warning).lower() for warning in item.get("warnings") or [])
    if item.get("status") == "error":
        return "error", _brief("; ".join(str(warning) for warning in item.get("warnings") or []), 300)
    if "prompt_injection" in name or _has_prompt_injection(content):
        return "quarantined", "prompt-injection or execution-style text detected"
    if "irrelevant" in name or "wrong_workflow" in name:
        return "excluded", "file name indicates irrelevant or wrong-workflow material"
    if "ocr_empty" in warnings or "sparse" in warnings or "failed" in warnings:
        return "weak", "extraction warning or low-quality OCR"
    return "active", ""


def _status_from_evidence(evidence: Any, metadata: dict[str, Any]) -> str:
    classification = str(metadata.get("classification") or "").lower()
    if classification in {"prompt_injection", "irrelevant"}:
        return "quarantined" if classification == "prompt_injection" else "excluded"
    if classification in {"wrong_workflow", "policy_guidance", "cross_case_sample", "mixed_case_document"}:
        return "excluded"
    if classification in {"process_only", "unclear"}:
        return "weak"
    credibility = str(getattr(evidence, "credibility", "") or "").lower()
    completeness = str(metadata.get("field_completeness") or "").lower()
    if credibility == "low" or completeness in {"minimal", "partial"}:
        return "weak"
    supports = list(getattr(evidence, "supports", []) or [])
    if not supports:
        return "weak"
    return "active"


def _reason_from_evidence(evidence: Any, metadata: dict[str, Any]) -> str:
    for value in (
        metadata.get("acceptance_rubric"),
        getattr(evidence, "reviewer_notes", ""),
        getattr(evidence, "summary", ""),
    ):
        text = _brief(value, 360)
        if text:
            return text
    return ""


def _evidence_match_probes(evidence: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": metadata.get("attachment_id") or "",
        "original_ref": metadata.get("original_ref") or "",
        "source_filename": metadata.get("source_filename") or "",
        "preview_paths": metadata.get("preview_paths") or [],
        "source_doc_id": metadata.get("source_doc_id") or getattr(evidence, "id", ""),
        "name": metadata.get("source_filename") or metadata.get("filename") or "",
    }


def _has_precise_attachment_identity(probe: dict[str, Any]) -> bool:
    return any(str(probe.get(key) or "") for key in ("attachment_id", "original_ref", "source_filename"))


def _find_precise_manifest_entry(manifest: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any] | None:
    identity = {
        "attachment_id": str(probe.get("attachment_id") or ""),
        "original_ref": str(probe.get("original_ref") or ""),
        "name": str(probe.get("source_filename") or ""),
    }
    if not any(identity.values()):
        return None
    matches = [
        item
        for item in manifest.get("attachments") or []
        if isinstance(item, dict)
        and all(not value or str(item.get(key) or "") == value for key, value in identity.items())
    ]
    return matches[0] if len(matches) == 1 else None


def _find_manifest_entry(manifest: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any] | None:
    attachments = [item for item in manifest.get("attachments") or [] if isinstance(item, dict)]
    wanted_id = str(probe.get("attachment_id") or "")
    wanted_ref = str(probe.get("original_ref") or "")
    wanted_name = str(probe.get("name") or probe.get("source_doc_id") or "")
    preview_paths = {str(path) for path in probe.get("preview_paths") or [] if str(path)}
    for item in attachments:
        if wanted_id and item.get("attachment_id") == wanted_id:
            return item
        if wanted_ref and item.get("original_ref") == wanted_ref:
            return item
        if preview_paths and preview_paths.intersection({str(path) for path in item.get("preview_paths") or []}):
            return item
        if wanted_name and wanted_name in {str(item.get("name") or ""), str(item.get("attachment_id") or "")}:
            return item
    return None


def _find_manifest_entry_by_evidence_text(
    manifest: dict[str, Any],
    evidence: Any,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    evidence_text = _search_text(
        {
            "type": getattr(evidence, "type", ""),
            "summary": getattr(evidence, "summary", ""),
            "content": getattr(evidence, "content", ""),
            "reviewer_notes": getattr(evidence, "reviewer_notes", ""),
            "quoted_text": getattr(evidence, "quoted_text", []),
            "supports": getattr(evidence, "supports", []),
            "conflicts": getattr(evidence, "conflicts", []),
            "metadata": metadata,
        }
    )
    evidence_tokens = _strong_match_tokens(evidence_text)
    evidence_kind = _material_kind(evidence_text)
    if not evidence_tokens and not evidence_kind:
        return None

    best_entry: dict[str, Any] | None = None
    best_score = 0
    for entry in manifest.get("attachments") or []:
        if not isinstance(entry, dict):
            continue
        entry_text = _search_text(
            {
                "name": entry.get("name", ""),
                "original_ref": entry.get("original_ref", ""),
                "summary": entry.get("summary", ""),
                "key_facts": entry.get("key_facts", []),
                "field_inventory": entry.get("field_inventory", []),
                "extraction_ref": entry.get("extraction_ref", ""),
            }
        )
        entry_tokens = _strong_match_tokens(entry_text)
        token_hits = len(evidence_tokens.intersection(entry_tokens))
        kind_match = bool(evidence_kind and evidence_kind == _material_kind(entry_text))
        score = token_hits + (3 if kind_match else 0)
        if score > best_score and (kind_match or token_hits >= 2):
            best_score = score
            best_entry = entry
    return best_entry


def _search_text(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return " ".join(f"{key} {_search_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_search_text(item) for item in value)
    return str(value or "")


def _strong_match_tokens(text: str) -> set[str]:
    upper = str(text or "").upper()
    tokens = set(re.findall(r"\b[A-Z]{2,}[-_A-Z0-9]{2,}\b", upper))
    tokens.update(re.findall(r"\b[A-Z]{1,6}-\d[A-Z0-9_-]*\b", upper))
    tokens.update(re.findall(r"\b\d{6,}\b", upper))
    return {token.strip("_-") for token in tokens if len(token.strip("_-")) >= 4}


def _material_kind(text: str) -> str:
    lower = str(text or "").lower()
    checks = (
        ("duplicate_payment_screen", ("duplicate_payment", "duplicate payment", "duplicate_check", "duplicate check", "historical_payment", "clearing_doc")),
        ("goods_receipt_or_service_acceptance", ("goods_receipt", "goods receipt", "grn", "receipt")),
        ("purchase_order", ("purchase_order", "purchase order", "po_id", "po id", "po-")),
        ("vendor_identity", ("vendor_record", "vendor record", "vendor_id", "supplier_name", "bank account", "payment_block")),
        ("invoice", ("invoice", "facture", "factu")),
    )
    for kind, markers in checks:
        if any(marker in lower for marker in markers):
            return kind
    return ""


def _context_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": item.get("attachment_id", ""),
        "name": item.get("name", ""),
        "content_kind": item.get("content_kind", ""),
        "status": item.get("status", ""),
        "summary": _brief(item.get("summary"), 360),
        "key_facts": _brief_list(item.get("key_facts"), 6, 160),
        "risks": _brief_list(item.get("risks"), 4, 180),
        "reason": _brief(item.get("reason"), 220),
        "original_ref": item.get("original_ref", ""),
        "preview_paths": list(item.get("preview_paths") or [])[:2],
        "extraction_ref": item.get("extraction_ref", ""),
        "field_inventory": _brief_records(item.get("field_inventory"), 12),
        "page_summaries": _compact_page_summaries(item.get("page_summaries"), 5),
        "quality_notes": _brief_list(item.get("quality_notes"), 6, 180),
        "extraction_methods": list(item.get("extraction_methods") or [])[:8],
        "visual_regions": _brief_records(item.get("visual_regions"), 6),
        "visual_check": _compact_visual_check(item.get("visual_check")),
        "block_crops": _brief_records(item.get("block_crops"), 18),
        "table_count": item.get("table_count", 0),
        "evidence_ids": list(item.get("evidence_ids") or [])[:6],
        "chars": item.get("chars", 0),
        "extraction_method": item.get("extraction_method", ""),
        "visual_notes": _brief_list(item.get("visual_notes"), 4, 180),
        "turn_id": item.get("turn_id", ""),
        "run_id": item.get("run_id", ""),
        "artifact_ref": item.get("artifact_ref", ""),
    }


def _context_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    status = str(item.get("status") or "")
    priority = {"active": 0, "weak": 1, "excluded": 2, "quarantined": 3, "error": 4}.get(status, 5)
    return (priority, str(item.get("updated_at") or ""))


def _merge_status(existing: str, new: str) -> str:
    if not existing:
        return new or "active"
    if new == "error" and existing != "error":
        return existing
    if existing == "weak" and new == "active":
        return "active"
    order = {"quarantined": 5, "excluded": 4, "weak": 3, "active": 2, "error": 1}
    return existing if order.get(existing, 0) >= order.get(new, 0) else new


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"active": 0, "weak": 0, "excluded": 0, "quarantined": 0, "error": 0}
    for item in items:
        status = str(item.get("status") or "active")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _source_sha256(store: Any, case_id: str, original_ref: str, item: dict[str, Any]) -> str:
    path: Path | None = None
    if original_ref:
        try:
            path = store.resolve_case_path(case_id, original_ref)
        except Exception:
            path = None
    if path is None:
        raw_path = str(item.get("path") or "")
        path = Path(raw_path) if raw_path else None
    if path and path.exists() and path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    content = str(item.get("content") or "")
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


def _attachment_id(basis: str, sha256: str) -> str:
    digest = hashlib.sha1(f"{basis}:{sha256}".encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"att_{digest}"


def _fallback_summary(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "attachment")
    status = str(item.get("status") or "success")
    kind = str(item.get("content_kind") or "unknown")
    chars = int(item.get("chars") or 0)
    return f"{name}: status={status}; kind={kind}; chars={chars}; original_ref={item.get('original_ref') or ''}"


def _fallback_key_facts(item: dict[str, Any]) -> list[str]:
    facts = [
        f"name={item.get('name') or ''}",
        f"content_kind={item.get('content_kind') or ''}",
        f"extraction_method={item.get('extraction_method') or ''}",
        f"chars={item.get('chars') or 0}",
    ]
    if item.get("original_ref"):
        facts.append(f"original_ref={item.get('original_ref')}")
    if item.get("extraction_ref"):
        facts.append(f"extraction_ref={item.get('extraction_ref')}")
    for field in item.get("field_inventory") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("field") or "")
        value = str(field.get("value") or "")
        if name and value:
            facts.append(f"{name}={_brief(value, 80)}")
        if len(facts) >= 12:
            break
    for note in _brief_list(item.get("visual_notes"), 3, 160):
        facts.append(f"visual_note={note}")
    visual_check = item.get("visual_check") if isinstance(item.get("visual_check"), dict) else {}
    if visual_check.get("looks_like_invoice"):
        facts.append(f"looks_like_invoice={visual_check.get('looks_like_invoice')}")
    return [fact for fact in facts if not fact.endswith("=")]


def _fallback_risks(item: dict[str, Any]) -> list[str]:
    risks = _source_quality_notes(item.get("warnings"))
    status = str(item.get("status") or "")
    if status == "error":
        risks.append("attachment_read_error")
    return _unique_strings(risks)[:8]


def _source_quality_notes(value: Any) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [str(item) for item in items if str(item).strip() and not _is_context_delivery_note(item)]


def _is_context_delivery_note(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "tool_response_excerpt_truncated",
            "content_is_excerpt_for_model_context",
            "attachment_text_was_truncated_in_tool_response",
            "content_preview_truncated",
            "context excerpt",
            "context window",
            "summary_compaction",
            "large_string_head_tail",
            "dossier keeps the full extracted text",
        )
    )


def _has_prompt_injection(text: str) -> bool:
    compact = text.lower().replace(" ", "")
    markers = (
        "ignorepreviousrules",
        "ignoreallprevious",
        "approvepayment",
        "submiterp",
        "promptinjection",
        "systemprompt",
    )
    return any(marker in compact for marker in markers)


def _brief(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + "...[excerpt]"


def _brief_list(value: Any, limit: int, max_chars: int) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [_brief(item, max_chars) for item in items[:limit] if _brief(item, max_chars)]


def _brief_records(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        record: dict[str, Any] = {}
        for key, field_value in item.items():
            if isinstance(field_value, str):
                record[str(key)] = _brief(field_value, 260)
            elif isinstance(field_value, (int, float, bool)) or field_value is None:
                record[str(key)] = field_value
            elif isinstance(field_value, list):
                record[str(key)] = field_value[:8]
            elif isinstance(field_value, dict):
                record[str(key)] = {
                    str(child_key): _brief(child_value, 160) if isinstance(child_value, str) else child_value
                    for child_key, child_value in list(field_value.items())[:8]
                }
            else:
                record[str(key)] = _brief(field_value, 160)
        records.append(record)
    return records


def _compact_visual_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "looks_like_invoice": value.get("looks_like_invoice", ""),
        "visible_sections": value.get("visible_sections") or {},
        "field_visibility": _brief_records(value.get("field_visibility"), 12),
        "same_source_check": value.get("same_source_check") or {},
        "ocr_quality": value.get("ocr_quality") or {},
        "page_integrity": value.get("page_integrity") or {},
        "layout_check": value.get("layout_check") or {},
        "limitations": _brief_list(value.get("limitations"), 6, 120),
    }


def _compact_page_summaries(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "page": item.get("page"),
                "block_count": item.get("block_count", 0),
                "table_count": item.get("table_count", 0),
                "preview_path": item.get("preview_path", ""),
                "quality_notes": _brief_list(item.get("quality_notes"), 4, 160),
            }
        )
    return records


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
