from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.config import Settings


_CURRENT_ROOT: ContextVar[Any | None] = ContextVar("invoice_agent_langfuse_root", default=None)


@dataclass
class LangfuseTurn:
    status: str = "disabled"
    trace_id: str = ""
    trace_url: str = ""
    root_observation_id: str = ""
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "langfuse": {
                "status": self.status,
                "trace_id": self.trace_id,
                "trace_url": self.trace_url,
                "root_observation_id": self.root_observation_id,
                "warning": self.warning,
            }
        }


class ObservationHandle:
    def __init__(self, observation: Any | None = None) -> None:
        self.observation = observation

    def update(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        usage_details: dict[str, int] | None = None,
        cost_details: dict[str, float] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        _update_observation(
            self.observation,
            output=output,
            metadata=metadata,
            usage_details=usage_details,
            cost_details=cost_details,
            level=level,
            status_message=status_message,
        )


class LangfuseTracer:
    def __init__(
        self,
        *,
        client: Any | None = None,
        enabled: bool = False,
        capture_payloads: str = "summary",
        warning: str = "",
    ) -> None:
        self.client = client
        self._enabled = enabled and client is not None
        self.capture_payloads = "full" if str(capture_payloads).lower() == "full" else "summary"
        self.warning = warning
        self._prompt_cache: dict[tuple[str, str], Any] = {}

    @classmethod
    def disabled(cls, warning: str = "") -> "LangfuseTracer":
        return cls(warning=warning)

    @classmethod
    def from_settings(cls, settings: Settings) -> "LangfuseTracer":
        if not settings.enable_langfuse:
            return cls.disabled()
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            return cls.disabled("Langfuse enabled but LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is missing.")
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_BASE_URL"] = settings.langfuse_base_url
        try:
            from langfuse import get_client
        except Exception as exc:
            return cls.disabled(f"Langfuse SDK unavailable: {type(exc).__name__}: {exc}")
        try:
            return cls(
                client=get_client(),
                enabled=True,
                capture_payloads=settings.langfuse_capture_payloads,
            )
        except Exception as exc:
            return cls.disabled(f"Langfuse client initialization failed: {type(exc).__name__}: {exc}")

    def enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def start_turn(
        self,
        *,
        case_id: str,
        run_id: str,
        session_id: str = "",
        turn_id: str = "",
        message_summary: str = "",
        attachments: list[Any] | None = None,
        max_steps: int = 0,
        model: str = "",
        workspace_root_hash: str = "",
    ) -> Iterator[LangfuseTurn]:
        if not self._enabled:
            yield LangfuseTurn(warning=self.warning)
            return
        input_payload = {
            "case_id": case_id,
            "run_id": run_id,
            "message_summary": _preview(message_summary),
            "attachments": _attachment_refs(attachments or []),
        }
        metadata = {
            "case_id": case_id,
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "max_steps": max_steps,
            "model": model,
            "workspace_root_hash": workspace_root_hash,
            "capture_payloads": self.capture_payloads,
        }
        try:
            manager = self.client.start_as_current_observation(
                as_type="span",
                name="invoice_agent.turn",
                input=input_payload,
                metadata=_clean(metadata),
            )
        except Exception as exc:
            yield LangfuseTurn(status="error", warning=f"{type(exc).__name__}: {exc}")
            return
        try:
            with manager as root:
                token = _CURRENT_ROOT.set(root)
                turn = LangfuseTurn(
                    status="enabled",
                    trace_id=str(getattr(root, "trace_id", "") or ""),
                    trace_url=_trace_url(self.client, root),
                    root_observation_id=str(getattr(root, "id", "") or ""),
                )
                with _propagate_context(
                    session_id=session_id,
                    metadata=_string_metadata(_clean(metadata)),
                    trace_name="invoice_agent.turn",
                ):
                    try:
                        yield turn
                    except Exception as exc:
                        _update_observation(root, level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
                        raise
                    finally:
                        _CURRENT_ROOT.reset(token)
        finally:
            self.flush()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
        as_type: str = "span",
    ) -> Iterator[ObservationHandle]:
        if not self._enabled:
            yield ObservationHandle()
            return
        with self._observation(name=name, as_type=as_type, input=input, metadata=metadata) as handle:
            yield handle

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
        prompt: Any | None = None,
    ) -> Iterator[ObservationHandle]:
        if not self._enabled:
            yield ObservationHandle()
            return
        with self._observation(
            name=name,
            as_type="generation",
            input=input,
            metadata=metadata,
            model=model,
            prompt=prompt,
        ) as handle:
            yield handle

    def managed_prompt(
        self,
        *,
        role: str,
        prompt_version: str,
        system_prompt: str,
        model: str,
    ) -> Any | None:
        if not self._enabled or not system_prompt:
            return None
        prompt_hash = _sha256(system_prompt)
        name = _prompt_name(role)
        cache_key = (name, prompt_hash)
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key]
        prompt = self._fetch_or_create_prompt(
            name=name,
            role=role,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            prompt_hash=prompt_hash,
            model=model,
        )
        if prompt is not None:
            self._prompt_cache[cache_key] = prompt
        return prompt

    def finish_turn(
        self,
        *,
        final_answer: str,
        case_status: str,
        step_count: int,
        phase: str,
        feedback_summary: dict[str, Any],
        report_paths: list[str],
        guard_retry_count: int,
    ) -> None:
        root = _CURRENT_ROOT.get()
        if not self._enabled or root is None:
            return
        output = {
            "final_answer_preview": _preview(final_answer),
            "case_status": case_status,
            "step_count": step_count,
            "phase": phase,
            "feedback_summary": feedback_summary,
            "report_paths": report_paths,
            "guard_retry_count": guard_retry_count,
        }
        _update_observation(root, output=output)
        self.score("final_answer_present", 1.0 if final_answer else 0.0)
        self.score("report_generated", 1.0 if report_paths else 0.0)
        self.score("case_ready_for_report", 1.0 if case_status == "ready_for_report" else 0.0)
        self.score("guard_retry_count", float(guard_retry_count))

    def score(self, name: str, value: float, *, comment: str = "") -> None:
        if not self._enabled:
            return
        scorer = getattr(self.client, "score_current_trace", None)
        if callable(scorer):
            try:
                scorer(name=name, value=value, data_type="NUMERIC", comment=comment or None)
            except Exception:
                return

    def flush(self) -> None:
        if not self._enabled:
            return
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                return

    @contextmanager
    def _observation(
        self,
        *,
        name: str,
        as_type: str,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        prompt: Any | None = None,
    ) -> Iterator[ObservationHandle]:
        kwargs = {
            "as_type": as_type,
            "name": name,
            "input": input,
            "metadata": _clean(metadata or {}),
            "model": model,
        }
        if prompt is not None:
            kwargs["prompt"] = prompt
        manager = self.client.start_as_current_observation(**kwargs)
        with manager as observation:
            handle = ObservationHandle(observation)
            try:
                yield handle
            except Exception as exc:
                handle.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
                raise

    def _fetch_or_create_prompt(
        self,
        *,
        name: str,
        role: str,
        prompt_version: str,
        system_prompt: str,
        prompt_hash: str,
        model: str,
    ) -> Any | None:
        existing = None
        get_prompt = getattr(self.client, "get_prompt", None)
        if callable(get_prompt):
            try:
                existing = get_prompt(
                    name,
                    type="text",
                    label="production",
                    cache_ttl_seconds=0,
                    fetch_timeout_seconds=10,
                )
            except Exception:
                existing = None
        if existing is not None and _prompt_text(existing) == system_prompt:
            return existing
        create_prompt = getattr(self.client, "create_prompt", None)
        if not callable(create_prompt):
            return existing
        try:
            return create_prompt(
                name=name,
                type="text",
                prompt=system_prompt,
                labels=["production"],
                tags=["invoice-agent", role],
                config={
                    "role": role,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt_hash,
                    "model": model,
                    "source": "local_system_prompt",
                },
                commit_message=f"sync {role} {prompt_version} {prompt_hash[:12]}",
            )
        except Exception:
            return existing


