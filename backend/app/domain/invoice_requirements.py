from __future__ import annotations

from pathlib import Path
from typing import Any

from app.compiler_runtime.requirement_pack import AURORA_AP_POLICY_PATH, RequirementPack


POLICY_PATH = AURORA_AP_POLICY_PATH


def load_requirement_pack(path: Path = POLICY_PATH) -> dict[str, Any]:
    pack = RequirementPack.from_path(path).raw
    for profile_id, rows in pack["profiles"].items():
        profile_ids = {str(row["id"]) for row in rows}
        if "invoice" in profile_ids and "invoice_calculation_valid" not in profile_ids:
            raise ValueError(f"Invoice review profile must include invoice_calculation_valid: {profile_id}")
    return pack


REQUIREMENT_PACK = load_requirement_pack()
REQUIREMENT_DEFINITIONS: dict[str, dict[str, Any]] = REQUIREMENT_PACK["requirements"]
REQUIREMENT_PROFILES: dict[str, list[dict[str, Any]]] = REQUIREMENT_PACK["profiles"]
UNCONFIGURED_POLICY_VALUES = frozenset(str(item) for item in REQUIREMENT_PACK.get("unconfigured_policy_values") or [])
REQUIREMENT_CATALOG_VERSION = str(REQUIREMENT_PACK["requirement_pack_version"])
REQUIREMENT_PLANNING_HINTS: dict[str, dict[str, Any]] = REQUIREMENT_PACK.get("planning_hints") or {}
REQUIREMENT_PROOF_SIGNATURES: tuple[dict[str, Any], ...] = tuple(
    REQUIREMENT_PACK.get("proof_signatures") or []
)


def profile_requirements(profile_id: str, *, required: bool | None = None) -> tuple[str, ...]:
    rows = REQUIREMENT_PROFILES.get(str(profile_id or "").strip(), [])
    return tuple(
        str(row["id"])
        for row in rows
        if required is None or bool(row.get("required", True)) is required
    )


def _evidence_profile_requirements(profile_id: str) -> tuple[str, ...]:
    return tuple(
        requirement_id
        for requirement_id in profile_requirements(profile_id)
        if REQUIREMENT_DEFINITIONS[requirement_id].get("owner") == "evidence"
    )


AP_THREE_WAY_REQUIREMENTS = _evidence_profile_requirements("legacy_three_way")
AP_LITE_REQUIREMENTS = _evidence_profile_requirements("ap_lite_po")

# Backward-compatible alias for existing AP review tests and stored cases.
CORE_REQUIREMENTS = AP_THREE_WAY_REQUIREMENTS

def _invoice_material_requirements(*, required: bool) -> tuple[str, ...]:
    return tuple(
        requirement_id
        for requirement_id in profile_requirements("invoice_only", required=required)
        if REQUIREMENT_DEFINITIONS[requirement_id].get("owner") == "evidence"
        and REQUIREMENT_DEFINITIONS[requirement_id].get("kind") in {"field", "visual", "risk_check"}
    )


INVOICE_REQUIRED_FIELD_REQUIREMENTS = _invoice_material_requirements(required=True)
INVOICE_OPTIONAL_FIELD_REQUIREMENTS = _invoice_material_requirements(required=False)
INVOICE_FIELD_REQUIREMENTS = INVOICE_REQUIRED_FIELD_REQUIREMENTS + INVOICE_OPTIONAL_FIELD_REQUIREMENTS

DEFAULT_REQUIREMENT_LABELS = {
    requirement_id: str(definition.get("label") or requirement_id.replace("_", " "))
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
}
KNOWN_REQUIREMENTS = frozenset(REQUIREMENT_DEFINITIONS)
COMPILER_DERIVED_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "compiler"
)
REVIEWER_DERIVED_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "reviewer"
)
COMPILER_AUTHORITY_REQUIREMENTS = frozenset(KNOWN_REQUIREMENTS - {
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "evidence"
})
AUTO_DERIVED_COMPILER_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id in COMPILER_AUTHORITY_REQUIREMENTS
    if (REQUIREMENT_PLANNING_HINTS.get(requirement_id) or {}).get(
        "activation", "derived" if requirement_id in COMPILER_DERIVED_REQUIREMENTS else "explicit"
    ) == "derived"
)
DYNAMIC_SUPPORT_REQUIREMENTS = frozenset(str(item) for item in REQUIREMENT_PACK.get("dynamic_support_requirements") or [])
if not DYNAMIC_SUPPORT_REQUIREMENTS.issubset(KNOWN_REQUIREMENTS - COMPILER_AUTHORITY_REQUIREMENTS):
    raise ValueError("Invalid dynamic_support_requirements in requirement pack")


def requirement_definition(requirement_id: str) -> dict[str, Any] | None:
    return REQUIREMENT_DEFINITIONS.get(str(requirement_id or "").strip())


def requirement_label(requirement_id: str) -> str:
    value = str(requirement_id or "").strip()
    return DEFAULT_REQUIREMENT_LABELS.get(value, value.replace("_", " ").strip() or "requirement")


def requirement_kind(requirement_id: str) -> str:
    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("kind") or "field")


def requirement_owner(requirement_id: str) -> str:
    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("owner") or "evidence")


def requirement_evidence_type(requirement_id: str) -> str:
    """Return the policy-declared stored evidence type for a source requirement."""

    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("evidence_type") or "").strip()


def requirement_premises(requirement_id: str) -> tuple[str, ...]:
    definition = requirement_definition(requirement_id) or {}
    return tuple(str(item) for item in definition.get("premise_requirements") or [])


def requirement_unconfigured_policy_values(requirement_id: str) -> tuple[str, ...]:
    definition = requirement_definition(requirement_id) or {}
    return tuple(
        str(item)
        for item in definition.get("required_policy_values") or []
        if str(item) in UNCONFIGURED_POLICY_VALUES
    )


def default_requirement_required(requirement_id: str) -> bool:
    definition = requirement_definition(requirement_id)
    return bool(definition.get("default_required", True)) if definition else True


def is_known_requirement(requirement_id: str) -> bool:
    return str(requirement_id or "").strip() in KNOWN_REQUIREMENTS
