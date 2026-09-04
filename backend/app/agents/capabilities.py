from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from app.state.schemas import CasePatch, MaterialsAdvisorResult, ReportWriterResult


RoleOwner = Literal["planner", "route_policy"]
RoleSideEffect = Literal["none"]


@dataclass(frozen=True)
class RoleCapability:
    name: str
    input_keys: tuple[str, ...]
    output_model: type[BaseModel]
    prompt_version: str
    prompt_file: str
    context_policy: tuple[str, ...]
    max_retries: int
    allowed_tools: tuple[str, ...]
    side_effects: RoleSideEffect
    owner: RoleOwner
    guard_policy: tuple[str, ...]
    fallback_policy: str

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "prompt_file": self.prompt_file,
            "output_model": self.output_model.__name__,
            "context_policy": list(self.context_policy),
            "max_retries": self.max_retries,
            "allowed_tools": list(self.allowed_tools),
            "side_effects": self.side_effects,
            "owner": self.owner,
            "guard_policy": list(self.guard_policy),
            "fallback_policy": self.fallback_policy,
        }


ROLE_CAPABILITIES: dict[str, RoleCapability] = {
    "materials_advisor": RoleCapability(
        name="materials_advisor",
        input_keys=("user_question", "case_state", "rag_context", "attachment_manifest", "memory_hints", "supervisor_task"),
        output_model=MaterialsAdvisorResult,
        prompt_version="materials_advisor_v4.5+global_policy_v1.2+task_skill_v1.2+rag_guidance_context+supervisor_task+optional_quality_split",
        prompt_file="backend/app/agents/materials_advisor/prompt.md",
        context_policy=("case_state_summary", "attachment_manifest", "rag_guidance", "memory_hints"),
        max_retries=1,
        allowed_tools=(),
        side_effects="none",
        owner="planner",
        guard_policy=("schema_retry", "rag_guidance_boundary"),
        fallback_policy="schema_retry_once_then_runtime_error",
    ),
    "case_patch_writer": RoleCapability(
        name="case_patch_writer",
        input_keys=("role_result", "case_state", "user_message", "memory_hints", "supervisor_task"),
        output_model=CasePatch,
        prompt_version="case_patch_writer_v4.8+global_policy_v1.2",
        prompt_file="backend/app/agents/patch_builder/prompt.md",
        context_policy=("case_state_summary", "role_result", "memory_hints"),
        max_retries=1,
        allowed_tools=(),
        side_effects="none",
        owner="route_policy",
        guard_policy=("schema_retry", "patch_normalizer", "quote_preservation"),
        fallback_policy="schema_retry_once_then_runtime_error",
    ),
    "report_writer": RoleCapability(
        name="report_writer",
        input_keys=(
            "canonical_consumer_packet",
            "user_request",
        ),
        output_model=ReportWriterResult,
        prompt_version="canonical_report_renderer_v1",
        prompt_file="",
        context_policy=("canonical_consumer_packet", "user_request"),
        max_retries=0,
        allowed_tools=(),
        side_effects="none",
        owner="route_policy",
        guard_policy=("canonical_packet_only", "partial_report_boundary", "no_execution_wording"),
        fallback_policy="fail_closed",
    ),
}


def role_capability(role: str) -> RoleCapability:
    try:
        return ROLE_CAPABILITIES[role]
    except KeyError as exc:
        raise KeyError(f"Unknown role capability: {role}") from exc


def role_input_keys(role: str) -> tuple[str, ...]:
    return role_capability(role).input_keys


def role_prompt_version(role: str) -> str:
    return role_capability(role).prompt_version
