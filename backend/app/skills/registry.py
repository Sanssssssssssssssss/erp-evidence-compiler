from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT


SkillSideEffect = Literal["none", "case_read", "case_write", "report_write", "file_extract"]
SkillArtifactPolicy = Literal["summary_only", "artifact_refs", "local_files"]


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    instruction_files: list[str] = Field(default_factory=list)
    resource_roots: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    side_effect: SkillSideEffect = "none"
    idempotency: str = "safe"
    allowed_roles: list[str] = Field(default_factory=list)
    max_output_chars: int = 12000
    artifact_policy: SkillArtifactPolicy = "summary_only"


class SkillRegistry:
    def __init__(self, root: Path | None = None, manifests: list[SkillManifest] | None = None) -> None:
        self.root = (root or PROJECT_ROOT).resolve()
        self._manifests = {item.name: item for item in (manifests or _default_manifests())}

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifests))

    def get(self, name: str) -> SkillManifest:
        try:
            return self._manifests[name]
        except KeyError as exc:
            raise KeyError(f"Unknown skill: {name}") from exc

    def for_role(self, role: str) -> list[SkillManifest]:
        return [item for item in self._manifests.values() if role in item.allowed_roles]

    def resolved_instruction_files(self, name: str) -> list[Path]:
        manifest = self.get(name)
        return [(self.root / value).resolve() for value in manifest.instruction_files]

    def visible_manifests(self) -> list[dict[str, object]]:
        return [item.model_dump(mode="json") for item in self._manifests.values()]


def _default_manifests() -> list[SkillManifest]:
    return [
        SkillManifest(
            name="invoice_payment",
            description="Invoice payment review rules, required materials, BPI guidance, and report templates.",
            instruction_files=[
                "backend/app/agents/materials_advisor/task_skill.md",
            ],
            resource_roots=["knowledge/invoice_payment"],
            tools=[],
            side_effect="none",
            allowed_roles=["materials_advisor", "case_patch_writer", "report_writer"],
            artifact_policy="artifact_refs",
        ),
        SkillManifest(
            name="report_generation",
            description="Markdown report drafting and PDF rendering policy.",
            instruction_files=[
                "backend/app/agents/report_writer/pdf_skill.md",
            ],
            resource_roots=["knowledge/invoice_payment/report_template.md"],
            tools=["write_case_file", "render_pdf"],
            side_effect="report_write",
            idempotency="side_effectful",
            allowed_roles=["report_writer"],
            artifact_policy="local_files",
        ),
    ]
