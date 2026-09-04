from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agents.capabilities import ROLE_CAPABILITIES
from app.config import PROJECT_ROOT, Settings
from app.runtime.supervisor_contract import CAPABILITY_CARDS, sorted_specialist_tool_specs
from app.skills import SkillRegistry
from app.tools.catalog import ToolCatalog


TENANT_POLICY_VERSION = "invoice_workbench_policy_v1"
PROMPT_CACHE_NAMESPACE = "invoice_workbench"
VOLATILE_KEYS = {"case_id", "run_id", "turn_id", "user_id", "timestamp", "created_at", "updated_at", "ts"}
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
RUN_ID_RE = re.compile(r"\brun_[0-9a-f]{8,}\b", re.I)
PARTITION_TOP_LEVEL_KEYS = (
    "partition_contract",
    "stable_prefix_hash",
    "tool_catalog_hash",
    "skill_hash",
    "tenant_policy_hash",
    "dynamic_context_hash",
    "volatile_tail_hash",
    "prompt_cache_key",
    "prompt_tokens",
    "cached_tokens",
    "cache_hit_ratio",
)
PARTITION_USAGE_KEYS = ("prompt_tokens", "cached_tokens", "cache_hit_ratio")
PARTITION_RUNTIME_KEYS = PARTITION_TOP_LEVEL_KEYS + ("role", "prompt_version")
PARTITION_MANIFEST_KEYS = (
    "unknown_context_keys",
    "partition_policy_warnings",
    "stable_prefix_exclusion_checks",
    "dynamic_context_budget_chars",
    "volatile_tail_budget_chars",
)
DYNAMIC_CONTEXT_KEYS = (
    "case_brief",
    "case_profile",
    "case_next_action_hint",
    "reply_brief",
    "evidence_cards",
    "session_summary",
    "recent_turns",
    "memory_hints",
    "rag_context",
    "attachment_manifest",
    "open_questions",
    "canonical_consumer_packet",
    "role_result",
    "case_state",
    "evidence",
    "conversation_summary",
    "active_requirement_ids",
)
VOLATILE_TAIL_KEYS = (
    "current_goal",
    "current_plan",
    "attachments",
    "recent_observations",
    "next_expected_action",
    "runtime_feedback",
    "report_paths",
    "user_message",
    "user_question",
    "user_request",
    "mode",
    "note",
    "report_instructions",
    "supervisor_task",
    "attachment_context",
    "extraction_context",
    "extraction_result",
    "target_evidence_id",
    "target_attachment_id",
    "user_correction",
)


@dataclass(frozen=True)
class PartitionedContextPayload:
    dynamic_context: dict[str, Any]
    volatile_tail: dict[str, Any]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class ContextPacket:
    role: str
    prompt_version: str
    stable_prefix: dict[str, Any]
    dynamic_context: dict[str, Any]
    volatile_tail: dict[str, Any]
    stable_prefix_hash: str
    tool_catalog_hash: str
    skill_hash: str
    tenant_policy_hash: str
    dynamic_context_hash: str
    volatile_tail_hash: str
    prompt_cache_key: str
    partition_diagnostics: dict[str, Any]

    def runtime_metadata(self) -> dict[str, Any]:
        return {
            "partition_contract": "prompt_partition_v1",
            "stable_prefix_hash": self.stable_prefix_hash,
            "tool_catalog_hash": self.tool_catalog_hash,
            "skill_hash": self.skill_hash,
            "tenant_policy_hash": self.tenant_policy_hash,
            "dynamic_context_hash": self.dynamic_context_hash,
            "volatile_tail_hash": self.volatile_tail_hash,
            "prompt_cache_key": self.prompt_cache_key,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "cache_hit_ratio": 0.0,
            "role": self.role,
            "prompt_version": self.prompt_version,
        }

    def manifest_metadata(self) -> dict[str, Any]:
        metadata = self.runtime_metadata()
        metadata.update({key: self.partition_diagnostics.get(key) for key in PARTITION_MANIFEST_KEYS})
        metadata["partition_previews"] = {"stable_prefix": _stable_prefix_preview(self)}
        metadata["partition_sections"] = {
            "dynamic_context": _section_index(self.dynamic_context),
            "volatile_tail": _section_index(self.volatile_tail),
        }
        return metadata

    def debug_metadata(self) -> dict[str, Any]:
        return self.runtime_metadata()