def generation_input(role: str, prompt_version: str, system_prompt: str, payload: dict[str, Any], *, mode: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": role,
        "prompt_version": prompt_version,
        "payload": build_langfuse_input(role=role, payload=payload or {}, mode=mode),
    }
    if mode == "full":
        result["raw_payload_local_only"] = True
    return result


def generation_output(raw_response: str, *, parsed_schema: str = "", error: str = "", mode: str = "summary") -> dict[str, Any]:
    parsed = _try_parse_json(raw_response)
    result: dict[str, Any] = {
        "parsed": sanitize_for_langfuse(parsed) if parsed is not None else None,
        "raw_preview": _preview(_redact_local_paths(raw_response), 1200),
        "schema": parsed_schema,
        "error": error,
    }
    if mode == "full":
        result["raw_response"] = _preview(_redact_local_paths(raw_response), 20000)
    return _clean(result)


def generation_hash_metadata(system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload_text = json.dumps(payload or {}, ensure_ascii=False, default=str, sort_keys=True)
    return {
        "prompt_sha256": _sha256(system_prompt),
        "payload_sha256": _sha256(payload_text),
        "raw_payload_local_only": True,
    }


def build_langfuse_input(role: str, payload: dict[str, Any], *, mode: str = "summary") -> dict[str, Any]:
    role_name = str(role or "").removesuffix("_retry")
    if role_name == "planner":
        result = _compact_planner_input(payload)
    elif role_name == "session_compactor":
        result = _compact_session_compactor_input(payload)
    elif role_name == "evidence_reviewer":
        result = _compact_evidence_reviewer_input(payload)
    else:
        result = _compact_generic_input(role_name, payload)
    if mode == "full":
        result["full_payload_summary_only"] = sanitize_for_langfuse(payload)
    return _clean(result)


def usage_details(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens", 0) or 0
    result = {
        "input": int(prompt),
        "output": int(completion),
        "total": int(total),
    }
    return {key: value for key, value in result.items() if value}


def cost_details(
    usage: dict[str, Any] | None,
    *,
    input_cost_per_1m: float = 0.0,
    output_cost_per_1m: float = 0.0,
    cached_input_cost_per_1m: float = 0.0,
) -> dict[str, float]:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    cached = max(0, min(cached, prompt))
    uncached = max(prompt - cached, 0)
    input_rate = max(float(input_cost_per_1m or 0.0), 0.0)
    output_rate = max(float(output_cost_per_1m or 0.0), 0.0)
    cached_rate = max(float(cached_input_cost_per_1m or 0.0), 0.0)
    if not input_rate and not output_rate and not cached_rate:
        return {}
    input_cost = (uncached * input_rate) / 1_000_000
    cached_input_cost = (cached * cached_rate) / 1_000_000 if cached else 0.0
    output_cost = (completion * output_rate) / 1_000_000
    total = input_cost + cached_input_cost + output_cost
    result = {
        "input": round(input_cost + cached_input_cost, 10),
        "output": round(output_cost, 10),
        "total": round(total, 10),
    }
    if cached:
        result["cached_input"] = round(cached_input_cost, 10)
    return {key: value for key, value in result.items() if value}


def safe_tool_input(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = {
        "tool": tool,
        "input_preview": _preview(sanitize_for_langfuse(payload)),
    }
    if "attachments" in payload:
        result["attachments_count"] = len(payload.get("attachments") or [])
    for key in ("content_ref", "attachment_id", "original_ref", "relative_path", "markdown_path", "pdf_path"):
        if payload.get(key):
            result[key] = sanitize_for_langfuse(payload.get(key))
    return result


def safe_tool_output(result: dict[str, Any] | None, *, error: str = "") -> dict[str, Any]:
    result = result or {}
    output: dict[str, Any] = {
        "result_preview": _preview(sanitize_for_langfuse(result)),
        "error": error,
    }
    for key in ("artifact_ref", "manifest_ref", "attachment_manifest", "relative_path", "markdown_path", "pdf_path"):
        value = result.get(key)
        if isinstance(value, str):
            output[key] = sanitize_for_langfuse(value)
        elif isinstance(value, dict):
            output[key] = sanitize_for_langfuse(
                {k: value.get(k) for k in ("path", "relative_path", "attachment_count") if k in value}
            )
    if isinstance(result.get("attachments"), list):
        output["attachments_count"] = len(result["attachments"])
        warnings = [item.get("warnings") for item in result["attachments"] if isinstance(item, dict) and item.get("warnings")]
        if warnings:
            output["attachment_warnings_count"] = len(warnings)
    return _clean(output)


def safe_role_input(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "input_keys": sorted((payload or {}).keys()), "input_preview": _preview(sanitize_for_langfuse(payload))}


def safe_role_output(result: Any, *, error: str = "") -> dict[str, Any]:
    return _clean({"result_preview": _preview(sanitize_for_langfuse(result)), "error": error})


def sanitize_for_langfuse(value: Any, *, max_list: int = 50, max_string: int = 2000) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in {
                "content",
                "raw_content",
                "raw_text",
                "full_text",
                "ocr_text",
                "raw_response_full",
                "system_prompt",
            }:
                sanitized[key_text] = _omitted_payload(item)
                continue
            if _is_path_key(lowered):
                sanitized[key_text] = _path_ref(str(item))
                continue
            sanitized[key_text] = sanitize_for_langfuse(item, max_list=max_list, max_string=max_string)
        return sanitized
    if isinstance(value, list):
        items = [sanitize_for_langfuse(item, max_list=max_list, max_string=max_string) for item in value[:max_list]]
        if len(value) > max_list:
            items.append({"truncated_items": len(value) - max_list})
        return items
    if isinstance(value, str):
        return _preview(_redact_local_paths(value), max_string)
    return value


def _compact_planner_input(payload: dict[str, Any]) -> dict[str, Any]:
    case = _case_source(payload)
    context_pack = _dict(payload.get("context_pack"))
    observations = payload.get("recent_observations") or context_pack.get("recent_observations") or []
    attachments = payload.get("attachments") or context_pack.get("attachments") or []
    memory = payload.get("memory_hints") or context_pack.get("memory_hints") or []
    return {
        "role": "planner",
        "user_message": _safe_text(payload.get("user_message") or context_pack.get("user_message"), 300),
        "case": _case_counts(case),
        "case_brief": _safe_text(payload.get("case_brief") or context_pack.get("case_brief") or case.get("summary"), 320),
        "next_expected_action": payload.get("next_expected_action") or context_pack.get("next_expected_action"),
        "attachments_count": len(attachments) if isinstance(attachments, list) else 0,
        "recent_observations": [_compact_observation(item) for item in observations[:8] if isinstance(item, dict)],
        "memory_hit_count": len(memory) if isinstance(memory, list) else 0,
        "report_paths": sanitize_for_langfuse(payload.get("report_paths") or context_pack.get("report_paths") or {}),
    }


def _compact_session_compactor_input(payload: dict[str, Any]) -> dict[str, Any]:
    case = _dict(payload.get("case_snapshot") or payload.get("case_state"))
    turns_to_compact = payload.get("turns_to_compact") or []
    recent_turns = payload.get("recent_turns_kept") or payload.get("recent_turns") or []
    rules = _dict(payload.get("compact_rules"))
    return {
        "role": "session_compactor",
        "case": _case_counts(case),
        "compaction": {
            "reason": payload.get("reason"),
            "estimated_context_chars": payload.get("estimated_context_chars"),
            "context_char_limit": payload.get("context_char_limit"),
            "recent_turns_kept_count": rules.get("recent_turns_kept_count"),
            "summary_max_chars": rules.get("summary_max_chars"),
            "turns_to_compact_count": len(turns_to_compact) if isinstance(turns_to_compact, list) else 0,
            "recent_turns_kept_count_actual": len(recent_turns) if isinstance(recent_turns, list) else 0,
        },
        "turns_to_compact": [_compact_turn(item) for item in turns_to_compact[:8] if isinstance(item, dict)],
        "recent_turns_kept": [_compact_turn(item) for item in recent_turns[:3] if isinstance(item, dict)],
    }


def _compact_evidence_reviewer_input(payload: dict[str, Any]) -> dict[str, Any]:
    case = _case_source(payload)
    attachment_context = payload.get("attachment_context") or payload.get("attachments") or []
    extraction = _dict(payload.get("extraction_result") or payload.get("last_extraction_result"))
    fields = extraction.get("extracted_fields") or extraction.get("field_inventory") or payload.get("extracted_fields") or {}
    evidence_cards = payload.get("evidence_cards") or extraction.get("evidence_cards") or []
    risks = payload.get("risk_flags") or extraction.get("risk_flags") or []
    docs = extraction.get("source_docs") or extraction.get("documents") or []
    return {
        "role": "evidence_reviewer",
        "mode": payload.get("mode") or payload.get("review_mode") or "review",
        "case": _case_counts(case),
        "requirements": _requirement_summary(case.get("requirements") or payload.get("requirements") or []),
        "attachment_count": len(attachment_context) if isinstance(attachment_context, list) else 0,
        "source_doc_count": len(docs) if isinstance(docs, list) else 0,
        "field_count": len(fields) if isinstance(fields, (dict, list)) else 0,
        "risk_count": len(risks) if isinstance(risks, list) else 0,
        "evidence_card_count": len(evidence_cards) if isinstance(evidence_cards, list) else 0,
        "refs": _collect_refs(payload),
    }


def _compact_generic_input(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    case = _case_source(payload)
    return {
        "role": role or "unknown",
        "input_keys": sorted((payload or {}).keys()),
        "case": _case_counts(case),
        "refs": _collect_refs(payload),
    }


def _case_source(payload: dict[str, Any]) -> dict[str, Any]:
    context_pack = _dict(payload.get("context_pack"))
    return _dict(
        payload.get("case_state")
        or payload.get("case_snapshot")
        or payload.get("case")
        or context_pack.get("case_state")
        or context_pack.get("case")
    )


def _case_counts(case: dict[str, Any]) -> dict[str, Any]:
    evidence = case.get("evidence_items") or case.get("evidence") or []
    requirements = case.get("requirements") or []
    risks = case.get("risk_flags") or case.get("risks") or []
    return {
        "case_id": case.get("case_id") or case.get("id"),
        "status": case.get("status"),
        "summary": _safe_text(case.get("summary"), 240),
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        "requirement_count": len(requirements) if isinstance(requirements, list) else 0,
        "risk_count": len(risks) if isinstance(risks, list) else 0,
        "missing_count": len(case.get("missing_materials") or []) if isinstance(case.get("missing_materials"), list) else 0,
        "weak_count": len(case.get("weak_materials") or []) if isinstance(case.get("weak_materials"), list) else 0,
        "conflict_count": len(case.get("conflict_materials") or []) if isinstance(case.get("conflict_materials"), list) else 0,
    }


def _requirement_summary(requirements: Any) -> list[dict[str, Any]]:
    if not isinstance(requirements, list):
        return []
    result: list[dict[str, Any]] = []
    for item in requirements[:20]:
        if isinstance(item, dict):
            result.append(
                {
                    "id": item.get("id") or item.get("requirement"),
                    "status": item.get("status"),
                    "required": item.get("required"),
                    "evidence_ids": item.get("evidence_ids") or item.get("evidence"),
                }
            )
    if len(requirements) > 20:
        result.append({"truncated_items": len(requirements) - 20})
    return result


def _compact_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": item.get("kind"),
        "name": item.get("name"),
        "summary": _safe_text(item.get("summary"), 240),
        "next_action_hint": item.get("next_action_hint"),
        "artifact_ref": item.get("artifact_ref") or item.get("content_ref"),
        "error_type": _dict(item.get("runtime_feedback")).get("error_type"),
    }


def _compact_turn(turn: dict[str, Any]) -> dict[str, Any]:
    attachments = turn.get("attachments") or []
    return {
        "turn_id": turn.get("turn_id"),
        "user_summary": _safe_text(turn.get("user_message_summary") or turn.get("user_message"), 220),
        "assistant_summary": _safe_text(turn.get("assistant_summary") or turn.get("assistant_message"), 220),
        "attachments": [_attachment_item_ref(item) for item in attachments[:6] if isinstance(item, dict)],
        "run_ids": turn.get("run_ids") or ([turn.get("run_id")] if turn.get("run_id") else []),
    }


def _attachment_item_ref(item: dict[str, Any]) -> dict[str, Any]:
    path = str(item.get("path") or item.get("source_path") or "")
    return {
        "name": item.get("name") or (Path(path).name if path else ""),
        "content_type": item.get("content_type") or item.get("mime_type"),
        "path_hash": _sha256(path)[:12] if path else "",
    }


def _collect_refs(value: Any) -> dict[str, Any]:
    refs: dict[str, list[Any]] = {"artifact_refs": [], "dossier_refs": [], "preview_count": []}
    _collect_refs_walk(value, refs)
    return {
        "artifact_refs": refs["artifact_refs"][:12],
        "dossier_refs": refs["dossier_refs"][:12],
        "preview_count": sum(count for count in refs["preview_count"] if isinstance(count, int)),
    }


def _collect_refs_walk(value: Any, refs: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"artifact_ref", "content_ref", "manifest_ref"} and isinstance(item, str):
                refs["artifact_refs"].append(item)
            elif lowered in {"dossier_ref", "extraction_ref"} and isinstance(item, str):
                refs["dossier_refs"].append(item)
            elif lowered == "preview_paths" and isinstance(item, list):
                refs["preview_count"].append(len(item))
            else:
                _collect_refs_walk(item, refs)
    elif isinstance(value, list):
        for item in value[:50]:
            _collect_refs_walk(item, refs)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_text(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    return _preview(_redact_local_paths(str(value)), max_chars)


def _try_parse_json(raw_response: str) -> Any | None:
    text = (raw_response or "").strip()
    if not text:
        return None
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        return None


def _is_path_key(lowered: str) -> bool:
    return lowered in {"path", "source_path", "original_path", "absolute_path"} or lowered.endswith("_path")


def _path_ref(path: str) -> dict[str, str] | str:
    if not path:
        return ""
    if not _looks_like_local_path(path):
        return _preview(path, 800)
    return {
        "name": Path(path).name,
        "path_kind": "local_path",
        "path_hash": _sha256(path)[:12],
    }


def _omitted_payload(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {"omitted": True, "sha256": _sha256(text), "chars": len(text)}


def _update_observation(observation: Any, **kwargs: Any) -> None:
    if observation is None:
        return
    data = {key: value for key, value in kwargs.items() if value not in (None, "", {}, [])}
    if not data:
        return
    update = getattr(observation, "update", None)
    if callable(update):
        update(**data)


def _trace_url(client: Any, observation: Any) -> str:
    trace_id = str(getattr(observation, "trace_id", "") or "")
    getter = getattr(client, "get_trace_url", None)
    if callable(getter):
        try:
            return str(getter(trace_id=trace_id) or "")
        except Exception:
            return ""
    return ""


def _prompt_name(role: str) -> str:
    safe_role = str(role or "unknown").strip().replace("_", "-")
    return f"invoice-agent/{safe_role}"


def _prompt_text(prompt: Any) -> str:
    value = getattr(prompt, "prompt", "")
    if isinstance(value, str):
        return value
    return ""


@contextmanager
def _propagate_context(*, session_id: str, metadata: dict[str, Any], trace_name: str) -> Iterator[None]:
    try:
        from langfuse import propagate_attributes
    except Exception:
        yield
        return
    with propagate_attributes(
        session_id=session_id or None,
        metadata=metadata,
        tags=["invoice-agent"],
        trace_name=trace_name,
    ):
        yield


def _attachment_refs(attachments: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in attachments[:10]:
        path = str(getattr(item, "path", "") or "")
        result.append(
            {
                "name": str(getattr(item, "name", "") or ""),
                "path_hash": _sha256(path)[:12] if path else "",
                "content_type": str(getattr(item, "content_type", "") or ""),
            }
        )
    return result


def _preview(value: Any, max_chars: int = 1600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:max_chars]


def _redact_local_paths(text: str) -> str:
    if not text:
        return ""
    pattern = r"(?i)(?:[a-z]:\\|\\\\|/users/|/home/|/mnt/|/var/|/tmp/)[^\s\"'<>]+"
    return re.sub(pattern, lambda match: f"[local_path:{_sha256(match.group(0))[:12]}]", text)


def _looks_like_local_path(value: str) -> bool:
    text = str(value or "")
    return bool(re.search(r"(?i)^(?:[a-z]:\\|\\\\|/users/|/home/|/mnt/|/var/|/tmp/)", text))


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", {}, [])}


def _string_metadata(value: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()}
