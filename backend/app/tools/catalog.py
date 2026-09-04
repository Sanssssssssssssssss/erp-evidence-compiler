from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from app.harness import MAX_ACTION_INPUT_CHARS, MAX_ACTION_PATCH_CHARS
from app.state.schemas import Attachment
from app.tools.capabilities import TOOL_CAPABILITIES, ToolCallContext, ToolCapability
from app.tools.file_workspace import FileWorkspace


RiskLevel = Literal["read", "extract", "local_write", "external_write", "destructive", "privileged"]
ApprovalMode = Literal["never", "always"]


TOOL_RISK: dict[str, RiskLevel] = {
    "read_case_state": "read",
    "list_case_files": "read",
    "read_attachment": "extract",
    "write_case_patch": "local_write",
    "write_case_file": "local_write",
    "render_pdf": "local_write",
    "inspect_compiler_run": "read",
    "recheck_compiler_check": "local_write",
    "cancel_compiler_run": "local_write",
}

DEFAULT_APPROVAL: dict[str, ApprovalMode] = {
    "read_case_state": "never",
    "list_case_files": "never",
    "read_attachment": "never",
    "write_case_patch": "never",
    "write_case_file": "always",
    "render_pdf": "always",
    "inspect_compiler_run": "never",
    "recheck_compiler_check": "never",
    "cancel_compiler_run": "never",
}

INTERNAL_ONLY = {"write_case_patch"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: ToolCapability
    risk_level: RiskLevel
    approval_mode: ApprovalMode = "never"
    internal_only: bool = False
    max_input_chars: int = MAX_ACTION_INPUT_CHARS

    def trace_metadata(self) -> dict[str, Any]:
        metadata = self.capability.trace_metadata()
        metadata.update(
            {
                "risk_level": self.risk_level,
                "approval_mode": self.approval_mode,
                "internal_only": self.internal_only,
                "tool_protocol": "tool_catalog_v1",
            }
        )
        return metadata

    def visible_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.capability.postcondition or self.capability.precondition,
            "risk_level": self.risk_level,
            "approval_mode": self.approval_mode,
            "parameters": self.capability.input_model.model_json_schema(),
        }


class ToolCatalog:
    def __init__(
        self,
        workspace: FileWorkspace | None = None,
        *,
        content_resolver: Any | None = None,
        specs: dict[str, ToolSpec] | None = None,
    ) -> None:
        self.workspace = workspace or FileWorkspace()
        self.content_resolver = content_resolver
        self._specs = specs or _default_specs()

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._specs

    def sorted_specs(self, *, include_internal: bool = False) -> tuple[ToolSpec, ...]:
        return tuple(
            spec
            for _name, spec in sorted(self._specs.items(), key=lambda item: item[0])
            if include_internal or not spec.internal_only
        )

    def visible_tools(self) -> list[dict[str, Any]]:
        return [spec.visible_schema() for spec in self.sorted_specs()]

    def tool_names(self, *, include_internal: bool = False) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.sorted_specs(include_internal=include_internal))

    def capability_metadata(self, name: str) -> dict[str, Any]:
        return self.get(name).trace_metadata()

    def call(
        self,
        tool: str,
        case_id: str,
        payload: dict[str, Any],
        *,
        attachments: list[Attachment] | None = None,
        run_state: Any | None = None,
        internal: bool = False,
    ) -> dict[str, Any]:
        spec = self.get(tool)
        self._precheck(spec, payload, internal=internal)
        parsed = spec.capability.input_model.model_validate(payload or {})
        context = ToolCallContext(
            workspace=self.workspace,
            case_id=case_id,
            attachments=attachments or [],
            run_state=run_state,
            content_resolver=self.content_resolver,
        )
        result = spec.capability.handler(context, parsed)
        self._validate_result(spec, result)
        return result

    def _precheck(self, spec: ToolSpec, payload: dict[str, Any], *, internal: bool) -> None:
        if spec.internal_only and not internal:
            raise PermissionError(f"{spec.name} is internal-only")
        payload_chars = len(json.dumps(payload or {}, ensure_ascii=False, default=str))
        if payload_chars > spec.max_input_chars:
            raise ValueError(f"Tool input too large for {spec.name}: {payload_chars} chars")

    def _validate_result(self, spec: ToolSpec, result: Any) -> None:
        if not isinstance(result, dict):
            raise TypeError(f"Tool {spec.name} returned {type(result).__name__}, expected dict")
        missing = [key for key in spec.capability.required_result_keys if key not in result]
        if missing:
            raise ValueError(f"Tool {spec.name} result missing required keys: {missing}")


def _default_specs() -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    for name, capability in TOOL_CAPABILITIES.items():
        specs[name] = ToolSpec(
            name=name,
            capability=capability,
            risk_level=TOOL_RISK.get(name, "read"),
            approval_mode=DEFAULT_APPROVAL.get(name, "never"),
            internal_only=name in INTERNAL_ONLY,
            max_input_chars=MAX_ACTION_PATCH_CHARS if name == "write_case_patch" else MAX_ACTION_INPUT_CHARS,
        )
    return specs