def build_context_packet(
    *,
    role: str,
    prompt_version: str,
    prompt_file: str,
    system_prompt: str,
    context_payload: dict[str, Any],
    settings: Settings,
    output_model: type[BaseModel] | None = None,
    role_contract: dict[str, Any] | None = None,
    tool_catalog: ToolCatalog | None = None,
    skills: SkillRegistry | None = None,
) -> ContextPacket:
    normalized_role = _normalize_role(role)
    tool_catalog_payload = _tool_catalog_payload(tool_catalog or ToolCatalog(), role=normalized_role)
    skill_payload = _skill_payload(skills or SkillRegistry())
    tenant_policy_payload = _tenant_policy_payload(settings, skill_payload=skill_payload)
    tool_catalog_hash = stable_hash(tool_catalog_payload)
    skill_hash = stable_hash(skill_payload)
    tenant_policy_hash = stable_hash(tenant_policy_payload)
    partitioned = partition_context_payload(
        context_payload,
        strict=bool(getattr(settings, "strict_context_partition", False)),
    )
    dynamic_context = partitioned.dynamic_context
    volatile_tail = partitioned.volatile_tail
    stable_prefix = {
        "contract": "stable_prefix_v1",
        "role": normalized_role,
        "prompt_version": str(prompt_version or ""),
        "prompt_file": str(prompt_file or ""),
        "system_prompt": str(system_prompt or ""),
        "role_contract": role_contract or _role_contract(normalized_role, output_model=output_model),
        "output_schema": _output_schema(output_model),
        "capability_cards": _sorted_jsonable(CAPABILITY_CARDS),
        "tool_catalog": tool_catalog_payload,
        "skill_manifest": skill_payload,
        "tenant_policy": tenant_policy_payload,
    }
    _assert_stable_prefix(stable_prefix)
    stable_prefix_hash = stable_hash(stable_prefix)
    prompt_cache_key = (
        f"{PROMPT_CACHE_NAMESPACE}:{tenant_policy_hash[:16]}:{normalized_role}:{prompt_version}:{stable_prefix_hash[:24]}"
    )
    return ContextPacket(
        role=normalized_role,
        prompt_version=str(prompt_version or ""),
        stable_prefix=stable_prefix,
        dynamic_context=dynamic_context,
        volatile_tail=volatile_tail,
        stable_prefix_hash=stable_prefix_hash,
        tool_catalog_hash=tool_catalog_hash,
        skill_hash=skill_hash,
        tenant_policy_hash=tenant_policy_hash,
        dynamic_context_hash=stable_hash(dynamic_context),
        volatile_tail_hash=stable_hash(volatile_tail),
        prompt_cache_key=prompt_cache_key,
        partition_diagnostics={
            **partitioned.diagnostics,
            "stable_prefix_exclusion_checks": _stable_prefix_exclusion_checks(stable_prefix),
        },
    )


def partition_context_payload(context_payload: dict[str, Any], *, strict: bool = False) -> PartitionedContextPayload:
    payload = dict(context_payload or {})
    if "context_pack" in payload and isinstance(payload["context_pack"], dict):
        context = dict(payload["context_pack"])
        user_message = payload.get("user_message", "")
    else:
        context = payload
        user_message = payload.get("user_message", "")
    dynamic = {key: context[key] for key in DYNAMIC_CONTEXT_KEYS if key in context}
    volatile = {key: context[key] for key in VOLATILE_TAIL_KEYS if key in context}
    if user_message and "user_message" not in volatile:
        volatile["user_message"] = user_message
    other = {
        key: value
        for key, value in context.items()
        if key not in dynamic and key not in volatile
    }
    unknown_keys = sorted(str(key) for key in other)
    warnings: list[str] = []
    if other:
        warnings.append(f"unknown_context_keys:{','.join(unknown_keys)}")
        if strict:
            raise ValueError(f"Unknown prompt partition context keys: {unknown_keys}")
        dynamic["other_context"] = other
    dynamic = _sorted_jsonable(dynamic)
    volatile = _sorted_jsonable(volatile)
    return PartitionedContextPayload(
        dynamic_context=dynamic,
        volatile_tail=volatile,
        diagnostics=_partition_diagnostics(dynamic, volatile, unknown_keys, warnings),
    )


def prompt_cache_model_settings_kwargs(settings: Settings, partition: dict[str, Any] | ContextPacket | None) -> dict[str, Any]:
    if not _supports_openai_responses_prompt_cache(settings):
        return {}
    metadata = partition.runtime_metadata() if isinstance(partition, ContextPacket) else dict(partition or {})
    key = str(metadata.get("prompt_cache_key") or "").strip()
    if not key:
        return {}
    return {"prompt_cache_retention": "24h", "extra_args": {"prompt_cache_key": key}}


