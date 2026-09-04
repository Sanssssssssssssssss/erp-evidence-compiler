from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from functools import reduce
from operator import mul
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.compiler_runtime.models import Claim, RecordFieldLocator


ProofTermKind = Literal["CLAIM", "WITNESS", "POLICY"]
CalculationOperation = Literal[
    "SUM",
    "MULTIPLY",
    "SUBTRACT",
    "ABS_DIFF",
    "EQUAL",
    "GREATER_THAN",
    "GTE",
    "LTE",
]

_BINARY_OPERATIONS = {"SUBTRACT", "ABS_DIFF", "EQUAL", "GREATER_THAN", "GTE", "LTE"}
_BOOLEAN_OPERATIONS = {"EQUAL", "GREATER_THAN", "GTE", "LTE"}

DECIMAL_ENGINE = "decimal-proof-engine"
DECIMAL_ENGINE_VERSION = "2"


class ProofTermError(ValueError):
    """Base error for deterministic proof-term evaluation."""


class ProofTermResolutionError(ProofTermError):
    """Raised when a requested proof term cannot be resolved."""


class ProofTermTypeError(ProofTermError):
    """Raised when proof terms cannot participate in the requested operation."""


class _ProofTermModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_text(value: str, *, field_name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


class ProofTermRef(_ProofTermModel):
    kind: ProofTermKind
    ref_id: str

    @field_validator("ref_id")
    @classmethod
    def validate_ref_id(cls, value: str) -> str:
        return _require_text(value, field_name="ref_id")


class SemanticBindingProposal(_ProofTermModel):
    """A model-proposed semantic relationship, never an acceptance verdict."""

    id: str
    check_id: str
    facet_ref: str
    relation: str
    term_refs: list[ProofTermRef] = Field(min_length=1)
    reason: str

    @field_validator("id", "check_id", "facet_ref", "relation", "reason")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_unique_refs(self) -> SemanticBindingProposal:
        identities = [(item.kind, item.ref_id) for item in self.term_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("term_refs must not contain duplicates")
        return self


class CalculationRequest(_ProofTermModel):
    """Model-callable input: operation and references, never values or results."""

    id: str
    check_id: str
    facet_ref: str
    operation: CalculationOperation
    operands: list[ProofTermRef] = Field(min_length=1)

    @field_validator("id", "check_id", "facet_ref")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_arity(self) -> CalculationRequest:
        count = len(self.operands)
        if self.operation in _BINARY_OPERATIONS and count != 2:
            raise ValueError(f"{self.operation} requires exactly two operands")
        if self.operation == "MULTIPLY" and count < 2:
            raise ValueError("MULTIPLY requires at least two operands")
        return self


class ResolvedProofTerm(_ProofTermModel):
    """Runtime-resolved numeric input captured inside a witness."""

    ref: ProofTermRef
    value: Decimal
    currency: str = ""
    unit: str = ""
    claim_content_hash: str = ""
    parent_lineage_hash: str = ""

    @field_validator("value")
    @classmethod
    def validate_finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("proof-term values must be finite")
        return value

    @field_validator("currency", "unit", "claim_content_hash", "parent_lineage_hash")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_term_lineage(self) -> ResolvedProofTerm:
        if self.ref.kind == "CLAIM" and not self.claim_content_hash:
            raise ValueError("CLAIM operands require claim_content_hash")
        if self.ref.kind != "CLAIM" and self.claim_content_hash:
            raise ValueError("only CLAIM operands may have claim_content_hash")
        if self.ref.kind == "WITNESS" and not self.parent_lineage_hash:
            raise ValueError("WITNESS operands require parent_lineage_hash")
        if self.ref.kind != "WITNESS" and self.parent_lineage_hash:
            raise ValueError("only WITNESS operands may have parent_lineage_hash")
        return self


class CalculationWitness(_ProofTermModel):
    """Runtime-generated, replayable proof that a closed arithmetic operation ran."""

    id: str
    check_id: str
    facet_ref: str
    operation: CalculationOperation
    operands: list[ResolvedProofTerm] = Field(min_length=1)
    result: Decimal | bool
    currency: str = ""
    unit: str = ""
    evidence_snapshot_hash: str
    policy_snapshot_hash: str
    engine: Literal["decimal-proof-engine"] = DECIMAL_ENGINE
    engine_version: Literal["1", "2"] = DECIMAL_ENGINE_VERSION
    lineage_hash: str

    @field_validator(
        "id",
        "check_id",
        "facet_ref",
        "evidence_snapshot_hash",
        "policy_snapshot_hash",
        "lineage_hash",
    )
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("currency", "unit")
    @classmethod
    def normalize_dimension_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_result_type_and_arity(self) -> CalculationWitness:
        if self.engine_version == "1" and self.operation in {"EQUAL", "GTE", "LTE"}:
            raise ValueError(f"{self.operation} requires decimal-proof-engine version 2")
        count = len(self.operands)
        if self.operation in _BINARY_OPERATIONS and count != 2:
            raise ValueError(f"{self.operation} requires exactly two operands")
        if self.operation == "MULTIPLY" and count < 2:
            raise ValueError("MULTIPLY requires at least two operands")
        if self.operation in _BOOLEAN_OPERATIONS:
            if not isinstance(self.result, bool):
                raise ValueError(f"{self.operation} witnesses require a boolean result")
        elif isinstance(self.result, bool):
            raise ValueError(f"{self.operation} witnesses require a decimal result")
        elif not self.result.is_finite():
            raise ValueError("witness result must be finite")
        return self


class ResolverWitness(_ProofTermModel):
    """Runtime-generated, replayable result of one registered ERP resolver."""

    id: str
    check_id: str
    facet_ref: str
    resolver_id: str
    resolver_version: str
    contract_hash: str
    result: bool
    resolved_inputs: dict[str, Any]
    diagnostics: list[str] = Field(default_factory=list)
    source_refs: list[str]
    source_fingerprints: dict[str, str]
    evidence_snapshot_hash: str
    source_binding_hash: str
    proposal_hash: str
    policy_source_hash: str
    lineage_hash: str = ""

    @field_validator(
        "id",
        "check_id",
        "facet_ref",
        "resolver_id",
        "resolver_version",
        "contract_hash",
        "evidence_snapshot_hash",
        "source_binding_hash",
        "proposal_hash",
        "policy_source_hash",
    )
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("source_refs", "diagnostics")
    @classmethod
    def validate_lists(cls, value: list[str], info: Any) -> list[str]:
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        if info.field_name == "source_refs" and not normalized:
            raise ValueError("source_refs must not be empty")
        return normalized

    @model_validator(mode="after")
    def seal_lineage(self) -> "ResolverWitness":
        if set(self.source_fingerprints) != set(self.source_refs):
            raise ValueError("ResolverWitness source fingerprints must cover source_refs")
        expected = resolver_witness_lineage_hash(self)
        if self.lineage_hash and self.lineage_hash != expected:
            raise ValueError("ResolverWitness lineage_hash does not match its contents")
        self.lineage_hash = expected
        return self


def resolver_witness_lineage_hash(witness: ResolverWitness) -> str:
    return _stable_hash(witness.model_dump(mode="json", exclude={"lineage_hash"}))


PolicyValue = Decimal | int | str | Mapping[str, Any]


def compute_calculation_witness(
    request: CalculationRequest,
    *,
    claims: Mapping[str, Claim],
    witnesses: Mapping[str, CalculationWitness],
    policy_values: Mapping[str, PolicyValue],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    engine_version: Literal["1", "2"] = DECIMAL_ENGINE_VERSION,
) -> CalculationWitness:
    """Resolve references and produce a deterministic arithmetic witness.

    This function proves only that the referenced numeric terms participate in the
    requested operation. It does not decide whether a term has the correct business
    role; that remains a semantic-binding/verifier concern.
    """

    evidence_hash = _require_text(evidence_snapshot_hash, field_name="evidence_snapshot_hash")
    policy_hash = _require_text(policy_snapshot_hash, field_name="policy_snapshot_hash")
    if request.id in witnesses:
        raise ProofTermResolutionError(f"witness id already exists: {request.id!r}")

    resolved = [
        _resolve_ref(
            ref,
            claims=claims,
            witnesses=witnesses,
            policy_values=policy_values,
            evidence_snapshot_hash=evidence_hash,
            policy_snapshot_hash=policy_hash,
        )
        for ref in request.operands
    ]
    currency, unit = _result_dimension(request.operation, resolved)
    result = _calculate(request.operation, [item.value for item in resolved])
    payload = _lineage_payload(
        witness_id=request.id,
        check_id=request.check_id,
        facet_ref=request.facet_ref,
        operation=request.operation,
        operands=resolved,
        result=result,
        currency=currency,
        unit=unit,
        evidence_snapshot_hash=evidence_hash,
        policy_snapshot_hash=policy_hash,
        engine_version=engine_version,
    )
    return CalculationWitness(
        id=request.id,
        check_id=request.check_id,
        facet_ref=request.facet_ref,
        operation=request.operation,
        operands=resolved,
        result=result,
        currency=currency,
        unit=unit,
        evidence_snapshot_hash=evidence_hash,
        policy_snapshot_hash=policy_hash,
        engine_version=engine_version,
        lineage_hash=_stable_hash(payload),
    )


def replay_calculation_witness(
    witness: CalculationWitness,
    *,
    claims: Mapping[str, Claim],
    witnesses: Mapping[str, CalculationWitness],
    policy_values: Mapping[str, PolicyValue],
) -> bool:
    """Return whether a witness and its full parent-witness chain replay exactly."""

    try:
        return _replay_calculation_witness(
            witness,
            claims=claims,
            witnesses=witnesses,
            policy_values=policy_values,
            visiting=set(),
        )
    except (ProofTermError, InvalidOperation, ValueError, TypeError):
        return False


# Short names are convenient for the runtime tool adapter without changing semantics.
compute_witness = compute_calculation_witness
replay_witness = replay_calculation_witness


def _replay_calculation_witness(
    witness: CalculationWitness,
    *,
    claims: Mapping[str, Claim],
    witnesses: Mapping[str, CalculationWitness],
    policy_values: Mapping[str, PolicyValue],
    visiting: set[str],
) -> bool:
    if witness.id in visiting:
        raise ProofTermResolutionError(f"cyclic witness lineage at {witness.id!r}")
    visiting.add(witness.id)
    try:
        for operand in witness.operands:
            if operand.ref.kind != "WITNESS":
                continue
            parent = witnesses.get(operand.ref.ref_id)
            if parent is None:
                raise ProofTermResolutionError(f"unknown WITNESS ref: {operand.ref.ref_id!r}")
            if not _replay_calculation_witness(
                parent,
                claims=claims,
                witnesses=witnesses,
                policy_values=policy_values,
                visiting=visiting,
            ):
                return False

        available_witnesses = dict(witnesses)
        available_witnesses.pop(witness.id, None)
        expected = compute_calculation_witness(
            CalculationRequest(
                id=witness.id,
                check_id=witness.check_id,
                facet_ref=witness.facet_ref,
                operation=witness.operation,
                operands=[item.ref for item in witness.operands],
            ),
            claims=claims,
            witnesses=available_witnesses,
            policy_values=policy_values,
            evidence_snapshot_hash=witness.evidence_snapshot_hash,
            policy_snapshot_hash=witness.policy_snapshot_hash,
            engine_version=witness.engine_version,
        )
        return expected == witness
    finally:
        visiting.remove(witness.id)


def _resolve_ref(
    ref: ProofTermRef,
    *,
    claims: Mapping[str, Claim],
    witnesses: Mapping[str, CalculationWitness],
    policy_values: Mapping[str, PolicyValue],
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
) -> ResolvedProofTerm:
    if ref.kind == "CLAIM":
        claim = claims.get(ref.ref_id)
        if claim is None:
            raise ProofTermResolutionError(f"unknown CLAIM ref: {ref.ref_id!r}")
        return ResolvedProofTerm(
            ref=ref,
            # Record observations retain their JSON type. Use its decimal spelling,
            # not binary float arithmetic. Document-authored floats stay rejected.
            value=_as_decimal(str(claim.value) if isinstance(claim.locator, RecordFieldLocator) and isinstance(claim.value, float) else claim.value, label=f"claim {ref.ref_id!r}"),
            currency=_claim_dimension(claim, "currency"),
            unit=_claim_dimension(claim, "unit"),
            claim_content_hash=claim_content_hash(claim),
        )

    if ref.kind == "POLICY":
        if ref.ref_id not in policy_values:
            raise ProofTermResolutionError(f"unknown POLICY ref: {ref.ref_id!r}")
        value, currency, unit = _policy_value(ref.ref_id, policy_values[ref.ref_id])
        return ResolvedProofTerm(ref=ref, value=value, currency=currency, unit=unit)

    parent = witnesses.get(ref.ref_id)
    if parent is None:
        raise ProofTermResolutionError(f"unknown WITNESS ref: {ref.ref_id!r}")
    if parent.evidence_snapshot_hash != evidence_snapshot_hash:
        raise ProofTermResolutionError(
            f"WITNESS ref {ref.ref_id!r} belongs to a different evidence snapshot"
        )
    if parent.policy_snapshot_hash != policy_snapshot_hash:
        raise ProofTermResolutionError(
            f"WITNESS ref {ref.ref_id!r} belongs to a different policy snapshot"
        )
    if not _witness_lineage_hash_matches(parent):
        raise ProofTermResolutionError(f"WITNESS ref {ref.ref_id!r} has an invalid lineage hash")
    if isinstance(parent.result, bool):
        raise ProofTermTypeError(f"boolean WITNESS ref {ref.ref_id!r} is not a numeric operand")
    return ResolvedProofTerm(
        ref=ref,
        value=parent.result,
        currency=parent.currency,
        unit=parent.unit,
        parent_lineage_hash=parent.lineage_hash,
    )


def _claim_dimension(claim: Claim, key: str) -> str:
    value = claim.attributes.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProofTermTypeError(f"claim {claim.id!r} attribute {key!r} must be text")
    return value.strip()


def _policy_value(ref_id: str, raw: PolicyValue) -> tuple[Decimal, str, str]:
    if isinstance(raw, Mapping):
        unknown = sorted(set(raw) - {"value", "currency", "unit"})
        if unknown:
            raise ProofTermTypeError(f"policy {ref_id!r} contains unknown fields: {unknown}")
        if "value" not in raw:
            raise ProofTermResolutionError(f"policy {ref_id!r} has no numeric value")
        currency = raw.get("currency", "")
        unit = raw.get("unit", "")
        if not isinstance(currency, str) or not isinstance(unit, str):
            raise ProofTermTypeError(f"policy {ref_id!r} currency and unit must be text")
        return _as_decimal(raw["value"], label=f"policy {ref_id!r}"), currency.strip(), unit.strip()
    return _as_decimal(raw, label=f"policy {ref_id!r}"), "", ""


def _as_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProofTermTypeError(f"{label} is not a numeric canonical value")
    if isinstance(value, float):
        raise ProofTermTypeError(f"{label} must not use a binary float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProofTermTypeError(f"{label} is not a canonical decimal value") from exc
    if not result.is_finite():
        raise ProofTermTypeError(f"{label} must be finite")
    return result


def _result_dimension(
    operation: CalculationOperation,
    operands: Sequence[ResolvedProofTerm],
) -> tuple[str, str]:
    non_policy = [item for item in operands if item.ref.kind != "POLICY"]
    comparable = non_policy or list(operands)
    currencies = {item.currency for item in comparable if item.currency}
    if len(currencies) > 1:
        raise ProofTermTypeError(f"currency mismatch: {sorted(currencies)}")

    if operation == "MULTIPLY":
        monetary = [item for item in comparable if item.currency]
        if len(monetary) > 1:
            raise ProofTermTypeError("MULTIPLY accepts at most one currency-bearing operand")
        counts = [item for item in comparable if item.unit and not item.currency]
        if len(counts) > 1:
            raise ProofTermTypeError("MULTIPLY accepts at most one count-unit operand")
        if monetary:
            # A quantity/count times a monetary unit price produces money.  We
            # deliberately stop here instead of inventing a unit algebra DSL;
            # the Verifier still owns the semantic claim that these operands
            # are quantity and unit price for the same line.
            return monetary[0].currency, ""
        if counts:
            return "", counts[0].unit
        return "", ""

    dimensions = {(item.currency, item.unit) for item in comparable}
    populated = {(currency, unit) for currency, unit in dimensions if currency or unit}
    if len(populated) > 1:
        raise ProofTermTypeError(f"unit mismatch: {sorted(populated)}")
    if populated and ("", "") in dimensions:
        raise ProofTermTypeError("cannot mix dimensioned and dimensionless non-policy operands")
    if not populated:
        return "", ""

    currency, unit = next(iter(populated))
    for item in operands:
        if item.ref.kind != "POLICY" or not (item.currency or item.unit):
            continue
        if (item.currency, item.unit) != (currency, unit):
            raise ProofTermTypeError(
                f"policy dimension mismatch: {(item.currency, item.unit)!r} != {(currency, unit)!r}"
            )
    return currency, unit


def _calculate(operation: CalculationOperation, values: Sequence[Decimal]) -> Decimal | bool:
    if operation == "SUM":
        return sum(values, Decimal("0"))
    if operation == "MULTIPLY":
        return reduce(mul, values, Decimal("1"))
    if operation == "SUBTRACT":
        return values[0] - values[1]
    if operation == "ABS_DIFF":
        return abs(values[0] - values[1])
    if operation == "EQUAL":
        return values[0] == values[1]
    if operation == "GTE":
        return values[0] >= values[1]
    if operation == "LTE":
        return values[0] <= values[1]
    return values[0] > values[1]


def _witness_lineage_hash_matches(witness: CalculationWitness) -> bool:
    payload = _lineage_payload(
        witness_id=witness.id,
        check_id=witness.check_id,
        facet_ref=witness.facet_ref,
        operation=witness.operation,
        operands=witness.operands,
        result=witness.result,
        currency=witness.currency,
        unit=witness.unit,
        evidence_snapshot_hash=witness.evidence_snapshot_hash,
        policy_snapshot_hash=witness.policy_snapshot_hash,
        engine_version=witness.engine_version,
    )
    return witness.lineage_hash == _stable_hash(payload)


def _lineage_payload(
    *,
    witness_id: str,
    check_id: str,
    facet_ref: str,
    operation: CalculationOperation,
    operands: Sequence[ResolvedProofTerm],
    result: Decimal | bool,
    currency: str,
    unit: str,
    evidence_snapshot_hash: str,
    policy_snapshot_hash: str,
    engine_version: Literal["1", "2"],
) -> dict[str, Any]:
    return {
        "id": witness_id,
        "check_id": check_id,
        "facet_ref": facet_ref,
        "operation": operation,
        "operands": [
            {
                "kind": item.ref.kind,
                "ref_id": item.ref.ref_id,
                "value": _canonical_decimal(item.value),
                "currency": item.currency,
                "unit": item.unit,
                "claim_content_hash": item.claim_content_hash,
                "parent_lineage_hash": item.parent_lineage_hash,
            }
            for item in operands
        ],
        "result": result if isinstance(result, bool) else _canonical_decimal(result),
        "currency": currency,
        "unit": unit,
        "evidence_snapshot_hash": evidence_snapshot_hash,
        "policy_snapshot_hash": policy_snapshot_hash,
        "engine": DECIMAL_ENGINE,
        "engine_version": engine_version,
    }


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def claim_content_hash(claim: Claim) -> str:
    """Commit a CLAIM operand to the complete immutable source observation."""
    return _stable_hash(claim.model_dump(mode="json"))


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CalculationOperation",
    "CalculationRequest",
    "CalculationWitness",
    "ProofTermError",
    "ProofTermKind",
    "ProofTermRef",
    "ProofTermResolutionError",
    "ProofTermTypeError",
    "ResolvedProofTerm",
    "ResolverWitness",
    "SemanticBindingProposal",
    "claim_content_hash",
    "compute_calculation_witness",
    "compute_witness",
    "replay_calculation_witness",
    "replay_witness",
    "resolver_witness_lineage_hash",
]
