from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Mapping, Sequence

from pydantic import ValidationError

from .models import Claim, ERPReviewContract, EvidenceIR, RecordFieldLocator, ReviewArtifact
from .proof_terms import (
    CalculationOperation,
    CalculationRequest,
    CalculationWitness,
    ProofTermError,
    ProofTermRef,
    ResolverWitness,
    SemanticBindingProposal,
    compute_witness,
)


_LINE_LOCATOR = re.compile(r"\blines?\s+(\d+)(?:\s*[-:]\s*(\d+))?\b", re.IGNORECASE)
_UNOBSERVED = object()
_PAGE_TEXT_LOCATOR = re.compile(
    r"\bpage\s+(\d+)(?:\s+(?:text|body(?:\s+text)?))?\s*$",
    re.IGNORECASE,
)
_PAGE_NUMBER_LOCATOR = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)
_NUMERIC_VALUE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*%?$")
_QUOTE_NUMBER = re.compile(
    r"(?<![\w])(?:[-+]\s*(?:(?:[A-Z]{3}|[$€£¥])\s*)?)?(?:\(\s*)?"
    r"(?:\d{1,3}(?:[ '’]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)"
    r"(?:\s*\))?\s*%?(?![\w])",
    re.UNICODE,
)
_NUMERIC_OBSERVATION_REPAIR = (
    "Re-bind the Claim with a canonical Decimal string for the localized number "
    "actually printed in its quote; do not use a JSON float."
)
_PERCENT_NUMERIC_OBSERVATION_REPAIR = (
    "Re-bind the Claim with a canonical Decimal factor string matching the printed "
    "percentage (for example, printed 20% -> Claim value string '0.20'; JSON float "
    "0.2, value 20, and string '20%' are invalid for that example)."
)


