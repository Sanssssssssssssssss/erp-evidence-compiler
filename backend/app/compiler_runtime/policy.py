from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.compiler_runtime.consumer import Reportability, derive_consumer_packet
from app.compiler_runtime.models import CompiledProof
from app.compiler_runtime.requirement_pack import (
    DEFAULT_REQUIREMENT_PACK,
    RequirementPack,
)


def expand_active_requirements(
    requirement_ids: Sequence[str],
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> list[str]:
    """Close declared premises and activation without building a business proof graph."""

    return pack.expand(requirement_ids)


def requirement_context(
    requirement_ids: Sequence[str],
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> list[dict[str, Any]]:
    return pack.requirement_context(requirement_ids)


def policy_excerpt_for(
    requirement_ids: Sequence[str],
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> dict[str, Any]:
    return pack.policy_excerpt_for(requirement_ids)


def canonical_policy_snapshot(policy_excerpt: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(policy_excerpt), ensure_ascii=False, default=str))


def configured_policy_values(policy_excerpt: Mapping[str, Any]) -> dict[str, Any]:
    raw_values = policy_excerpt.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    configured: dict[str, Any] = {}
    for ref_id, envelope in values.items():
        if not isinstance(envelope, Mapping) or envelope.get("configured") is not True:
            continue
        raw = envelope.get("value")
        if isinstance(raw, Mapping):
            if "amount" in raw:
                configured[str(ref_id)] = {
                    "value": raw.get("amount"),
                    "currency": str(raw.get("currency") or "").strip(),
                    "unit": str(raw.get("unit") or "").strip(),
                }
                continue
            if "value" in raw:
                configured[str(ref_id)] = {
                    "value": raw.get("value"),
                    "currency": str(raw.get("currency") or "").strip(),
                    "unit": str(raw.get("unit") or "").strip(),
                }
                continue
        configured[str(ref_id)] = raw
    return configured


def policy_hash(policy_excerpt: Mapping[str, Any]) -> str:
    payload = json.dumps(
        canonical_policy_snapshot(policy_excerpt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def required_policy_refs(
    requirement_ids: Sequence[str],
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> set[str]:
    return pack.required_policy_refs(requirement_ids)


def proof_decision_ready(proof: CompiledProof | None) -> bool:
    """Return whether required proof scope has no unresolved obligation.

    This proof-only helper is used while CaseState is being projected.  Report
    generation must use ``case_reportability`` because execution/integrity state
    lives on ReviewArtifact, not CompiledProof.
    """

    return bool(
        proof
        and proof.decisions
        and not any(obligation.blocking for obligation in proof.obligations)
    )


def case_reportability(case_state: Any) -> Reportability:
    return derive_consumer_packet(case_state).reportability


def case_review_complete(case_state: Any) -> bool:
    return derive_consumer_packet(case_state).review_complete


def case_decision_ready(case_state: Any) -> bool:
    return derive_consumer_packet(case_state).decision_ready


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = [
    "canonical_policy_snapshot",
    "case_decision_ready",
    "case_reportability",
    "case_review_complete",
    "configured_policy_values",
    "expand_active_requirements",
    "policy_excerpt_for",
    "policy_hash",
    "proof_decision_ready",
    "required_policy_refs",
    "requirement_context",
]