def with_usage_metrics(partition: dict[str, Any] | None, usage: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(partition or {})
    usage = usage or {}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    metadata["prompt_tokens"] = prompt_tokens
    metadata["cached_tokens"] = cached_tokens
    metadata["cache_hit_ratio"] = round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0.0
    return metadata


def runtime_partition_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in PARTITION_RUNTIME_KEYS if key in value}


def usage_from_result(result: Any) -> dict[str, Any]:
    """Sum reported usage, retaining zero and marking incomplete known totals."""
    responses = list(getattr(result, "raw_responses", []) or [])
    totals: dict[str, Any] = {}
    reported: dict[str, int] = {}
    for response in responses:
        usage = _model_dump(getattr(response, "usage", None))
        prompt_details = _model_dump(usage.get("input_tokens_details") or usage.get("prompt_tokens_details"))
        completion_details = _model_dump(usage.get("output_tokens_details") or usage.get("completion_tokens_details"))
        values = {
            "prompt_tokens": usage.get("input_tokens") if usage.get("input_tokens") is not None else usage.get("prompt_tokens"),
            "completion_tokens": usage.get("output_tokens") if usage.get("output_tokens") is not None else usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": prompt_details.get("cached_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
        }
        for key, value in values.items():
            if value is not None:
                totals[key] = totals.get(key, 0) + int(value)
                reported[key] = reported.get(key, 0) + 1
    partial = sorted(key for key, count in reported.items() if count < len(responses))
    if partial:
        totals["partial_metrics"] = partial
    return totals


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(_sorted_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_role(role: str) -> str:
    value = str(role or "").strip().removeprefix("role:")
    if value in {"case_manager", "manager", "supervisor"}:
        return "planner"
    return value or "planner"


def _tool_catalog_payload(catalog: ToolCatalog, *, role: str) -> dict[str, Any]:
    specialist_tools = [
        {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        }
        for name, description, model in sorted_specialist_tool_specs()
    ]
    workspace_tools = catalog.visible_tools()
    if role != "planner":
        workspace_tools = []
        specialist_tools = []
    return {"specialist_tools": specialist_tools, "workspace_tools": workspace_tools}


def _skill_payload(skills: SkillRegistry) -> dict[str, Any]:
    manifests = sorted(skills.visible_manifests(), key=lambda item: str(item.get("name") or ""))
    enriched = []
    for manifest in manifests:
        item = dict(manifest)
        item["instruction_file_hashes"] = _file_hashes(getattr(skills, "root", PROJECT_ROOT), item.get("instruction_files") or [])
        item["resource_root_hashes"] = _resource_root_hashes(getattr(skills, "root", PROJECT_ROOT), item.get("resource_roots") or [])
        enriched.append(item)
    return {"skills": enriched}


def _tenant_policy_payload(settings: Settings, *, skill_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": TENANT_POLICY_VERSION,
        "knowledge_roots": _knowledge_root_hashes(settings),
        "skill_hash": stable_hash(skill_payload),
    }


def _knowledge_root_hashes(settings: Settings) -> list[dict[str, str]]:
    rows = []
    for root in sorted(settings.knowledge_roots, key=lambda item: str(item)):
        path = Path(root)
        rows.extend(_resource_root_hashes(PROJECT_ROOT, [str(path)]))
    return rows


def _resource_root_hashes(root: Path, values: list[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in values:
        path = (root / str(value)).resolve() if not Path(str(value)).is_absolute() else Path(str(value)).resolve()
        if path.is_file():
            rows.append(_path_hash_record(path))
        elif path.is_dir():
            for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
                if child.is_file() and not child.name.startswith("."):
                    rows.append(_path_hash_record(child))
    return rows


def _file_hashes(root: Path, values: list[Any]) -> list[dict[str, str]]:
    rows = []
    for value in values:
        path = (root / str(value)).resolve()
        if path.is_file():
            rows.append(_path_hash_record(path))
    return rows


def _path_hash_record(path: Path) -> dict[str, str]:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    try:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        relative = path.name
    return {"path": relative, "sha256": digest}


def _role_contract(role: str, *, output_model: type[BaseModel] | None) -> dict[str, Any]:
    if role == "planner":
        return {
            "owner": "manager",
            "output_mode": "final_answer_or_tool_calls",
            "capability_cards": _sorted_jsonable(CAPABILITY_CARDS),
        }
    capability = ROLE_CAPABILITIES.get(role)
    if not capability:
        return {"role": role, "output_model": output_model.__name__ if output_model else ""}
    return capability.trace_metadata()


def _output_schema(output_model: type[BaseModel] | None) -> dict[str, Any]:
    if output_model is None:
        return {}
    return output_model.model_json_schema()


def _assert_stable_prefix(prefix: dict[str, Any]) -> None:
    text = canonical_json(prefix)
    if UUID_RE.search(text) or RUN_ID_RE.search(text):
        raise ValueError("StablePrefix contains run-like volatile identifiers")
    bad_keys = _volatile_keys(prefix)
    if bad_keys:
        raise ValueError(f"StablePrefix contains volatile keys: {bad_keys[:5]}")


def _stable_prefix_exclusion_checks(prefix: dict[str, Any]) -> dict[str, Any]:
    text = canonical_json(prefix)
    return {
        "contains_uuid": bool(UUID_RE.search(text)),
        "contains_run_id": bool(RUN_ID_RE.search(text)),
        "volatile_keys": _volatile_keys(prefix),
    }


def _partition_diagnostics(
    dynamic: dict[str, Any],
    volatile: dict[str, Any],
    unknown_keys: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "unknown_context_keys": unknown_keys,
        "partition_policy_warnings": warnings,
        "dynamic_context_budget_chars": len(canonical_json(dynamic)),
        "volatile_tail_budget_chars": len(canonical_json(volatile)),
    }


def _volatile_keys(value: Any, path: str = "") -> list[str]:
    if path.startswith(("output_schema", "tool_catalog")) or ".properties." in path:
        return []
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            key_text = str(key)
            current = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in VOLATILE_KEYS:
                rows.append(current)
            rows.extend(_volatile_keys(item, current))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_volatile_keys(item, f"{path}[{index}]"))
        return rows
    return []


def _supports_openai_responses_prompt_cache(settings: Settings) -> bool:
    return (
        str(settings.llm_provider or "").lower() == "openai"
        and str(settings.llm_base_url or "").rstrip("/") == "https://api.openai.com/v1"
    )


def _sorted_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sorted_jsonable(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        if all(isinstance(item, dict) and "name" in item for item in value):
            value = sorted(value, key=lambda item: str(item.get("name") or ""))
        elif all(isinstance(item, dict) and "role" in item for item in value):
            value = sorted(value, key=lambda item: str(item.get("role") or ""))
        return [_sorted_jsonable(item) for item in value]
    if isinstance(value, BaseModel):
        return _sorted_jsonable(value.model_dump(mode="json"))
    return value


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return {}
    if is_dataclass(value) and not isinstance(value, type):
        try:
            data = asdict(value)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _stable_prefix_preview(packet: ContextPacket) -> dict[str, Any]:
    prefix = packet.stable_prefix
    tool_catalog = prefix.get("tool_catalog") if isinstance(prefix.get("tool_catalog"), dict) else {}
    workspace_tools = tool_catalog.get("workspace_tools") if isinstance(tool_catalog, dict) else []
    specialist_tools = tool_catalog.get("specialist_tools") if isinstance(tool_catalog, dict) else []
    skill_manifest = prefix.get("skill_manifest") if isinstance(prefix.get("skill_manifest"), dict) else {}
    skills = skill_manifest.get("skills") if isinstance(skill_manifest, dict) else []
    tenant_policy = prefix.get("tenant_policy") if isinstance(prefix.get("tenant_policy"), dict) else {}
    output_schema = prefix.get("output_schema") if isinstance(prefix.get("output_schema"), dict) else {}
    return {
        "contract": prefix.get("contract"),
        "role": prefix.get("role"),
        "prompt_version": prefix.get("prompt_version"),
        "prompt_file": prefix.get("prompt_file"),
        "system_prompt_sha256": stable_hash(prefix.get("system_prompt") or ""),
        "output_schema_name": str(output_schema.get("title") or output_schema.get("$id") or ""),
        "tool_catalog_hash": packet.tool_catalog_hash,
        "skill_hash": packet.skill_hash,
        "tenant_policy_hash": packet.tenant_policy_hash,
        "tenant_policy_version": str(tenant_policy.get("policy_version") or ""),
        "knowledge_root_count": len(tenant_policy.get("knowledge_roots") or []),
        "workspace_tool_names": [str(item.get("name") or "") for item in workspace_tools if isinstance(item, dict)],
        "specialist_tool_names": [str(item.get("name") or "") for item in specialist_tools if isinstance(item, dict)],
        "skill_names": [str(item.get("name") or "") for item in skills if isinstance(item, dict)],
    }


def _section_index(value: dict[str, Any]) -> dict[str, Any]:
    text = canonical_json(value)
    return {
        "keys": sorted(str(key) for key in value.keys()),
        "chars": len(text),
    }