def locator_supports_quote(content: str, *, locator: str, quote: str) -> bool:
    """Return whether a persisted locator resolves to the quoted source text."""
    if not content or not locator or not quote:
        return False
    line_match = _LINE_LOCATOR.search(locator)
    if line_match:
        first = int(line_match.group(1))
        last = int(line_match.group(2) or first)
        lines = content.splitlines(keepends=True)
        return 1 <= first <= last <= len(lines) and quote in "".join(lines[first - 1:last])
    page_match = _PAGE_TEXT_LOCATOR.search(locator)
    if page_match:
        marker = re.search(
            rf"\[page\s+{re.escape(page_match.group(1))}\s+text\]",
            content,
            re.IGNORECASE,
        )
        if marker:
            next_page = re.search(r"\[page\s+\d+\s+text\]", content[marker.end() :], re.IGNORECASE)
            end = marker.end() + next_page.start() if next_page else len(content)
            return quote in content[marker.end() : end]
    locator_positions = [match.start() for match in re.finditer(re.escape(locator), content)]
    quote_positions = [match.start() for match in re.finditer(re.escape(quote), content)]
    return any(
        len(content[min(locator_pos, quote_pos) : max(locator_pos + len(locator), quote_pos + len(quote))]) <= 1200
        and content[min(locator_pos, quote_pos) : max(locator_pos + len(locator), quote_pos + len(quote))].count("\n") <= 6
        for locator_pos in locator_positions
        for quote_pos in quote_positions
    )


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """A run-local source. Content is exposed only after ``read_source``."""

    source_id: str
    content: str
    title: str = ""
    kind: str = "unknown"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    record_model: str = ""
    record_revision: str = ""
    structured_fields: InitVar[Mapping[str, Any] | None] = None

    def __post_init__(self, structured_fields: Mapping[str, Any] | None) -> None:
        source_id = self.source_id.strip()
        if not source_id:
            raise ValueError("source_id must not be blank")
        if not isinstance(self.content, str):
            raise TypeError("source content must be text")
        kind = self.kind.strip() or "unknown"
        record_model = self.record_model.strip()
        record_revision = self.record_revision.strip()
        if (
            structured_fields is not None
            or kind == "record"
            or record_model
            or record_revision
        ) and (kind != "record" or not record_model or not record_revision):
            raise ValueError(
                "structured records require kind='record', record_model, and record_revision"
            )
        content = self.content
        if structured_fields is not None:
            try:
                normalized_fields = json.loads(
                    json.dumps(
                        structured_fields,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("record_fields must be finite JSON data") from exc
            canonical = json.dumps(
                {
                    "record_ref": source_id,
                    "model": record_model,
                    "revision": record_revision,
                    "fields": normalized_fields,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if content and content != canonical:
                raise ValueError("structured record content must match its canonical fields")
            content = canonical
        elif record_model or record_revision:
            try:
                envelope = json.loads(content)
            except (TypeError, ValueError) as exc:
                raise ValueError("record identity requires canonical structured content") from exc
            expected = {
                "record_ref": source_id,
                "model": record_model,
                "revision": record_revision,
                "fields": envelope.get("fields") if isinstance(envelope, Mapping) else None,
            }
            canonical = json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if envelope != expected or content != canonical:
                raise ValueError("record identity does not match canonical structured content")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provenance", dict(self.provenance))
        object.__setattr__(self, "record_model", record_model)
        object.__setattr__(self, "record_revision", record_revision)

    @property
    def provenance_text(self) -> str:
        # Keep the same order that read_source exposes, so a copied multi-field
        # quote is checked against the exact text the Worker saw.
        return json.dumps(self.provenance, ensure_ascii=False, default=str)

    @property
    def record_fields(self) -> Mapping[str, Any] | None:
        if not self.record_revision:
            return None
        return json.loads(self.content)["fields"]


@dataclass(frozen=True, slots=True)
class CheckSubmission:
    submission_id: str
    check_id: str
    claim_ids: tuple[str, ...]
    binding_ids: tuple[str, ...] = ()
    witness_ids: tuple[str, ...] = ()
    note: str = ""


class EvidenceSandbox:
    """Small, in-memory capability boundary for one evidence review run.

    The sandbox deliberately has no file, shell, Python, policy, or CaseStore
    capability. Invalid model actions return repairable results and leave the
    accepted IR and check submissions unchanged.
    """

    capability_names = (
        "list_sources",
        "read_source",
        "bind_claim",
        "bind_record_field_claim",
        "compute_witness",
        "run_registered_check",
        "submit_check",
    )

    def __init__(
        self,
        *,
        sources: Iterable[SourceRecord],
        allowed_check_ids: Iterable[str],
        allowed_check_facets: Mapping[str, Iterable[str]] | None = None,
        allowed_check_policy_refs: Mapping[str, Iterable[str]] | None = None,
        policy_values: Mapping[str, Any] | None = None,
        policy_snapshot_hash: str = "",
        evidence_ir: EvidenceIR | None = None,
        erp_check_contracts: Mapping[str, ERPReviewContract] | None = None,
    ) -> None:
        source_rows = list(sources)
        self._sources = {row.source_id: row for row in source_rows}
        if len(self._sources) != len(source_rows):
            raise ValueError("source_id values must be unique")

        self._allowed_check_ids = frozenset(
            check_id.strip() for check_id in allowed_check_ids if check_id.strip()
        )
        self._allowed_check_facets = {
            check_id: frozenset(str(facet).strip() for facet in facets if str(facet).strip())
            for check_id, facets in (allowed_check_facets or {}).items()
            if check_id in self._allowed_check_ids
        }
        self._allowed_check_policy_refs = {
            check_id: frozenset(str(ref).strip() for ref in refs if str(ref).strip())
            for check_id, refs in (allowed_check_policy_refs or {}).items()
            if check_id in self._allowed_check_ids
        }
        self._policy_values = dict(policy_values or {})
        self._resolved_document_currencies: dict[str, str] = {}
        self._policy_snapshot_hash = policy_snapshot_hash.strip() or self._digest(
            {"policy_values": self._policy_values}
        )
        self._read_source_ids: set[str] = set()
        self._base_ir = evidence_ir or EvidenceIR()
        self._claims = [claim.model_copy(deep=True) for claim in self._base_ir.claims]
        self._claim_by_id: dict[str, Claim] = {}
        self._claim_by_fingerprint: dict[str, Claim] = {}
        for claim in self._claims:
            source = self._sources.get(claim.source_id)
            if source is not None and source.kind == "record" and not isinstance(
                claim.locator, RecordFieldLocator
            ):
                raise ValueError(
                    f"invalid seeded record claim {claim.id!r}: "
                    "LOCATOR_SOURCE_TYPE_MISMATCH"
                )
            if isinstance(claim.locator, RecordFieldLocator):
                error = self._record_observation_error(
                    source_id=claim.source_id,
                    locator=claim.locator,
                    value=claim.value,
                )
                if error is not None:
                    raise ValueError(
                        f"invalid seeded record claim {claim.id!r}: {error['code']}"
                    )
            fingerprint = self._fingerprint_claim(
                subject=claim.subject,
                predicate=claim.predicate,
                value=claim.value,
                source_id=claim.source_id,
                quote=claim.quote,
                locator=claim.locator,
                attributes=claim.attributes,
            )
            if claim.id in self._claim_by_id and self._claim_by_id[claim.id] != claim:
                raise ValueError(f"conflicting seeded claim id: {claim.id}")
            self._claim_by_id[claim.id] = claim
            self._claim_by_fingerprint.setdefault(fingerprint, claim)

        self._submissions: list[CheckSubmission] = []
        self._submission_by_id: dict[str, CheckSubmission] = {}
        self._submission_by_fingerprint: dict[str, CheckSubmission] = {}
        self._binding_proposals: list[SemanticBindingProposal] = []
        self._binding_by_id: dict[str, SemanticBindingProposal] = {}
        self._witnesses: list[CalculationWitness] = []
        self._witness_by_id: dict[str, CalculationWitness] = {}
        self._resolver_witnesses: list[ResolverWitness] = []
        self._resolver_witness_by_id: dict[str, ResolverWitness] = {}
        self._erp_check_contracts = dict(erp_check_contracts or {})
        self._focused_write_check_ids: frozenset[str] | None = None

    @classmethod
    def from_artifact(
        cls,
        *,
        artifact: ReviewArtifact,
        sources: Iterable[SourceRecord],
    ) -> "EvidenceSandbox":
        """Restore the last committed CHECK boundary from its canonical artifact."""

        plan = artifact.plan
        sandbox = cls(
            sources=sources,
            allowed_check_ids=(node.id for node in plan.nodes if node.kind == "CHECK"),
            allowed_check_facets={
                node.id: node.facet_refs for node in plan.nodes if node.kind == "CHECK"
            },
            allowed_check_policy_refs={
                node.id: node.policy_refs for node in plan.nodes if node.kind == "CHECK"
            },
            policy_values=artifact.resolved_policy_terms,
            policy_snapshot_hash=artifact.policy_hash,
            evidence_ir=artifact.evidence_ir,
            erp_check_contracts={
                node.id: node.action_contract
                for node in plan.nodes
                if node.kind == "CHECK" and isinstance(node.action_contract, ERPReviewContract)
            },
        )
        sandbox._binding_proposals = [item.model_copy(deep=True) for item in artifact.binding_proposals]
        sandbox._binding_by_id = {item.id: item for item in sandbox._binding_proposals}
        sandbox._witnesses = [item.model_copy(deep=True) for item in artifact.calculation_witnesses]
        sandbox._witness_by_id = {item.id: item for item in sandbox._witnesses}
        sandbox._resolver_witnesses = [
            item.model_copy(deep=True) for item in artifact.resolver_witnesses
        ]
        sandbox._resolver_witness_by_id = {
            item.id: item for item in sandbox._resolver_witnesses
        }

        check_ids = set(artifact.submitted_claim_refs)
        check_ids.update(artifact.submitted_binding_refs)
        check_ids.update(artifact.submitted_witness_refs)
        for check_id in sorted(check_ids):
            submission = CheckSubmission(
                submission_id=f"restored_{check_id}",
                check_id=check_id,
                claim_ids=tuple(artifact.submitted_claim_refs.get(check_id, ())),
                binding_ids=tuple(artifact.submitted_binding_refs.get(check_id, ())),
                witness_ids=tuple(artifact.submitted_witness_refs.get(check_id, ())),
                note="restored committed boundary",
            )
            sandbox._submissions.append(submission)
            sandbox._submission_by_id[submission.submission_id] = submission
        return sandbox

    @property
    def evidence_ir(self) -> EvidenceIR:
        source_ids = sorted(set(self._base_ir.source_ids) | set(self._sources))
        schema_version = (
            "2"
            if any(isinstance(claim.locator, RecordFieldLocator) for claim in self._claims)
            else self._base_ir.schema_version
        )
        return self._base_ir.model_copy(
            update={
                "schema_version": schema_version,
                "source_ids": source_ids,
                "claims": [claim.model_copy(deep=True) for claim in self._claims],
            },
            deep=True,
        )

    @property
    def submissions(self) -> tuple[CheckSubmission, ...]:
        return tuple(self._submissions)

    def latest_submissions(self) -> tuple[CheckSubmission, ...]:
        """Append-only receipts, with exactly one active candidate per CHECK."""
        latest = {item.check_id: item for item in self._submissions}
        return tuple(latest[key] for key in sorted(latest))

    @property
    def binding_proposals(self) -> tuple[SemanticBindingProposal, ...]:
        return tuple(item.model_copy(deep=True) for item in self._binding_proposals)

    @property
    def calculation_witnesses(self) -> tuple[CalculationWitness, ...]:
        return tuple(item.model_copy(deep=True) for item in self._witnesses)

    @property
    def resolver_witnesses(self) -> tuple[ResolverWitness, ...]:
        return tuple(item.model_copy(deep=True) for item in self._resolver_witnesses)

    @property
    def resolved_policy_terms(self) -> dict[str, Any]:
        result = dict(self._policy_values)
        for ref_id, currency in self._resolved_document_currencies.items():
            raw = result.get(ref_id)
            if isinstance(raw, Mapping) and raw.get("unit") == "document_currency":
                result[ref_id] = {
                    "value": raw.get("value"),
                    "currency": currency,
                    "unit": "",
                }
        return result

    @property
    def read_source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._read_source_ids))

    @property
    def source_records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._sources[source_id] for source_id in sorted(self._sources))

    @contextmanager
    def focused_writes(self, check_ids: Iterable[str]) -> Iterator[None]:
        """Temporarily restrict CHECK-owned mutations to an explicit focus set."""

        focused = frozenset(
            str(check_id).strip() for check_id in check_ids if str(check_id).strip()
        )
        unknown = sorted(focused - self._allowed_check_ids)
        if unknown:
            raise ValueError(f"focused writes reference checks outside the ProofPlan: {unknown}")
        previous = self._focused_write_check_ids
        self._focused_write_check_ids = (
            focused if previous is None else frozenset(focused & previous)
        )
        try:
            yield
        finally:
            self._focused_write_check_ids = previous

    def discard_proof_material(
        self,
        *,
        claim_ids: Iterable[str] = (),
        binding_ids: Iterable[str] = (),
        witness_ids: Iterable[str] = (),
    ) -> None:
        """Discard proof terms already proven unreachable from every submission."""

        discarded_claim_ids = set(claim_ids)
        discarded_binding_ids = set(binding_ids)
        discarded_witness_ids = set(witness_ids)
        retained_bindings = [
            item for item in self._binding_proposals if item.id not in discarded_binding_ids
        ]
        retained_witnesses = [
            item for item in self._witnesses if item.id not in discarded_witness_ids
        ]
        retained_resolver_witnesses = [
            item
            for item in self._resolver_witnesses
            if item.id not in discarded_witness_ids
        ]

        referenced_claim_ids = {
            claim_id for submission in self._submissions for claim_id in submission.claim_ids
        }
        referenced_binding_ids = {
            binding_id for submission in self._submissions for binding_id in submission.binding_ids
        }
        referenced_witness_ids = {
            witness_id for submission in self._submissions for witness_id in submission.witness_ids
        }
        for binding in retained_bindings:
            referenced_claim_ids.update(
                ref.ref_id for ref in binding.term_refs if ref.kind == "CLAIM"
            )
            referenced_witness_ids.update(
                ref.ref_id for ref in binding.term_refs if ref.kind == "WITNESS"
            )
        for witness in retained_witnesses:
            referenced_claim_ids.update(
                operand.ref.ref_id
                for operand in witness.operands
                if operand.ref.kind == "CLAIM"
            )
            referenced_witness_ids.update(
                operand.ref.ref_id
                for operand in witness.operands
                if operand.ref.kind == "WITNESS"
            )
        if (
            discarded_claim_ids.intersection(referenced_claim_ids)
            or discarded_binding_ids.intersection(referenced_binding_ids)
            or discarded_witness_ids.intersection(referenced_witness_ids)
        ):
            raise ValueError("cannot discard proof material referenced by a retained term or submission")

        self._claims = [item for item in self._claims if item.id not in discarded_claim_ids]
        self._claim_by_id = {item.id: item for item in self._claims}
        self._claim_by_fingerprint = {}
        for claim in self._claims:
            fingerprint = self._fingerprint_claim(
                subject=claim.subject,
                predicate=claim.predicate,
                value=claim.value,
                source_id=claim.source_id,
                quote=claim.quote,
                locator=claim.locator,
                attributes=claim.attributes,
            )
            self._claim_by_fingerprint.setdefault(fingerprint, claim)

        self._binding_proposals = retained_bindings
        self._binding_by_id = {item.id: item for item in retained_bindings}
        self._witnesses = retained_witnesses
        self._witness_by_id = {item.id: item for item in retained_witnesses}
        self._resolver_witnesses = retained_resolver_witnesses
        self._resolver_witness_by_id = {
            item.id: item for item in retained_resolver_witnesses
        }
        self._resolved_document_currencies = {}
        for witness in retained_witnesses:
            for operand in witness.operands:
                raw_policy = self._policy_values.get(operand.ref.ref_id)
                if (
                    operand.ref.kind == "POLICY"
                    and isinstance(raw_policy, Mapping)
                    and raw_policy.get("unit") == "document_currency"
                    and operand.currency
                ):
                    self._resolved_document_currencies[operand.ref.ref_id] = operand.currency

    def list_sources(self) -> dict[str, Any]:
        sources = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "characters": len(source.content),
            }
            for source in sorted(self._sources.values(), key=lambda row: row.source_id)
        ]
        return self._success("list_sources", sources=sources)

    def read_source(self, source_id: str) -> dict[str, Any]:
        source_id = source_id.strip()
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(
                "read_source",
                code="SOURCE_NOT_FOUND",
                message=f"Source {source_id!r} is not available in this run.",
                repair="Call list_sources and retry with one of its source_id values.",
                source_id=source_id,
            )

        self._read_source_ids.add(source_id)
        return self._success(
            "read_source",
            source={
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "content": source.content,
                "system_provenance": dict(source.provenance),
                "record_model": source.record_model or None,
                "record_revision": source.record_revision or None,
                "record_fields": dict(source.record_fields) if source.record_fields is not None else None,
            },
        )

    def bind_claim(
        self,
        *,
        subject: str,
        predicate: str,
        value: Any,
        source_id: str,
        quote: str,
        locator: str | int | None = None,
        confidence: str = "medium",
        claim_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_id = source_id.strip()
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(
                "bind_claim",
                code="SOURCE_NOT_FOUND",
                message=f"Source {source_id!r} is not available in this run.",
                repair="Call list_sources and use a listed source_id.",
                source_id=source_id,
            )
        if source_id not in self._read_source_ids:
            return self._failure(
                "bind_claim",
                code="SOURCE_NOT_READ",
                message=f"Source {source_id!r} has not been read in this run.",
                repair=f"Call read_source with source_id={source_id!r}, then bind the claim.",
                source_id=source_id,
            )
        if source.kind == "record":
            return self._failure(
                "bind_claim",
                code="LOCATOR_SOURCE_TYPE_MISMATCH",
                message="Structured record sources require revision-bound record_field locators.",
                repair="Use bind_record_field_claim with a field path and record revision.",
                source_id=source_id,
            )

        quote = quote.strip()
        if not quote:
            return self._failure(
                "bind_claim",
                code="QUOTE_REQUIRED",
                message="A claim must include a non-empty verbatim quote.",
                repair="Copy the shortest exact supporting text from read_source into quote.",
                source_id=source_id,
            )
        if quote not in source.content and quote not in source.provenance_text:
            return self._failure(
                "bind_claim",
                code="QUOTE_NOT_IN_SOURCE",
                message="The quote is not an exact substring of the selected source.",
                repair=(
                    "Read the source again and copy the quote exactly from content or "
                    "system_provenance, including case and spacing."
                ),
                source_id=source_id,
            )

        if locator is None:
            positions = [match.start() for match in re.finditer(f"(?={re.escape(quote)})", source.content)]
            if len(positions) != 1:
                return self._failure(
                    "bind_claim", code="LOCATOR_AMBIGUOUS" if positions else "QUOTE_NOT_IN_SOURCE",
                    message="Automatic location requires one exact quote occurrence in source content.",
                    repair="Use a longer unique exact quote or explicitly disambiguate its location.",
                    candidate_spans=[{"start": start, "end": start + len(quote)} for start in positions],
                )
            start = positions[0]
            first = source.content[:start].count("\n") + 1
            last = source.content[:start + len(quote)].count("\n") + 1
            locator = f"line {first}" if first == last else f"line {first}-{last}"
        normalized_locator, locator_error = self._normalize_locator(locator, source.content)
        if locator_error is not None:
            return self._failure("bind_claim", source_id=source_id, **locator_error)
        if (
            quote in source.content
            and not locator_supports_quote(
                source.content,
                locator=normalized_locator,
                quote=quote,
            )
        ):
            page_match = _PAGE_NUMBER_LOCATOR.search(normalized_locator)
            page_locator = f"page {page_match.group(1)} text" if page_match else ""
            if page_locator and locator_supports_quote(
                source.content,
                locator=page_locator,
                quote=quote,
            ):
                normalized_locator = page_locator
            else:
                return self._failure(
                    "bind_claim",
                    code="LOCATOR_QUOTE_MISMATCH",
                    message="The locator does not resolve to the quoted source text.",
                    repair="Use the page-text or block locator that contains the exact quote.",
                    source_id=source_id,
                )

        # Fail at the observation boundary for values that are plainly intended
        # as numeric proof terms.  Text observations remain verifier-owned; this
        # gate deliberately does not infer numeric meaning from business labels.
        if self._has_numeric_intent(value) and not self._numeric_value_matches_quote(
            value,
            quote,
        ):
            return self._failure(
                "bind_claim",
                code="CLAIM_VALUE_NOT_OBSERVED",
                message=(
                    "Numeric Claim value does not match any localized number "
                    "in its exact source quote."
                ),
                repair=self._numeric_observation_repair(quote),
                source_id=source_id,
            )

        return self._append_claim(
            action="bind_claim",
            subject=subject,
            predicate=predicate,
            value=value,
            source_id=source_id,
            quote=quote,
            locator=normalized_locator,
            confidence=confidence,
            claim_id=claim_id,
            attributes=attributes,
        )

    def bind_record_field_claim(
        self,
        *,
        subject: str,
        predicate: str,
        value: Any = _UNOBSERVED,
        source_id: str,
        locator: RecordFieldLocator | Mapping[str, Any],
        confidence: str = "medium",
        claim_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = "bind_record_field_claim"
        source_id = source_id.strip()
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(
                action,
                code="SOURCE_NOT_FOUND",
                message=f"Source {source_id!r} is not available in this run.",
                repair="Call list_sources and use a listed source_id.",
                source_id=source_id,
            )
        if source_id not in self._read_source_ids:
            return self._failure(
                action,
                code="SOURCE_NOT_READ",
                message=f"Source {source_id!r} has not been read in this run.",
                repair=f"Call read_source with source_id={source_id!r}, then bind the claim.",
                source_id=source_id,
            )
        try:
            normalized_locator = (
                locator
                if isinstance(locator, RecordFieldLocator)
                else RecordFieldLocator.model_validate(locator)
            )
        except ValidationError as exc:
            return self._failure(
                action,
                code="LOCATOR_INVALID",
                message="The record field locator is invalid.",
                repair="Use a record_field locator with record_ref, RFC 6901 field_path, and record_revision.",
                validation_errors=exc.errors(include_url=False, include_input=False),
            )
        if value is _UNOBSERVED:
            value, observation_error = self._record_field_observation(
                source_id=source_id, locator=normalized_locator,
            )
            if observation_error is not None:
                return self._failure(action, **observation_error)
        observation_error = self._record_observation_error(
            source_id=source_id, locator=normalized_locator, value=value,
        )
        if observation_error is not None:
            return self._failure(action, **observation_error)
        return self._append_claim(
            action=action,
            subject=subject,
            predicate=predicate,
            value=value,
            source_id=source_id,
            quote="",
            locator=normalized_locator,
            confidence=confidence,
            claim_id=claim_id,
            attributes=attributes,
        )

    def compute_witness(
        self,
        *,
        check_id: str,
        facet_ref: str,
        operation: CalculationOperation,
        refs: Sequence[ProofTermRef | Mapping[str, Any] | str],
    ) -> dict[str, Any]:
        check_id = check_id.strip()
        facet_ref = facet_ref.strip()
        write_scope_error = self._validate_focused_write(
            action="compute_witness",
            check_id=check_id,
        )
        if write_scope_error is not None:
            return write_scope_error
        scope_error = self._validate_check_facet(
            action="compute_witness",
            check_id=check_id,
            facet_ref=facet_ref,
        )
        if scope_error is not None:
            return scope_error

        try:
            operands = []
            for item in refs:
                if isinstance(item, str):
                    ref_id = item.strip()
                    kinds = [kind for kind, found in (
                        ("CLAIM", ref_id in self._claim_by_id),
                        ("WITNESS", ref_id in self._witness_by_id),
                        ("POLICY", ref_id in self._allowed_check_policy_refs.get(check_id, ()) and ref_id in self._policy_values),
                    ) if found]
                    if len(kinds) != 1:
                        return self._failure(
                            "compute_witness",
                            code="WITNESS_REFERENCE_AMBIGUOUS" if kinds else "WITNESS_REFERENCE_UNKNOWN",
                            message=f"Reference {ref_id!r} must identify exactly one existing proof term.",
                            repair="Use a unique ID returned by a binding/calculation tool or configured for this CHECK.",
                            ref_id=ref_id, matching_kinds=kinds,
                        )
                    item = {"kind": kinds[0], "ref_id": ref_id}
                operands.append(item if isinstance(item, ProofTermRef) else ProofTermRef.model_validate(item))
        except ValidationError as exc:
            return self._failure(
                "compute_witness",
                code="WITNESS_REFERENCE_INVALID",
                message="Witness refs must identify admitted CLAIM, prior WITNESS, or configured POLICY terms.",
                repair="Use only typed ids returned by bind_claim or compute_witness, or a configured CHECK policy ref.",
                validation_errors=exc.errors(include_url=False, include_input=False),
            )

        for ref in operands:
            if ref.kind == "CLAIM":
                claim = self._claim_by_id.get(ref.ref_id)
                if claim is None:
                    return self._unknown_proof_ref("compute_witness", ref)
                if not self._numeric_claim_matches_quote(claim):
                    return self._failure(
                        "compute_witness",
                        code="CLAIM_VALUE_NOT_OBSERVED",
                        message=(
                            f"Numeric Claim {claim.id!r} does not match any localized number "
                            "in its exact source quote."
                        ),
                        repair=self._numeric_observation_repair(claim.quote),
                        claim_id=claim.id,
                    )
            elif ref.kind == "WITNESS":
                if ref.ref_id not in self._witness_by_id:
                    return self._unknown_proof_ref("compute_witness", ref)
            else:
                allowed_policy_refs = self._allowed_check_policy_refs.get(check_id, frozenset())
                if ref.ref_id not in allowed_policy_refs or ref.ref_id not in self._policy_values:
                    return self._failure(
                        "compute_witness",
                        code="POLICY_REFERENCE_NOT_AVAILABLE",
                        message=f"Policy ref {ref.ref_id!r} is not configured for CHECK {check_id!r}.",
                        repair="Use a configured policy_ref declared on this CHECK, or leave the CHECK unresolved.",
                        check_id=check_id,
                        policy_ref=ref.ref_id,
                    )

        witness_seed = {
            "check_id": check_id,
            "facet_ref": facet_ref,
            "operation": operation,
            "refs": [item.model_dump(mode="json") for item in operands],
            "evidence_snapshot_hash": self.evidence_ir.source_snapshot_hash(),
            "policy_snapshot_hash": self._policy_snapshot_hash,
        }
        witness_id = f"witness_{self._digest(witness_seed)[:16]}"
        duplicate = self._witness_by_id.get(witness_id)
        if duplicate is not None:
            return self._success(
                "compute_witness",
                created=False,
                duplicate=True,
                witness=duplicate.model_dump(mode="json"),
            )

        try:
            policy_values, resolved_document_currencies = self._policy_values_for_request(operands)
            witness = compute_witness(
                CalculationRequest(
                    id=witness_id,
                    check_id=check_id,
                    facet_ref=facet_ref,
                    operation=operation,
                    operands=operands,
                ),
                claims=self._claim_by_id,
                witnesses=self._witness_by_id,
                policy_values=policy_values,
                evidence_snapshot_hash=witness_seed["evidence_snapshot_hash"],
                policy_snapshot_hash=self._policy_snapshot_hash,
            )
        except (ProofTermError, ValidationError, InvalidOperation, ValueError, TypeError) as exc:
            return self._failure(
                "compute_witness",
                code="WITNESS_COMPUTATION_REJECTED",
                message=f"The deterministic Decimal engine rejected this witness: {exc}",
                repair="Check operand types, currency/unit consistency, operation arity, and configured policy refs.",
                check_id=check_id,
                facet_ref=facet_ref,
            )

        self._witnesses.append(witness)
        self._witness_by_id[witness.id] = witness
        self._resolved_document_currencies.update(resolved_document_currencies)
        return self._success(
            "compute_witness",
            created=True,
            duplicate=False,
            witness=witness.model_dump(mode="json"),
        )

    def run_registered_check(self, *, check_id: str) -> dict[str, Any]:
        """Execute one frozen ERP resolver and retain its replayable receipt."""

        check_id = check_id.strip()
        scope_error = self._validate_focused_write(
            action="run_registered_check", check_id=check_id
        )
        if scope_error is not None:
            return scope_error
        contract = self._erp_check_contracts.get(check_id)
        if contract is not None and contract.execution_mode != "registered_resolver":
            return self._failure(
                "run_registered_check", code="EXECUTION_MODE_MISMATCH",
                message="This CHECK requires evidence extraction and independent semantic review.",
                repair="Use the planned source and calculation tools, not a registered resolver.",
            )
        if contract is None:
            return self._failure(
                "run_registered_check",
                code="REGISTERED_CHECK_NOT_FOUND",
                message=f"CHECK {check_id!r} has no registered ERP resolver contract.",
                repair="Use a CHECK id from the focused typed ERP ProofPlan.",
            )
        evidence_ir = self.evidence_ir
        source_fingerprints = {
            source_id: evidence_ir.source_fingerprints[source_id]
            for source_id in contract.source_refs
        }
        source_binding_hash = self._digest(source_fingerprints)
        instruction_hashes = {
            evidence_ir.source_fingerprints[source.source_id]
            for source in self._sources.values()
            if source.provenance.get("role") == "instruction"
        }
        if source_binding_hash != contract.source_snapshot_hash:
            return self._failure(
                "run_registered_check",
                code="SOURCE_SNAPSHOT_CHANGED",
                message="The registered CHECK source snapshot no longer matches the admitted sources.",
                repair="Start a new compiler revision from a freshly sealed Manager proposal.",
            )
        if contract.policy_source_hash not in instruction_hashes:
            return self._failure(
                "run_registered_check",
                code="POLICY_SOURCE_CHANGED",
                message="The registered policy source is absent from the admitted snapshot.",
                repair="Start a new compiler revision with the admitted task instruction.",
            )

        witness_id = f"resolver_{contract.immutable_contract_hash[:16]}"
        duplicate = self._resolver_witness_by_id.get(witness_id)
        if duplicate is not None:
            return self._success(
                "run_registered_check",
                created=False,
                duplicate=True,
                witness=duplicate.model_dump(mode="json"),
            )
        try:
            from erp_agent_odoo.capabilities.erp_resolvers import resolve_erp_check

            inputs, result, diagnostics = resolve_erp_check(
                contract,
                {source_id: self._sources[source_id] for source_id in contract.source_refs},
            )
            witness = ResolverWitness(
                id=witness_id,
                check_id=check_id,
                facet_ref=sorted(self._allowed_check_facets[check_id])[0],
                resolver_id=contract.resolver_program.resolver_id,
                resolver_version=contract.resolver_program.resolver_version,
                contract_hash=contract.immutable_contract_hash,
                result=result,
                resolved_inputs=inputs,
                diagnostics=diagnostics,
                source_refs=list(contract.source_refs),
                source_fingerprints=source_fingerprints,
                evidence_snapshot_hash=evidence_ir.source_snapshot_hash(),
                source_binding_hash=source_binding_hash,
                proposal_hash=contract.proposal_hash,
                policy_source_hash=contract.policy_source_hash,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(
                "run_registered_check",
                code="REGISTERED_RESOLVER_REJECTED",
                message=f"The registered ERP resolver rejected this CHECK: {exc}",
                repair="Correct the frozen source or registered resolver; do not invent proof terms.",
            )
        self._resolver_witnesses.append(witness)
        self._resolver_witness_by_id[witness.id] = witness
        return self._success(
            "run_registered_check",
            created=True,
            duplicate=False,
            witness=witness.model_dump(mode="json"),
        )

    def submit_check(
        self,
        *,
        check_id: str,
        claim_ids: Sequence[str] = (),
        binding_proposals: Sequence[SemanticBindingProposal | Mapping[str, Any]] = (),
        witness_ids: Sequence[str] = (),
        note: str = "",
        submission_id: str = "",
    ) -> dict[str, Any]:
        check_id = check_id.strip()
        write_scope_error = self._validate_focused_write(
            action="submit_check",
            check_id=check_id,
        )
        if write_scope_error is not None:
            return write_scope_error
        if check_id not in self._allowed_check_ids:
            return self._failure(
                "submit_check",
                code="CHECK_NOT_IN_PLAN",
                message=f"Check {check_id!r} is not an executable CHECK in the current ProofPlan.",
                repair="Submit one of the CHECK ids from the current ProofPlan.",
                check_id=check_id,
                allowed_check_ids=sorted(self._allowed_check_ids),
            )

        normalized_claim_ids = tuple(dict.fromkeys(item.strip() for item in claim_ids if item.strip()))
        unknown_claim_ids = [item for item in normalized_claim_ids if item not in self._claim_by_id]
        if unknown_claim_ids:
            return self._failure(
                "submit_check",
                code="CLAIM_REFERENCE_NOT_FOUND",
                message="The check submission references claims that were not admitted to EvidenceIR.",
                repair="Bind the missing claims successfully, or remove their ids before retrying.",
                check_id=check_id,
                unknown_claim_ids=unknown_claim_ids,
            )

        normalized_witness_ids = tuple(
            dict.fromkeys(item.strip() for item in witness_ids if item.strip())
        )
        witness_objects = {**self._witness_by_id, **self._resolver_witness_by_id}
        witness_error = self._validate_owned_refs(
            action="submit_check",
            check_id=check_id,
            ref_ids=normalized_witness_ids,
            objects=witness_objects,
            kind="witness",
        )
        if witness_error is not None:
            return witness_error

        parsed_proposals: list[SemanticBindingProposal] = []
        try:
            parsed_proposals = [
                item
                if isinstance(item, SemanticBindingProposal)
                else SemanticBindingProposal.model_validate(item)
                for item in binding_proposals
            ]
        except ValidationError as exc:
            return self._failure(
                "submit_check",
                code="BINDING_PROPOSAL_INVALID",
                message="A semantic binding proposal does not satisfy the typed proposal schema.",
                repair="Provide id, current check_id/facet_ref, relation, typed term_refs, and reason.",
                check_id=check_id,
                validation_errors=exc.errors(include_url=False, include_input=False),
            )

        proposal_ids = [item.id for item in parsed_proposals]
        if len(set(proposal_ids)) != len(proposal_ids):
            return self._failure(
                "submit_check",
                code="BINDING_ID_CONFLICT",
                message="One submission cannot contain duplicate binding proposal ids.",
                repair="Give each distinct proposal one unique id.",
                check_id=check_id,
            )
        for proposal in parsed_proposals:
            scope_error = self._validate_check_facet(
                action="submit_check",
                check_id=proposal.check_id,
                facet_ref=proposal.facet_ref,
            )
            if proposal.check_id != check_id or scope_error is not None:
                return self._failure(
                    "submit_check",
                    code="BINDING_SCOPE_MISMATCH",
                    message=(
                        f"Binding proposal {proposal.id!r} must belong to CHECK {check_id!r} "
                        "and one of that CHECK's facet_refs."
                    ),
                    repair="Use the current check_id and a facet_ref declared on that CHECK.",
                    check_id=check_id,
                    binding_id=proposal.id,
                )
            existing = self._binding_by_id.get(proposal.id)
            if existing is not None and existing != proposal:
                return self._failure(
                    "submit_check",
                    code="BINDING_ID_CONFLICT",
                    message=f"Binding id {proposal.id!r} already identifies different content.",
                    repair="Use a new unique binding id or resubmit the identical proposal.",
                    check_id=check_id,
                    binding_id=proposal.id,
                )
            for ref in proposal.term_refs:
                if ref.kind == "CLAIM" and (
                    ref.ref_id not in self._claim_by_id or ref.ref_id not in normalized_claim_ids
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)
                if ref.kind == "WITNESS" and (
                    ref.ref_id not in witness_objects
                    or ref.ref_id not in normalized_witness_ids
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)
                if ref.kind == "POLICY" and (
                    ref.ref_id not in self._policy_values
                    or ref.ref_id
                    not in self._allowed_check_policy_refs.get(check_id, frozenset())
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)

        note = note.strip()
        fingerprint = self._digest(
            {
                "check_id": check_id,
                "claim_ids": sorted(normalized_claim_ids),
                "binding_proposals": sorted(
                    (item.model_dump(mode="json") for item in parsed_proposals),
                    key=lambda item: item["id"],
                ),
                "witness_ids": sorted(normalized_witness_ids),
                "note": note,
            }
        )
        duplicate = self._submission_by_fingerprint.get(fingerprint)
        if duplicate is not None:
            return self._success(
                "submit_check",
                created=False,
                duplicate=True,
                submission=self._dump_submission(duplicate),
            )

        submission_id = submission_id.strip() or f"submission_{fingerprint[:16]}"
        if submission_id in self._submission_by_id:
            return self._failure(
                "submit_check",
                code="SUBMISSION_ID_CONFLICT",
                message=f"Submission id {submission_id!r} already identifies different content.",
                repair="Omit submission_id to receive a deterministic id, or provide a new unique id.",
                submission_id=submission_id,
            )

        submission = CheckSubmission(
            submission_id=submission_id,
            check_id=check_id,
            claim_ids=normalized_claim_ids,
            binding_ids=tuple(proposal_ids),
            witness_ids=normalized_witness_ids,
            note=note,
        )
        for proposal in parsed_proposals:
            if proposal.id not in self._binding_by_id:
                self._binding_proposals.append(proposal)
                self._binding_by_id[proposal.id] = proposal
        self._submissions.append(submission)
        self._submission_by_id[submission_id] = submission
        self._submission_by_fingerprint[fingerprint] = submission
        return self._success(
            "submit_check",
            created=True,
            duplicate=False,
            submission=self._dump_submission(submission),
        )

    def _validate_focused_write(
        self,
        *,
        action: str,
        check_id: str,
    ) -> dict[str, Any] | None:
        focused = self._focused_write_check_ids
        if focused is None or check_id in focused:
            return None
        return self._failure(
            action,
            code="CHECK_OUTSIDE_FOCUS",
            message=(
                f"CHECK {check_id!r} is frozen during this focused Executor run."
            ),
            repair="Write proof material only for one of the focused CHECK ids.",
            check_id=check_id,
            focused_check_ids=sorted(focused),
        )

    def _validate_check_facet(
        self,
        *,
        action: str,
        check_id: str,
        facet_ref: str,
    ) -> dict[str, Any] | None:
        if check_id not in self._allowed_check_ids:
            return self._failure(
                action,
                code="CHECK_NOT_IN_PLAN",
                message=f"Check {check_id!r} is not an executable CHECK in the current ProofPlan.",
                repair="Use one of the CHECK ids from the current ProofPlan.",
                check_id=check_id,
                allowed_check_ids=sorted(self._allowed_check_ids),
            )
        allowed_facets = self._allowed_check_facets.get(check_id, frozenset())
        if not facet_ref or facet_ref not in allowed_facets:
            return self._failure(
                action,
                code="FACET_NOT_IN_CHECK",
                message=f"Facet {facet_ref!r} is not declared on CHECK {check_id!r}.",
                repair="Use one of the facet_refs declared on the current CHECK.",
                check_id=check_id,
                facet_ref=facet_ref,
                allowed_facet_refs=sorted(allowed_facets),
            )
        return None

    def _validate_owned_refs(
        self,
        *,
        action: str,
        check_id: str,
        ref_ids: Sequence[str],
        objects: Mapping[str, Any],
        kind: str,
    ) -> dict[str, Any] | None:
        unknown = [ref_id for ref_id in ref_ids if ref_id not in objects]
        if unknown:
            return self._failure(
                action,
                code=f"{kind.upper()}_REFERENCE_NOT_FOUND",
                message=f"The submission references unknown {kind} ids.",
                repair=f"Use only {kind} ids created successfully in this sandbox.",
                check_id=check_id,
                unknown_ids=unknown,
            )
        cross_check = [
            ref_id for ref_id in ref_ids
            if objects[ref_id].check_id != check_id
            and not (kind == "witness" and self._admitted_upstream_witness(check_id, objects[ref_id]))
        ]
        if cross_check:
            return self._failure(
                action,
                code=f"{kind.upper()}_CHECK_MISMATCH",
                message=f"The submission references {kind}s owned by another CHECK.",
                repair=f"Submit each {kind} only under the CHECK that created it.",
                check_id=check_id,
                cross_check_ids=cross_check,
            )
        bad_facets = [
            ref_id
            for ref_id in ref_ids
            if objects[ref_id].facet_ref
            not in self._allowed_check_facets.get(check_id, frozenset())
        ]
        if bad_facets:
            return self._failure(
                action,
                code=f"{kind.upper()}_FACET_MISMATCH",
                message=f"The submission references {kind}s outside the CHECK's facet_refs.",
                repair="Recompute or resubmit proof terms under a declared facet_ref.",
                check_id=check_id,
                invalid_ids=bad_facets,
            )
        return None

    def _admitted_upstream_witness(self, check_id: str, witness: Any) -> bool:
        contract = self._erp_check_contracts.get(check_id)
        if contract is None or contract.execution_mode != "evidence_review":
            return False
        by_logical = {item.logical_check_id: item for item in self._erp_check_contracts.values()}
        pending = list(contract.upstream_logical_check_ids)
        seen: set[str] = set()
        while pending:
            logical = pending.pop()
            if logical in seen or logical not in by_logical:
                continue
            seen.add(logical)
            owner = by_logical[logical]
            if owner.execution_check_instance_id == witness.check_id:
                return any(item.check_id == witness.check_id and witness.id in item.witness_ids for item in self.latest_submissions())
            pending.extend(owner.upstream_logical_check_ids)
        return False

    def _policy_values_for_request(
        self,
        refs: Sequence[ProofTermRef],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        result = dict(self._policy_values)
        resolved_document_currencies: dict[str, str] = {}
        currencies: set[str] = set()
        for ref in refs:
            if ref.kind == "CLAIM" and ref.ref_id in self._claim_by_id:
                currency = str(self._claim_by_id[ref.ref_id].attributes.get("currency") or "").strip()
            elif ref.kind == "WITNESS" and ref.ref_id in self._witness_by_id:
                currency = self._witness_by_id[ref.ref_id].currency
            else:
                currency = ""
            if currency:
                currencies.add(currency)
        for ref in refs:
            if ref.kind != "POLICY":
                continue
            raw = result.get(ref.ref_id)
            if not isinstance(raw, Mapping) or raw.get("unit") != "document_currency":
                continue
            if len(currencies) != 1:
                raise ProofTermError(
                    f"policy {ref.ref_id!r} uses document_currency but operands do not resolve one currency"
                )
            currency = next(iter(currencies))
            previous = self._resolved_document_currencies.get(ref.ref_id)
            if previous and previous != currency:
                raise ProofTermError(
                    f"policy {ref.ref_id!r} document_currency resolved inconsistently: "
                    f"{previous!r} != {currency!r}"
                )
            resolved_document_currencies[ref.ref_id] = currency
            result[ref.ref_id] = {
                "value": raw.get("value"),
                "currency": currency,
                "unit": "",
            }
        return result, resolved_document_currencies

    def _numeric_claim_matches_quote(self, claim: Claim) -> bool:
        if isinstance(claim.locator, RecordFieldLocator):
            return self._record_observation_error(
                source_id=claim.source_id,
                locator=claim.locator,
                value=claim.value,
            ) is None
        return self._numeric_value_matches_quote(claim.value, claim.quote)

    @staticmethod
    def _numeric_observation_repair(quote: str) -> str:
        if "%" in quote:
            return _PERCENT_NUMERIC_OBSERVATION_REPAIR
        return _NUMERIC_OBSERVATION_REPAIR

    @staticmethod
    def _has_numeric_intent(raw_value: Any) -> bool:
        if isinstance(raw_value, bool) or raw_value is None:
            return False
        if isinstance(raw_value, (Decimal, int, float)):
            return True
        return isinstance(raw_value, str) and bool(
            _NUMERIC_VALUE.fullmatch(raw_value.strip())
        )

    @classmethod
    def _numeric_value_matches_quote(cls, raw_value: Any, quote: str) -> bool:
        if isinstance(raw_value, bool) or raw_value is None or isinstance(raw_value, float):
            return False
        text = str(raw_value).strip()
        if not _NUMERIC_VALUE.fullmatch(text):
            return False
        value_percent = text.endswith("%")
        if value_percent:
            # Claim values are canonical numeric factors. A printed `20%` is
            # represented as `0.20`, never as the presentation string `20%`.
            return False
        try:
            canonical = Decimal(text.rstrip("%").replace(",", "."))
        except InvalidOperation:
            return False
        for match in _QUOTE_NUMBER.finditer(quote):
            token = match.group(0).strip()
            token_percent = token.endswith("%")
            for candidate in cls._localized_decimal_candidates(token):
                if token_percent and candidate / Decimal("100") == canonical:
                    return True
                if not token_percent and candidate == canonical:
                    return True
        return False

    @staticmethod
    def _localized_decimal_candidates(token: str) -> set[Decimal]:
        text = token.strip().rstrip("%").strip()
        negative = text.startswith("(") and text.endswith(")")
        text = text.strip("()").strip()
        sign = Decimal("-1") if negative else Decimal("1")
        if text[:1] in {"+", "-"}:
            if text[0] == "-":
                sign *= Decimal("-1")
            text = text[1:].strip()
            text = re.sub(r"^(?:[A-Z]{3}|[$€£¥])\s*", "", text)

        normalized = ""
        if any(mark in text for mark in (" ", "'", "’")):
            grouped = re.fullmatch(
                r"(\d{1,3}(?:[ '’]\d{3})+)(?:([.,])(\d+))?",
                text,
            )
            if grouped is None:
                return set()
            integer = re.sub(r"[ '’]", "", grouped.group(1))
            fraction = grouped.group(3)
            normalized = integer if fraction is None else f"{integer}.{fraction}"
        elif "." in text and "," in text:
            decimal_mark = "." if text.rfind(".") > text.rfind(",") else ","
            grouping_mark = "," if decimal_mark == "." else "."
            if text.count(decimal_mark) != 1:
                return set()
            integer, fraction = text.rsplit(decimal_mark, 1)
            if not fraction.isdigit():
                return set()
            grouped_integer = re.fullmatch(
                rf"\d{{1,3}}(?:{re.escape(grouping_mark)}\d{{3}})+",
                integer,
            )
            if grouped_integer is None:
                return set()
            normalized = integer.replace(grouping_mark, "") + "." + fraction
        elif "." in text or "," in text:
            mark = "." if "." in text else ","
            count = text.count(mark)
            if count > 1:
                if re.fullmatch(rf"\d{{1,3}}(?:{re.escape(mark)}\d{{3}})+", text) is None:
                    return set()
                normalized = text.replace(mark, "")
            else:
                integer, fraction = text.split(mark, 1)
                if not integer.isdigit() or not fraction.isdigit():
                    return set()
                # With no locale, `1,234` and `1.234` are equally valid as a
                # grouped integer or a three-decimal value. Do not guess.
                if len(fraction) == 3 and 1 <= len(integer) <= 3:
                    return set()
                normalized = f"{integer}.{fraction}"
        elif text.isdigit():
            normalized = text
        else:
            return set()

        try:
            return {Decimal(normalized) * sign}
        except InvalidOperation:
            return set()

    def _unknown_proof_ref(self, action: str, ref: ProofTermRef) -> dict[str, Any]:
        return self._failure(
            action,
            code=f"{ref.kind}_REFERENCE_NOT_FOUND",
            message=f"Proof term {ref.kind}:{ref.ref_id} is not available in this sandbox.",
            repair="Use ids returned by successful sandbox tools.",
            ref=ref.model_dump(mode="json"),
        )

    def _binding_ref_failure(
        self,
        check_id: str,
        binding_id: str,
        ref: ProofTermRef,
    ) -> dict[str, Any]:
        return self._failure(
            "submit_check",
            code="BINDING_REFERENCE_NOT_SUBMITTED",
            message=(
                f"Binding proposal {binding_id!r} references {ref.kind}:{ref.ref_id} "
                "outside this CHECK submission."
            ),
            repair="Submit every Claim/Witness used by the binding with the same CHECK; use only configured CHECK policies.",
            check_id=check_id,
            binding_id=binding_id,
            ref=ref.model_dump(mode="json"),
        )

    @staticmethod
    def _normalize_locator(
        locator: str | int,
        source_content: str,
    ) -> tuple[str, Mapping[str, Any] | None]:
        if isinstance(locator, bool):
            text = ""
        elif isinstance(locator, int):
            text = f"line {locator}" if locator > 0 else ""
        elif isinstance(locator, str):
            text = locator.strip()
            if text.isdigit():
                text = f"line {int(text)}" if int(text) > 0 else ""
        else:
            text = ""

        if not text or len(text) > 500 or not any(character.isalnum() for character in text):
            return "", {
                "code": "LOCATOR_INVALID",
                "message": "Locator must be a non-empty textual locator or positive line number.",
                "repair": "Use a stable locator such as 'invoice.pdf page 1' or line 3.",
            }

        line_match = _LINE_LOCATOR.search(text)
        if line_match:
            first = int(line_match.group(1))
            last = int(line_match.group(2) or first)
            line_count = max(1, len(source_content.splitlines()))
            if first < 1 or last < first or last > line_count:
                return "", {
                    "code": "LOCATOR_OUT_OF_RANGE",
                    "message": f"Locator {text!r} is outside the source's {line_count} lines.",
                    "repair": f"Use a line range between 1 and {line_count}, or an opaque page/field locator.",
                }
        return text, None

    def _append_claim(
        self,
        *,
        action: str,
        subject: str,
        predicate: str,
        value: Any,
        source_id: str,
        quote: str,
        locator: str | RecordFieldLocator,
        confidence: str,
        claim_id: str,
        attributes: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        subject = subject.strip()
        predicate = predicate.strip()
        if not subject or not predicate:
            return self._failure(
                action,
                code="CLAIM_SHAPE_INVALID",
                message="Claim subject and predicate must both be non-empty.",
                repair="Provide one concrete subject and one stable predicate.",
                source_id=source_id,
            )
        try:
            claim_attributes = dict(attributes or {})
        except (TypeError, ValueError):
            return self._failure(
                action,
                code="CLAIM_SHAPE_INVALID",
                message="Claim attributes must be a JSON object.",
                repair="Provide attributes as key-value pairs, or omit attributes.",
                source_id=source_id,
            )
        fingerprint = self._fingerprint_claim(
            subject=subject,
            predicate=predicate,
            value=value,
            source_id=source_id,
            quote=quote,
            locator=locator,
            attributes=claim_attributes,
        )
        duplicate = self._claim_by_fingerprint.get(fingerprint)
        if duplicate is not None:
            return self._success(
                action,
                created=False,
                duplicate=True,
                claim=duplicate.model_dump(mode="json"),
            )
        claim_id = claim_id.strip() or f"claim_{fingerprint[:16]}"
        if claim_id in self._claim_by_id:
            return self._failure(
                action,
                code="CLAIM_ID_CONFLICT",
                message=f"Claim id {claim_id!r} already identifies different content.",
                repair="Omit claim_id to receive a deterministic id, or provide a new unique id.",
                claim_id=claim_id,
            )
        try:
            claim = Claim(
                id=claim_id,
                subject=subject,
                predicate=predicate,
                value=value,
                source_id=source_id,
                quote=quote,
                locator=locator,
                confidence=confidence,
                attributes=claim_attributes,
            )
        except ValidationError as exc:
            return self._failure(
                action,
                code="CLAIM_SHAPE_INVALID",
                message="The claim does not satisfy the Claim schema.",
                repair="Correct the fields identified in details and submit the claim again.",
                validation_errors=exc.errors(include_url=False, include_input=False),
            )
        self._claims.append(claim)
        self._claim_by_id[claim.id] = claim
        self._claim_by_fingerprint[fingerprint] = claim
        return self._success(
            action,
            created=True,
            duplicate=False,
            claim=claim.model_dump(mode="json"),
        )

    @staticmethod
    def _resolve_json_pointer(document: Any, pointer: str) -> Any:
        current = document
        for raw_token in pointer[1:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping):
                current = current[token]
            elif isinstance(current, list):
                if not re.fullmatch(r"0|[1-9]\d*", token):
                    raise ValueError("invalid array index")
                current = current[int(token)]
            else:
                raise TypeError("pointer traverses a scalar")
        return current

    def _record_field_observation(
        self,
        *,
        source_id: str,
        locator: RecordFieldLocator,
    ) -> tuple[Any, dict[str, Any] | None]:
        source = self._sources.get(source_id)
        if source is None:
            return None, {
                "code": "SOURCE_NOT_FOUND",
                "message": f"Source {source_id!r} is not available in this run.",
                "repair": "Use a structured source admitted to this run.",
                "source_id": source_id,
            }
        expected_fingerprint = self._base_ir.source_fingerprints.get(source_id)
        actual_fingerprint = hashlib.sha256(source.content.encode()).hexdigest()
        if expected_fingerprint != actual_fingerprint:
            return None, {
                "code": "SOURCE_FINGERPRINT_MISMATCH",
                "message": "The structured source content does not match its admitted fingerprint.",
                "repair": "Re-admit the current canonical record snapshot before binding claims.",
                "source_id": source_id,
            }
        record_fields = source.record_fields
        if record_fields is None:
            return None, {
                "code": "LOCATOR_SOURCE_TYPE_MISMATCH",
                "message": "record_field locators require a structured record source.",
                "repair": "Use bind_claim for document text, or select a structured record source.",
                "source_id": source_id,
            }
        if locator.record_ref != source_id:
            return None, {
                "code": "LOCATOR_RECORD_MISMATCH",
                "message": "The locator record_ref does not match source_id.",
                "repair": "Use the same admitted record_ref for source_id and locator.record_ref.",
                "source_id": source_id,
            }
        if locator.record_revision != source.record_revision:
            return None, {
                "code": "SOURCE_REVISION_MISMATCH",
                "message": "The locator record_revision is stale for this source snapshot.",
                "repair": "Read the current record source and bind against its exact revision.",
                "source_id": source_id,
            }
        try:
            observed = self._resolve_json_pointer(record_fields, locator.field_path)
        except (KeyError, IndexError, TypeError, ValueError):
            return None, {
                "code": "LOCATOR_FIELD_NOT_FOUND",
                "message": "The field_path does not resolve inside the admitted record.",
                "repair": "Use an RFC 6901 path returned by read_source.record_fields.",
                "source_id": source_id,
            }
        return observed, None

    def _record_observation_error(
        self,
        *,
        source_id: str,
        locator: RecordFieldLocator,
        value: Any,
    ) -> dict[str, Any] | None:
        observed, error = self._record_field_observation(source_id=source_id, locator=locator)
        if error is not None:
            return error
        try:
            observed_json = json.dumps(
                observed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            value_json = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            value_json = ""
            observed_json = "invalid"
        if observed_json != value_json:
            return {
                "code": "CLAIM_VALUE_NOT_OBSERVED",
                "message": "Claim value does not equal the value at the admitted record field.",
                "repair": "Copy the exact typed value resolved by the field locator.",
                "source_id": source_id,
            }
        return None

    @classmethod
    def _fingerprint_claim(
        cls,
        *,
        subject: str,
        predicate: str,
        value: Any,
        source_id: str,
        quote: str,
        locator: str | RecordFieldLocator,
        attributes: Mapping[str, Any],
    ) -> str:
        locator_payload = (
            locator.model_dump(mode="json")
            if isinstance(locator, RecordFieldLocator)
            else locator
        )
        return cls._digest(
            {
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "source_id": source_id,
                "quote": quote,
                "locator": locator_payload,
                "attributes": attributes,
            }
        )

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _dump_submission(submission: CheckSubmission) -> dict[str, Any]:
        return {
            "submission_id": submission.submission_id,
            "check_id": submission.check_id,
            "claim_ids": list(submission.claim_ids),
            "binding_ids": list(submission.binding_ids),
            "witness_ids": list(submission.witness_ids),
            "note": submission.note,
        }

    @staticmethod
    def _success(action: str, **data: Any) -> dict[str, Any]:
        return {"ok": True, "action": action, **data}

    @staticmethod
    def _failure(
        action: str,
        *,
        code: str,
        message: str,
        repair: str,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "error": {
                "code": code,
                "message": message,
                "repair": repair,
                "details": details,
            },
        }
