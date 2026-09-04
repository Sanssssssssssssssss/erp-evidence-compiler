from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.compiler_runtime.graph_walk import reachable_ids
from app.compiler_runtime.models import AssessmentStatus, ExecutionStatus
from app.compiler_runtime.proof_terms import (
    CalculationWitness,
    ProofTermRef,
)


Reportability = Literal["FULL", "PARTIAL", "NONE"]


_PROOF_ID_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<kind>claim|binding|witness|check|requirement|obligation|finding)"
    r"(?:[._:][A-Za-z0-9_]+)+"
    r"(?![A-Za-z0-9_.:-])",
    re.IGNORECASE,
)
_TRISTATE_RE = re.compile(
    r"\b(?:SUPPORTED|CONTRADICTED|NOT_FOUND)\b",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.,:'\N{RIGHT SINGLE QUOTATION MARK}\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}:-])"
    r"(?P<open>\()?\s*(?P<sign>[+\-\N{MINUS SIGN}]?)\s*"
    r"(?P<number>"
    r"(?:\d{1,3}(?:[ ,.\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}'\N{RIGHT SINGLE QUOTATION MARK}]\d{3})+(?:[.,]\d{1,6})?)"
    r"|(?:\d+(?:[.,]\d{1,6})?)"
    r")"
    r"\s*(?P<percent>[%\N{FULLWIDTH PERCENT SIGN}]?)\s*(?P<close>\))?"
    r"(?![\w.:-])"
)
_CURRENCY_SYMBOLS = "$€£¥￥₹₽₩"
_ISO_CURRENCY_CODES = {
    "AED",
    "AFN",
    "ALL",
    "AMD",
    "ANG",
    "AOA",
    "ARS",
    "AUD",
    "AWG",
    "AZN",
    "BAM",
    "BBD",
    "BDT",
    "BGN",
    "BHD",
    "BIF",
    "BMD",
    "BND",
    "BOB",
    "BOV",
    "BRL",
    "BSD",
    "BTN",
    "BWP",
    "BYN",
    "BZD",
    "CAD",
    "CDF",
    "CHE",
    "CHF",
    "CHW",
    "CLF",
    "CLP",
    "CNY",
    "COP",
    "COU",
    "CRC",
    "CUP",
    "CVE",
    "CZK",
    "DKK",
    "DJF",
    "DOP",
    "DZD",
    "EGP",
    "ERN",
    "ETB",
    "EUR",
    "FJD",
    "FKP",
    "GBP",
    "GEL",
    "GHS",
    "GIP",
    "GMD",
    "GNF",
    "GTQ",
    "GYD",
    "HKD",
    "HNL",
    "HTG",
    "HUF",
    "IDR",
    "ILS",
    "INR",
    "IQD",
    "IRR",
    "ISK",
    "JMD",
    "JOD",
    "JPY",
    "KES",
    "KGS",
    "KHR",
    "KMF",
    "KPW",
    "KRW",
    "KWD",
    "KYD",
    "KZT",
    "LAK",
    "LBP",
    "LKR",
    "LRD",
    "LSL",
    "LYD",
    "MAD",
    "MDL",
    "MGA",
    "MKD",
    "MMK",
    "MNT",
    "MOP",
    "MRU",
    "MUR",
    "MVR",
    "MWK",
    "MXN",
    "MXV",
    "MYR",
    "MZN",
    "NAD",
    "NGN",
    "NIO",
    "NOK",
    "NZD",
    "OMR",
    "PAB",
    "PEN",
    "PGK",
    "PHP",
    "PKR",
    "PLN",
    "PYG",
    "QAR",
    "RMB",
    "RON",
    "RSD",
    "RUB",
    "RWF",
    "SAR",
    "SBD",
    "SCR",
    "SDG",
    "SEK",
    "SGD",
    "SHP",
    "SLE",
    "SOS",
    "SRD",
    "SSP",
    "STN",
    "SVC",
    "SYP",
    "SZL",
    "THB",
    "TJS",
    "TMT",
    "TND",
    "TOP",
    "TRY",
    "TTD",
    "TWD",
    "TZS",
    "UAH",
    "UGX",
    "USD",
    "USN",
    "UYI",
    "UYU",
    "UYW",
    "UZS",
    "VED",
    "VES",
    "VND",
    "VUV",
    "WST",
    "XAF",
    "XAG",
    "XAU",
    "XBA",
    "XBB",
    "XBC",
    "XBD",
    "XCD",
    "XDR",
    "XOF",
    "XPD",
    "XPF",
    "XPT",
    "XSU",
    "XTS",
    "XUA",
    "XXX",
    "YER",
    "ZAR",
    "ZMW",
    "ZWG",
}
_CURRENCY_SYMBOL_CODES = {
    "$": frozenset({"AUD", "CAD", "HKD", "NZD", "SGD", "USD"}),
    "€": frozenset({"EUR"}),
    "£": frozenset({"GBP"}),
    "¥": frozenset({"CNY", "JPY"}),
    "￥": frozenset({"CNY", "JPY"}),
    "₹": frozenset({"INR"}),
    "₽": frozenset({"RUB"}),
    "₩": frozenset({"KRW"}),
}


class _ConsumerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsumerLineage(_ConsumerModel):
    plan_hash: str = ""
    proof_signature_hash: str = ""
    evidence_snapshot_hash: str = ""
    policy_hash: str = ""
    review_artifact_hash: str = ""
    compiled_proof_hash: str = ""
    compiler_version: str = ""
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class ConsumerClaim(_ConsumerModel):
    id: str
    subject: str
    predicate: str
    value: Any
    source_id: str
    quote: str
    locator: str
    confidence: str
    currency: str = ""
    unit: str = ""


class ConsumerBinding(_ConsumerModel):
    """Kernel-admitted binding structure without the model's free-form rationale."""

    id: str
    check_id: str
    facet_ref: str
    relation: str
    term_refs: list[ProofTermRef] = Field(default_factory=list)


class ConsumerObligation(_ConsumerModel):
    id: str
    requirement_id: str
    check_id: str
    missing_fact: str
    blocking: bool
    candidate_actions: list[str] = Field(default_factory=list)


class ConsumerLeafFinding(_ConsumerModel):
    id: str
    root_requirement_id: str
    check_id: str
    facet_refs: list[str] = Field(default_factory=list)
    status: AssessmentStatus
    missing_fact: str = ""
    gap_code: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    binding_ids: list[str] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    decisive_for_root: bool = False
    partial_leaf_finding: bool = False


class ConsumerRootDecision(_ConsumerModel):
    requirement_id: str
    required: bool
    root_node_id: str
    status: AssessmentStatus
    supporting_check_ids: list[str] = Field(default_factory=list)
    contradicting_check_ids: list[str] = Field(default_factory=list)
    unresolved_check_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    leaf_finding_ids: list[str] = Field(default_factory=list)
    decision_ready: bool = False
    stop_reason: str = ""


class CanonicalConsumerPacket(_ConsumerModel):
    """Derived, read-only projection for every conclusion-writing consumer.

    The packet is intentionally not stored in CaseState.  It can always be rebuilt
    from the ReviewArtifact and the Proof Kernel output, so it cannot become a
    second source of truth.
    """

    schema_version: str = "1"
    case_id: str = ""
    lineage: ConsumerLineage
    execution_status: ExecutionStatus
    review_complete: bool
    decision_ready: bool
    reportability: Reportability
    integrity_rejected: bool = False
    root_decisions: list[ConsumerRootDecision] = Field(default_factory=list)
    leaf_findings: list[ConsumerLeafFinding] = Field(default_factory=list)
    obligations: list[ConsumerObligation] = Field(default_factory=list)
    claims: list[ConsumerClaim] = Field(default_factory=list)
    binding_proposals: list[ConsumerBinding] = Field(default_factory=list)
    calculation_witnesses: list[CalculationWitness] = Field(default_factory=list)
    source_fingerprints: dict[str, str] = Field(default_factory=dict)

    def content_hash(self) -> str:
        return _stable_hash(self.model_dump(mode="json"))


def derive_consumer_packet(case_state: Any) -> CanonicalConsumerPacket:
    """Derive the only DTO allowed to feed report/manager conclusions."""

    artifact = getattr(case_state, "review_artifact", None)
    proof = getattr(case_state, "compiled_proof", None)
    execution_status = _execution_status(artifact)
    integrity_rejected = _integrity_rejected(artifact, proof)

    plan = getattr(artifact, "plan", None)
    plan_nodes = {
        str(getattr(node, "id", "") or ""): node
        for node in list(getattr(plan, "nodes", []) or [])
        if str(getattr(node, "id", "") or "")
    }
    node_results = {
        str(getattr(result, "node_id", "") or ""): result
        for result in list(getattr(proof, "node_results", []) or [])
        if str(getattr(result, "node_id", "") or "")
    }
    obligations_by_check = {
        str(getattr(item, "check_id", "") or ""): item
        for item in list(getattr(proof, "obligations", []) or [])
        if str(getattr(item, "check_id", "") or "")
    }
    diagnostic_code_by_check: dict[str, str] = {}
    for diagnostic in list(getattr(proof, "diagnostics", []) or []):
        node_id = str(getattr(diagnostic, "node_id", "") or "")
        code = str(getattr(diagnostic, "code", "") or "")
        if node_id and code:
            diagnostic_code_by_check.setdefault(node_id, code)
    requirement_requiredness = {
        str(getattr(item, "id", "") or ""): bool(getattr(item, "required", True))
        for item in list(getattr(case_state, "requirements", []) or [])
        if str(getattr(item, "id", "") or "")
    }

    roots: list[ConsumerRootDecision] = []
    findings: list[ConsumerLeafFinding] = []
    for decision in list(getattr(proof, "decisions", []) or []):
        requirement_id = str(getattr(decision, "requirement_id", "") or "")
        root_node_id = str(getattr(decision, "root_node_id", "") or "")
        reachable_checks = _reachable_check_ids(root_node_id, plan_nodes)
        decisive_ids = _decisive_check_ids(decision)
        leaf_finding_ids: list[str] = []
        for check_id in reachable_checks:
            result = node_results.get(check_id)
            if result is None:
                continue
            finding_id = f"{requirement_id}::{check_id}"
            leaf_finding_ids.append(finding_id)
            obligation = obligations_by_check.get(check_id)
            node = plan_nodes.get(check_id)
            findings.append(
                ConsumerLeafFinding(
                    id=finding_id,
                    root_requirement_id=requirement_id,
                    check_id=check_id,
                    facet_refs=sorted(str(item) for item in list(getattr(node, "facet_refs", []) or [])),
                    status=str(getattr(result, "status", "NOT_FOUND") or "NOT_FOUND"),
                    missing_fact=str(getattr(obligation, "missing_fact", "") or ""),
                    gap_code=(
                        str(getattr(result, "gap_code", "") or "")
                        or diagnostic_code_by_check.get(check_id, "")
                    ),
                    claim_ids=sorted(str(item) for item in list(getattr(result, "claim_ids", []) or [])),
                    binding_ids=sorted(str(item) for item in list(getattr(result, "binding_ids", []) or [])),
                    witness_ids=sorted(str(item) for item in list(getattr(result, "witness_ids", []) or [])),
                    source_ids=sorted(str(item) for item in list(getattr(result, "source_ids", []) or [])),
                    decisive_for_root=check_id in decisive_ids,
                )
            )
        status = str(getattr(decision, "status", "NOT_FOUND") or "NOT_FOUND")
        roots.append(
            ConsumerRootDecision(
                requirement_id=requirement_id,
                required=requirement_requiredness.get(requirement_id, True),
                root_node_id=root_node_id,
                status=status,
                supporting_check_ids=list(getattr(decision, "supporting_check_ids", []) or []),
                contradicting_check_ids=list(getattr(decision, "contradicting_check_ids", []) or []),
                unresolved_check_ids=list(getattr(decision, "unresolved_check_ids", []) or []),
                obligation_ids=list(getattr(decision, "obligation_ids", []) or []),
                leaf_finding_ids=leaf_finding_ids,
                decision_ready=status in {"SUPPORTED", "CONTRADICTED"},
                stop_reason=str(getattr(decision, "stop_reason", "") or ""),
            )
        )

    obligations = [
        ConsumerObligation(
            id=str(getattr(item, "id", "") or ""),
            requirement_id=str(getattr(item, "requirement_id", "") or ""),
            check_id=str(getattr(item, "check_id", "") or ""),
            missing_fact=str(getattr(item, "missing_fact", "") or ""),
            blocking=bool(getattr(item, "blocking", True)),
            candidate_actions=[str(action) for action in list(getattr(item, "candidate_actions", []) or [])],
        )
        for item in list(getattr(proof, "obligations", []) or [])
    ]

    explicit_requirement_ids = set(requirement_requiredness)
    required_ids = (
        {item for item, required in requirement_requiredness.items() if required}
        if explicit_requirement_ids
        else {root.requirement_id for root in roots if root.required}
    )
    roots_by_requirement = {root.requirement_id: root for root in roots}
    required_findings = [
        item for item in findings if item.root_requirement_id in required_ids
    ]
    review_complete = bool(
        artifact
        and proof
        and roots
        and execution_status == "COMPLETED"
        and not integrity_rejected
        and required_ids
        and required_ids <= set(roots_by_requirement)
        and required_findings
        and all(item.status in {"SUPPORTED", "CONTRADICTED"} for item in required_findings)
    )
    required_roots_ready = all(
        requirement_id in roots_by_requirement
        and roots_by_requirement[requirement_id].status in {"SUPPORTED", "CONTRADICTED"}
        for requirement_id in required_ids
    )
    required_obligations = any(
        item.blocking and item.requirement_id in required_ids
        for item in obligations
    )
    decision_ready = bool(
        artifact
        and proof
        and roots
        and execution_status == "COMPLETED"
        and not integrity_rejected
        and required_roots_ready
        and not required_obligations
    )
    strong_findings = [item for item in findings if item.status in {"SUPPORTED", "CONTRADICTED"}]
    if integrity_rejected or execution_status == "FAILED" or not strong_findings:
        reportability: Reportability = "NONE"
    elif review_complete and decision_ready:
        reportability = "FULL"
    else:
        reportability = "PARTIAL"

    if reportability == "PARTIAL":
        findings = [
            item.model_copy(update={"partial_leaf_finding": item.status in {"SUPPORTED", "CONTRADICTED"}})
            for item in findings
        ]

    claims, bindings, witnesses, source_fingerprints = _referenced_terms(artifact, findings)
    lineage = ConsumerLineage(
        plan_hash=str(getattr(artifact, "plan_hash", "") or ""),
        proof_signature_hash=str(getattr(artifact, "proof_signature_hash", "") or ""),
        evidence_snapshot_hash=str(getattr(artifact, "evidence_snapshot_hash", "") or ""),
        policy_hash=str(getattr(artifact, "policy_hash", "") or ""),
        review_artifact_hash=_artifact_hash(artifact),
        compiled_proof_hash=_stable_hash(_model_dump(proof)) if proof is not None else "",
        compiler_version=str(getattr(artifact, "compiler_version", "") or ""),
        prompt_versions=dict(getattr(artifact, "prompt_versions", {}) or {}),
    )
    return CanonicalConsumerPacket(
        case_id=str(getattr(case_state, "case_id", "") or ""),
        lineage=lineage,
        execution_status=execution_status,
        review_complete=review_complete,
        decision_ready=decision_ready,
        reportability=reportability,
        integrity_rejected=integrity_rejected,
        root_decisions=roots,
        leaf_findings=findings,
        obligations=obligations,
        claims=claims,
        binding_proposals=bindings,
        calculation_witnesses=witnesses,
        source_fingerprints=source_fingerprints,
    )


def partial_report_appendix(packet: CanonicalConsumerPacket) -> str:
    """Render the deterministic boundary section for a PARTIAL report."""

    if packet.reportability != "PARTIAL":
        return ""
    supported = [item for item in packet.leaf_findings if item.status == "SUPPORTED"]
    contradicted = [item for item in packet.leaf_findings if item.status == "CONTRADICTED"]
    unresolved = [item for item in packet.leaf_findings if item.status == "NOT_FOUND"]
    lines = ["## 系统核定的部分审查边界", "", "本报告状态：部分已核实；审查尚未完整结束。"]
    lines.extend(_finding_lines("已核实叶", supported))
    lines.extend(_finding_lines("已证实冲突叶", contradicted))
    if unresolved:
        lines.extend(["", "### 未决叶"])
        for item in unresolved:
            gap = _business_gap_text(item)
            lines.append(f"- `{item.root_requirement_id}` / `{item.check_id}`：{gap}")
    if packet.obligations:
        gaps_by_check = {item.check_id: _business_gap_text(item) for item in unresolved}
        lines.extend(["", "### 未完成义务"])
        for item in packet.obligations:
            gap = gaps_by_check.get(item.check_id, item.missing_fact)
            lines.append(f"- `{item.requirement_id}` / `{item.check_id}`：{gap}")
    lines.extend([
        "",
        "边界：以上局部发现不构成整体支持；不得据此付款、过账或作最终审批。",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_consumer_report(packet: CanonicalConsumerPacket) -> str:
    """Render the canonical business report without asking a model to restate proof."""

    if packet.reportability == "NONE":
        raise ValueError("canonical proof has no reportable Kernel-accepted leaf finding")
    lines = [
        "# 证据审查报告",
        "",
        "## 第一章 概览",
        "",
        f"- 案件：`{_md_cell(packet.case_id)}`",
        f"- 报告等级：`{packet.reportability}`",
        f"- 编译执行状态：`{packet.execution_status}`",
        f"- review_complete：`{str(packet.review_complete).lower()}`",
        f"- decision_ready：`{str(packet.decision_ready).lower()}`",
        "",
        "本报告仅呈现 Canonical Consumer Packet 中由 Proof Kernel 接纳的结果，"
        "不构成付款、审批、过账或其他执行授权。",
        "",
        "### 摘要结论",
        "",
        _consumer_report_summary(packet),
        "",
        "## 第二章 状态表",
        "",
        "### 根结论",
        "",
        "| 要求编号 | 三态结论 | 支持叶 | 冲突叶 | 未决叶 |",
        "|---|---|---|---|---|",
    ]
    for item in packet.root_decisions:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_code(item.requirement_id),
                    item.status,
                    _md_ids(item.supporting_check_ids),
                    _md_ids(item.contradicting_check_ids),
                    _md_ids(item.unresolved_check_ids),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "### 叶结果",
            "",
            "| 叶检查 | Facet | 三态结论 | 根结论决定性 | 缺口 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in packet.leaf_findings:
        gap = _business_gap_text(item) if item.status == "NOT_FOUND" else "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_code(item.check_id),
                    _md_ids(item.facet_refs),
                    item.status,
                    "是" if item.decisive_for_root else "否",
                    _md_cell(gap),
                )
            )
            + " |"
        )

    lines.extend(["", "## 第三章 金额与计算表", ""])
    numeric_rows = _consumer_numeric_rows(packet)
    if numeric_rows:
        lines.extend(
            [
                "| 类型 | 证明项 | 项目/运算 | 核定值 | 来源 |",
                "|---|---|---|---|---|",
            ]
        )
        for row in numeric_rows:
            lines.append("| " + " | ".join(_md_cell(value) for value in row) + " |")
    else:
        lines.append("本轮 Canonical Consumer Packet 中没有可报告的数值证明项。")

    lines.extend(["", "## 第四章 缺口表", ""])
    gaps = [item for item in packet.leaf_findings if item.status == "NOT_FOUND"]
    if gaps or packet.obligations:
        gaps_by_check = {item.check_id: _business_gap_text(item) for item in gaps}
        lines.extend(
            [
                "| 对象 | 缺失事实 | 缺口代码 | 阻断 |",
                "|---|---|---|---|",
            ]
        )
        for item in gaps:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md_code(item.check_id),
                        _md_cell(_business_gap_text(item)),
                        _md_cell(item.gap_code or "—"),
                        "—",
                    )
                )
                + " |"
            )
        for item in packet.obligations:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md_code(item.id),
                        _md_cell(gaps_by_check.get(item.check_id, item.missing_fact)),
                        "—",
                        "是" if item.blocking else "否",
                    )
                )
                + " |"
            )
    else:
        lines.append("没有未决证明缺口。")

    lines.extend(
        [
            "",
            "## 第五章 原始材料附录说明",
            "",
            "PDF renderer 会在本正文后追加“原始材料附录（仅供人工核对，不构成系统结论；"
            "以正文 canonical Proof 为准）”。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _business_gap_text(item: ConsumerLeafFinding) -> str:
    if item.gap_code == "BINDING_MISSING" and "stated_components" in item.facet_refs:
        return "税费、折扣或调整的适用税率或计算基数缺失，无法完整核验。"
    return item.missing_fact or item.gap_code or "所需事实尚未取得"


def _negative_component_keys(packet: CanonicalConsumerPacket) -> set[tuple[str, str, str]]:
    return {
        (claim.subject, claim.source_id, claim.locator)
        for claim in packet.claims
        if claim.predicate.casefold() == "sign"
        and str(claim.value).strip().casefold() == "negative"
    }


def _consumer_numeric_rows(packet: CanonicalConsumerPacket) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    negative_components = _negative_component_keys(packet)
    for claim in packet.claims:
        if not _canonical_value_numbers(claim.value, currency_hint=claim.currency):
            continue
        value = str(claim.value)
        if (
            (claim.subject, claim.source_id, claim.locator) in negative_components
            and not value.lstrip().startswith("-")
        ):
            value = f"-{value}"
        number_tokens = list(_NUMBER_TOKEN_RE.finditer(value))
        if (
            claim.currency
            and len(number_tokens) == 1
            and _adjacent_currency_options(value, number_tokens[0]) is None
        ):
            value = f"{claim.currency} {value}"
        elif claim.unit and claim.unit != "%":
            value = f"{value} {claim.unit}"
        rows.append(("Claim", claim.id, claim.predicate, value, f"{claim.source_id} {claim.locator}".strip()))
    for witness in packet.calculation_witnesses:
        if isinstance(witness.result, bool):
            continue
        value = _report_decimal(witness.result, monetary=bool(witness.currency))
        if witness.currency:
            value = f"{value} {witness.currency}"
        elif witness.unit:
            value = f"{value} {witness.unit}"
        label = _witness_business_label(witness.facet_ref, witness.operation)
        rows.append(("Witness", witness.id, label, value, "decimal-proof-engine"))
    return rows


def _consumer_report_summary(packet: CanonicalConsumerPacket) -> str:
    contradicted = [
        item for item in packet.leaf_findings if item.status == "CONTRADICTED"
    ]
    candidates = [item for item in contradicted if item.decisive_for_root]
    candidates.extend(item for item in contradicted if item not in candidates)
    final = next(
        (
            item
            for item in packet.leaf_findings
            if "final_total" in item.facet_refs
            and item.status in {"SUPPORTED", "CONTRADICTED"}
        ),
        None,
    )
    if final is not None and final not in candidates:
        candidates.append(final)
    for finding in candidates:
        summary = _reconciliation_summary(packet, finding)
        if summary:
            return summary
    if final is not None:
        if final.status == "SUPPORTED":
            return "核定结论：内部计算验证通过，目标要求已获得证据支持。"
    statuses = {item.status for item in packet.root_decisions}
    if "CONTRADICTED" in statuses:
        return "核定结论：目标要求存在已证实冲突。"
    if statuses and statuses <= {"SUPPORTED"}:
        return "核定结论：目标要求已获得证据支持。"
    return "核定结论：存在未决证据缺口，尚不能形成强结论。"


def _reconciliation_summary(
    packet: CanonicalConsumerPacket,
    finding: ConsumerLeafFinding,
) -> str:
    differences = [
        item
        for item in packet.calculation_witnesses
        if item.check_id == finding.check_id and item.operation == "ABS_DIFF"
    ]
    difference = next(
        (item for item in differences if finding.status == "CONTRADICTED" and item.result != 0),
        differences[0] if differences else None,
    )
    if difference is None:
        return ""
    printed = next((item for item in difference.operands if item.ref.kind == "CLAIM"), None)
    recomputed = next((item for item in difference.operands if item.ref.kind == "WITNESS"), None)
    if printed is None or recomputed is None:
        return ""
    labels = {
        "final_total": ("最终总金额", "票面总额", "重算总额"),
        "subtotal_aggregation": ("小计金额", "票面小计", "重算小计"),
        "line_extensions": ("行项目金额", "票面行金额", "重算行金额"),
        "stated_components": ("税费或折扣金额", "票面组件金额", "重算组件金额"),
    }
    subject, printed_label, recomputed_label = next(
        (labels[facet] for facet in finding.facet_refs if facet in labels),
        ("金额", "票面金额", "重算金额"),
    )
    relation = "不一致" if finding.status == "CONTRADICTED" else "一致"
    conclusion = "金额核对通过：" if finding.status == "SUPPORTED" else ""
    currency = difference.currency
    return (
        f"{conclusion}{subject}{relation}："
        f"{printed_label} {_report_amount(printed.value, printed.currency or currency)}，"
        f"{recomputed_label} {_report_amount(recomputed.value, recomputed.currency or currency)}，"
        f"差额 {_report_amount(difference.result, currency)}。"
    )


def _report_amount(value: Any, currency: str) -> str:
    return f"{_report_decimal(value, monetary=bool(currency))} {currency}".strip()


def _report_decimal(value: Any, *, monetary: bool) -> str:
    try:
        text = format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return str(value)
    if "." not in text:
        return f"{text}.00" if monetary else text
    whole, fraction = text.split(".", 1)
    fraction = fraction.rstrip("0")
    if monetary:
        fraction = fraction.ljust(2, "0")
    return f"{whole}.{fraction}" if fraction else whole


def _witness_business_label(facet_ref: str, operation: str) -> str:
    labels = {
        ("line_extensions", "MULTIPLY"): "行项目重算金额（MULTIPLY）",
        ("line_extensions", "ABS_DIFF"): "行项目差额（ABS_DIFF）",
        ("subtotal_aggregation", "SUM"): "重算小计（SUM）",
        ("subtotal_aggregation", "ABS_DIFF"): "小计差额（ABS_DIFF）",
        ("stated_components", "MULTIPLY"): "税费或折扣重算金额（MULTIPLY）",
        ("stated_components", "ABS_DIFF"): "税费或折扣差额（ABS_DIFF）",
        ("final_total", "SUM"): "重算总额（SUM）",
        ("final_total", "SUBTRACT"): "重算总额（SUBTRACT）",
        ("final_total", "ABS_DIFF"): "总额差额（ABS_DIFF）",
    }
    return labels.get((facet_ref, operation), operation)


def _md_cell(value: Any) -> str:
    return str(value or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _md_code(value: Any) -> str:
    return f"`{_md_cell(value).replace('`', '')}`"


def _md_ids(values: list[str]) -> str:
    return "、".join(_md_code(value) for value in values) or "—"


def finalize_consumer_report(markdown: str, packet: CanonicalConsumerPacket) -> str:
    """Apply deterministic reportability boundaries after model generation."""

    text = str(markdown or "").strip()
    if packet.reportability == "NONE":
        raise ValueError("canonical proof has no reportable Kernel-accepted leaf finding")
    validate_canonical_report_projection(text, packet)
    _reject_execution_or_approval_claims(text)
    if packet.reportability == "PARTIAL":
        _reject_partial_overclaim(text)
        if "## 系统核定的部分审查边界" not in text:
            text = f"{text}\n\n{partial_report_appendix(packet)}".rstrip()
    return text


def validate_canonical_report_projection(
    markdown: str,
    packet: CanonicalConsumerPacket,
) -> str:
    """Fail closed when canonical Markdown invents packet-external conclusions.

    This guard intentionally runs on the Report Writer's canonical Markdown, not
    on the rendered PDF.  Descriptive source appendices are added downstream and
    therefore remain outside this conclusion-projection boundary.
    """

    text = str(markdown or "")
    catalog = _proof_id_catalog(packet)
    _validate_explicit_proof_ids(text, catalog)
    _validate_report_statuses(text, packet, catalog)
    _validate_report_business_numbers(text, packet)
    return text


def _proof_id_catalog(packet: CanonicalConsumerPacket) -> dict[str, set[str]]:
    requirements = {
        *[item.requirement_id for item in packet.root_decisions],
        *[item.root_requirement_id for item in packet.leaf_findings],
        *[item.requirement_id for item in packet.obligations],
    }
    checks = {
        *[item.check_id for item in packet.leaf_findings],
        *[item.check_id for item in packet.obligations],
        *[item.check_id for item in packet.binding_proposals],
        *[item.check_id for item in packet.calculation_witnesses],
        *[
            check_id
            for root in packet.root_decisions
            for check_id in (
                *root.supporting_check_ids,
                *root.contradicting_check_ids,
                *root.unresolved_check_ids,
            )
        ],
    }
    return {
        "claim": {item.id for item in packet.claims if item.id},
        "binding": {item.id for item in packet.binding_proposals if item.id},
        "witness": {item.id for item in packet.calculation_witnesses if item.id},
        "requirement": {item for item in requirements if item},
        "check": {item for item in checks if item},
        "obligation": {item.id for item in packet.obligations if item.id},
        "finding": {item.id for item in packet.leaf_findings if item.id},
        "gap_code": {item.gap_code for item in packet.leaf_findings if item.gap_code},
    }


def _validate_explicit_proof_ids(markdown: str, catalog: dict[str, set[str]]) -> None:
    for match in _PROOF_ID_TOKEN_RE.finditer(markdown):
        token = match.group(0)
        kind = match.group("kind").lower()
        if token not in catalog[kind] and token not in catalog["gap_code"]:
            raise ValueError(
                f"report cites proof id outside canonical consumer packet: {token}"
            )

    label_specs: tuple[tuple[str, str], ...] = (
        ("claim", r"(?:claim\s*(?:id|number)|主张\s*(?:ID|编号))"),
        ("binding", r"(?:binding\s*(?:id|number)|绑定\s*(?:ID|编号))"),
        (
            "witness",
            r"(?:witness\s*(?:id|number)|calculation\s*(?:id|number)|"
            r"(?:见证|计算)\s*(?:ID|编号))",
        ),
        ("requirement", r"(?:requirement\s*(?:id|number)|要求\s*(?:ID|编号))"),
        ("check", r"(?:check\s*(?:id|number)|(?:检查|叶检查)\s*(?:ID|编号))"),
        ("obligation", r"(?:obligation\s*(?:id|number)|义务\s*(?:ID|编号))"),
        ("finding", r"(?:finding\s*(?:id|number)|发现\s*(?:ID|编号))"),
    )
    for kind, label in label_specs:
        pattern = re.compile(
            rf"{label}(?:[ \t]*[:：=#][ \t]*|[ \t]+)"
            rf"[`*_]*(?P<id>[A-Za-z0-9_][A-Za-z0-9_.:-]*)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(markdown):
            identifier = match.group("id").rstrip(".:-")
            if identifier not in catalog[kind]:
                raise ValueError(
                    "report cites proof id outside canonical consumer packet: "
                    f"{identifier}"
                )

    proof_label = re.compile(
        r"(?:proof\s*(?:id|number)|证明\s*(?:ID|编号))"
        r"(?:[ \t]*[:：=#][ \t]*|[ \t]+)"
        r"[`*_]*(?P<id>[A-Za-z0-9_][A-Za-z0-9_.:-]*)",
        re.IGNORECASE,
    )
    admitted_proof_ids = set().union(
        catalog["claim"],
        catalog["binding"],
        catalog["witness"],
        catalog["finding"],
    )
    for match in proof_label.finditer(markdown):
        identifier = match.group("id").rstrip(".:-")
        if identifier not in admitted_proof_ids:
            raise ValueError(
                f"report cites proof id outside canonical consumer packet: {identifier}"
            )

    for headers, rows in _markdown_tables(markdown):
        columns = [_table_id_kinds(header) for header in headers]
        for row in rows:
            for index, kinds in enumerate(columns):
                if not kinds or index >= len(row):
                    continue
                admitted = set().union(*(catalog[kind] for kind in kinds))
                for identifier in _table_id_tokens(row[index]):
                    if identifier not in admitted:
                        raise ValueError(
                            "report cites proof id outside canonical consumer packet: "
                            f"{identifier}"
                        )


def _validate_report_statuses(
    markdown: str,
    packet: CanonicalConsumerPacket,
    catalog: dict[str, set[str]],
) -> None:
    expected_by_id = {
        **{item.requirement_id: str(item.status) for item in packet.root_decisions},
        **{item.check_id: str(item.status) for item in packet.leaf_findings},
        **{item.id: str(item.status) for item in packet.leaf_findings},
    }

    for headers, rows in _markdown_tables(markdown):
        status_columns = [
            index for index, header in enumerate(headers) if _is_status_header(header)
        ]
        check_columns = [
            index
            for index, header in enumerate(headers)
            if "check" in _table_id_kinds(header)
        ]
        requirement_columns = [
            index
            for index, header in enumerate(headers)
            if "requirement" in _table_id_kinds(header)
        ]
        target_columns = [*check_columns, *requirement_columns]
        for row in rows:
            for status_index in status_columns:
                if status_index >= len(row):
                    continue
                statuses = _TRISTATE_RE.findall(row[status_index])
                if len(statuses) != 1:
                    continue
                for target_index in target_columns:
                    if target_index >= len(row):
                        continue
                    identifiers = _table_id_tokens(row[target_index])
                    expected = {
                        expected_by_id[item]
                        for item in identifiers
                        if item in expected_by_id
                    }
                    if len(expected) == 1:
                        _require_matching_status(
                            identifier=identifiers[0],
                            actual=statuses[0],
                            expected=next(iter(expected)),
                        )
                        break

    identifiers = sorted(expected_by_id, key=len, reverse=True)
    if identifiers:
        identifier_re = re.compile(
            r"(?<![A-Za-z0-9_.:-])(?:"
            + "|".join(re.escape(item) for item in identifiers)
            + r")(?![A-Za-z0-9_.:-])"
        )
        for line in markdown.splitlines():
            id_matches = list(identifier_re.finditer(line))
            for status_match in _TRISTATE_RE.finditer(line):
                preceding = [item for item in id_matches if item.end() <= status_match.start()]
                if not preceding:
                    continue
                identifier_match = max(preceding, key=lambda item: item.end())
                between = line[identifier_match.end() : status_match.start()]
                if len(between) > 96 or not re.search(
                    r"[:：=|→]|->|\b(?:is|status|decision)\b|(?:状态|结论|为)",
                    between,
                    re.IGNORECASE,
                ):
                    continue
                identifier = identifier_match.group(0)
                _require_matching_status(
                    identifier=identifier,
                    actual=status_match.group(0),
                    expected=expected_by_id[identifier],
                )

    metadata_specs: tuple[tuple[str, str, str], ...] = (
        (
            r"(?:\breportability\b|报告等级|报告状态)",
            r"FULL|PARTIAL|NONE",
            packet.reportability,
        ),
        (
            r"(?:\bexecution_status\b|执行状态)",
            r"COMPLETED|PARTIAL|FAILED",
            str(packet.execution_status),
        ),
        (r"\breview_complete\b", r"TRUE|FALSE", str(packet.review_complete).upper()),
        (r"\bdecision_ready\b", r"TRUE|FALSE", str(packet.decision_ready).upper()),
        (
            r"\bintegrity_rejected\b",
            r"TRUE|FALSE",
            str(packet.integrity_rejected).upper(),
        ),
    )
    for label, values, expected in metadata_specs:
        pattern = re.compile(
            rf"{label}\s*(?:[:：=]|为)\s*[`*_]*(?P<value>{values})\b",
            re.IGNORECASE,
        )
        for match in pattern.finditer(markdown):
            actual = match.group("value").upper()
            if actual != str(expected).upper():
                raise ValueError(
                    "report status conflicts with canonical consumer packet: "
                    f"{match.group(0)} (expected {expected})"
                )


def _require_matching_status(*, identifier: str, actual: str, expected: str) -> None:
    if actual.upper() != expected.upper():
        raise ValueError(
            "report status conflicts with canonical consumer packet: "
            f"{identifier}={actual} (expected {expected})"
        )


def _markdown_tables(markdown: str) -> list[tuple[list[str], list[list[str]]]]:
    lines = markdown.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        headers = _split_table_row(lines[index])
        separators = _split_table_row(lines[index + 1])
        if (
            len(headers) < 2
            or len(separators) != len(headers)
            or not all(re.fullmatch(r":?-{3,}:?", item.strip()) for item in separators)
        ):
            index += 1
            continue
        rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines) and "|" in lines[cursor]:
            row = _split_table_row(lines[cursor])
            if len(row) < 2:
                break
            rows.append(row)
            cursor += 1
        tables.append((headers, rows))
        index = cursor
    return tables


def _split_table_row(line: str) -> list[str]:
    if "|" not in line:
        return []
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [item.strip() for item in value.split("|")]


def _table_id_kinds(header: str) -> set[str]:
    normalized = re.sub(r"[\s`*_]", "", header).lower()
    has_id_marker = "id" in normalized or "编号" in normalized or "number" in normalized
    kinds: set[str] = set()
    if has_id_marker and ("claim" in normalized or "主张" in normalized):
        kinds.add("claim")
    if has_id_marker and ("binding" in normalized or "绑定" in normalized):
        kinds.add("binding")
    if has_id_marker and (
        "witness" in normalized or "calculation" in normalized or "计算" in normalized or "见证" in normalized
    ):
        kinds.add("witness")
    if has_id_marker and ("requirement" in normalized or "要求" in normalized):
        kinds.add("requirement")
    if (has_id_marker and ("check" in normalized or "检查" in normalized)) or "叶检查" in normalized:
        kinds.add("check")
    if has_id_marker and ("obligation" in normalized or "义务" in normalized):
        kinds.add("obligation")
    if has_id_marker and ("finding" in normalized or "发现" in normalized):
        kinds.add("finding")
    if has_id_marker and ("proof" in normalized or "证明" in normalized):
        kinds.update({"claim", "binding", "witness", "finding"})
    return kinds


def _table_id_tokens(cell: str) -> list[str]:
    value = re.sub(r"<br\s*/?>", "、", cell, flags=re.IGNORECASE)
    value = value.replace("`", "").replace("**", "").strip()
    if not value or re.fullmatch(r"(?:[-—–]+|N/?A|NONE|无|未提供|不适用)", value, re.IGNORECASE):
        return []
    namespaced = [match.group(0) for match in _PROOF_ID_TOKEN_RE.finditer(value)]
    pieces = re.split(r"\s*(?:、|,|，|;|；|/)\s*", value)
    tokens: list[str] = []
    for piece in pieces:
        clean = piece.strip(" \t`*()（）[]【】")
        match = re.match(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", clean)
        if match:
            tokens.append(match.group(0).rstrip(".:-"))
    return list(dict.fromkeys([*namespaced, *tokens]))


def _is_status_header(header: str) -> bool:
    normalized = re.sub(r"[\s`*_]", "", header).lower()
    return any(token in normalized for token in ("三态", "状态", "status", "结论"))


def _validate_report_business_numbers(
    markdown: str,
    packet: CanonicalConsumerPacket,
) -> None:
    allowed = _packet_numeric_values(packet)
    for match in _NUMBER_TOKEN_RE.finditer(markdown):
        if bool(match.group("open")) != bool(match.group("close")):
            continue
        is_percent = bool(match.group("percent"))
        report_currency = _adjacent_currency_options(markdown, match)
        has_currency = report_currency is not None
        if not (is_percent or has_currency or _has_unambiguous_decimal(match.group("number"))):
            continue
        if _is_structural_number(markdown, match, has_currency=has_currency, is_percent=is_percent):
            continue
        parse_text = match.group(0)
        if is_percent and match.group("open") and not match.group("sign"):
            parse_text = parse_text.strip()[1:-1]
        if _is_markdown_list_marker(markdown, match):
            parse_text = re.sub(
                r"^\s*[\-\N{MINUS SIGN}]\s+",
                "",
                parse_text,
                count=1,
            )
        values = _parse_localized_decimals(parse_text)
        if not values:
            continue
        if any(
            _matches_packet_numeric(
                value,
                is_percent=is_percent,
                report_currency=report_currency,
                allowed=allowed,
            )
            for value in values
        ):
            continue
        raw = match.group(0).strip()
        raise ValueError(
            "report contains business numeric value outside canonical consumer packet: "
            f"{raw}"
        )


def _is_markdown_list_marker(markdown: str, match: re.Match[str]) -> bool:
    if match.group("open") or match.group("sign") not in {"-", "\N{MINUS SIGN}"}:
        return False
    line_start = markdown.rfind("\n", 0, match.start()) + 1
    return (
        not markdown[line_start:match.start()].strip()
        and re.match(r"^\s*[\-\N{MINUS SIGN}]\s+\d", match.group(0)) is not None
    )


def _packet_numeric_values(
    packet: CanonicalConsumerPacket,
) -> set[tuple[Decimal, frozenset[str]]]:
    values: set[tuple[Decimal, frozenset[str]]] = set()
    negative_components = _negative_component_keys(packet)
    for claim in packet.claims:
        claim_values = _canonical_value_numbers(
            claim.value,
            currency_hint=claim.currency,
        )
        values.update(claim_values)
        if (claim.subject, claim.source_id, claim.locator) in negative_components:
            values.update((-abs(value), currencies) for value, currencies in claim_values)
    for witness in packet.calculation_witnesses:
        if not isinstance(witness.result, bool):
            values.add(
                (
                    witness.result,
                    _currency_options_for_token(witness.currency),
                )
            )
        for operand in witness.operands:
            values.add(
                (
                    operand.value,
                    _currency_options_for_token(operand.currency),
                )
            )
    return values


def _canonical_value_numbers(
    value: Any,
    *,
    currency_hint: str = "",
) -> set[tuple[Decimal, frozenset[str]]]:
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, (dict, list, tuple, set)):
        values = value.values() if isinstance(value, dict) else value
        return set().union(
            *(
                _canonical_value_numbers(
                    item,
                    currency_hint=currency_hint,
                )
                for item in values
            ),
        )
    if isinstance(value, (Decimal, int, float)):
        try:
            numeric = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            return set()
        return {
            (numeric, _currency_options_for_token(currency_hint))
        } if numeric.is_finite() else set()
    if not isinstance(value, str):
        return set()

    raw = value.strip()
    embedded_percentages: set[tuple[Decimal, frozenset[str]]] = set()
    for match in _NUMBER_TOKEN_RE.finditer(raw):
        if not match.group("percent"):
            continue
        percent_token = f"{match.group('sign') or ''}{match.group('number')}{match.group('percent')}"
        for item in _parse_localized_decimals(percent_token):
            embedded_percentages.add((item / Decimal("100"), frozenset()))
    explicit_percent = bool(re.search(r"[%\N{FULLWIDTH PERCENT SIGN}]", raw))
    embedded_currency = _currency_options_in_canonical_value(raw)
    iso_codes = "|".join(sorted(_ISO_CURRENCY_CODES))
    stripped = re.sub(
        rf"^(?:[{re.escape(_CURRENCY_SYMBOLS)}]|(?:{iso_codes}))\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(
        rf"\s*(?:[{re.escape(_CURRENCY_SYMBOLS)}]|(?:{iso_codes}))$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    if not _NUMBER_TOKEN_RE.fullmatch(stripped):
        return embedded_percentages
    return embedded_percentages | {
        (
            item / Decimal("100") if explicit_percent else item,
            _currency_options_for_token(currency_hint) or embedded_currency,
        )
        for item in _parse_localized_decimals(stripped)
    }


def _adjacent_currency_options(
    markdown: str,
    match: re.Match[str],
) -> frozenset[str] | None:
    before = markdown[max(0, match.start() - 16) : match.start()]
    after = markdown[match.end() : match.end() + 16]
    tokens: list[str] = []
    symbol_class = re.escape(_CURRENCY_SYMBOLS)
    symbol_before = re.search(rf"([{symbol_class}])\s*$", before)
    symbol_after = re.match(rf"^\s*([{symbol_class}])", after)
    if symbol_before:
        tokens.append(symbol_before.group(1))
    if symbol_after:
        tokens.append(symbol_after.group(1))
    code_pattern = "|".join(sorted(_ISO_CURRENCY_CODES))
    code_before = re.search(rf"\b({code_pattern})\s*$", before, re.IGNORECASE)
    code_after = re.match(rf"^\s*({code_pattern})\b", after, re.IGNORECASE)
    if code_before:
        tokens.append(code_before.group(1))
    if code_after:
        tokens.append(code_after.group(1))
    if not tokens:
        return None
    options = [_currency_options_for_token(item) for item in tokens]
    compatible = set(options[0])
    for item in options[1:]:
        compatible.intersection_update(item)
    return frozenset(compatible)


def _currency_options_in_canonical_value(value: str) -> frozenset[str]:
    code_pattern = "|".join(sorted(_ISO_CURRENCY_CODES))
    prefix = re.match(
        rf"^\s*(?:([{re.escape(_CURRENCY_SYMBOLS)}])|({code_pattern})(?=\s|[+\-(\d]))",
        value,
        re.IGNORECASE,
    )
    suffix = re.search(
        rf"(?:([{re.escape(_CURRENCY_SYMBOLS)}])|({code_pattern}))\s*$",
        value,
        re.IGNORECASE,
    )
    token = (prefix or suffix)
    if token is None:
        return frozenset()
    return _currency_options_for_token(token.group(1) or token.group(2) or "")


def _currency_options_for_token(value: str) -> frozenset[str]:
    token = str(value or "").strip().upper()
    if not token:
        return frozenset()
    if token == "RMB":
        return frozenset({"CNY"})
    if token in _CURRENCY_SYMBOL_CODES:
        return _CURRENCY_SYMBOL_CODES[token]
    if token in _ISO_CURRENCY_CODES:
        return frozenset({token})
    return frozenset()


def _has_unambiguous_decimal(raw_number: str) -> bool:
    compact = re.sub(
        r"[ \N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}'\N{RIGHT SINGLE QUOTATION MARK}]",
        "",
        raw_number,
    )
    separators = [index for index, char in enumerate(compact) if char in ".,"]
    if not separators:
        return False
    trailing = len(compact) - separators[-1] - 1
    if len(separators) == 1 and trailing == 3:
        return False
    if len(separators) > 1 and len(set(compact[index] for index in separators)) == 1:
        groups = re.split(r"[.,]", compact)
        if all(len(item) == 3 for item in groups[1:]):
            return False
    return trailing >= 2


def _is_structural_number(
    markdown: str,
    match: re.Match[str],
    *,
    has_currency: bool,
    is_percent: bool,
) -> bool:
    line_start = markdown.rfind("\n", 0, match.start()) + 1
    line_end = markdown.find("\n", match.end())
    if line_end < 0:
        line_end = len(markdown)
    line = markdown[line_start:line_end]
    number_start = match.start("number") - line_start
    number_end = match.end("number") - line_start

    date_patterns = (
        r"(?<!\d)(?:19|20)\d{2}[./-](?:0?[1-9]|1[0-2])(?:[./-](?:0?[1-9]|[12]\d|3[01]))?(?!\d)",
        r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}(?!\d)",
    )
    for pattern in date_patterns:
        for date_match in re.finditer(pattern, line):
            if date_match.start() <= number_start and number_end <= date_match.end():
                return True

    prefix = line[:number_start]
    if re.search(
        r"(?:\b(?:version|ver|page|section|chapter|seite|abschnitt|kapitel)\b|"
        r"版本|版次|页码|章节|章号|节号|§)\s*[:：#-]?\s*$",
        prefix,
        re.IGNORECASE,
    ):
        return True

    compact_number = re.sub(r"[^0-9.,]", "", match.group("number"))
    components = re.split(r"[.,]", compact_number)
    structural_prefix = re.fullmatch(r"\s*(?:#{1,6}\s*)?(?:第\s*)?", prefix)
    suffix = line[number_end:]
    if (
        not has_currency
        and not is_percent
        and structural_prefix
        and len(components) >= 2
        and all(len(item) <= 3 for item in components)
        and re.match(r"\s*(?:章|节|部分|[.)、]|\s|$)", suffix)
    ):
        return True
    return False


def _parse_localized_decimals(raw: str) -> set[Decimal]:
    text = raw.strip()
    negative = bool(re.search(r"[\-\N{MINUS SIGN}]", text)) or (
        text.startswith("(") and text.endswith(")")
    )
    text = re.sub(r"[%\N{FULLWIDTH PERCENT SIGN}()\s+\-\N{MINUS SIGN}]", "", text)
    text = re.sub(
        r"[\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}'\N{RIGHT SINGLE QUOTATION MARK}]",
        "",
        text,
    )
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", text):
        return set()

    normalized: set[str] = set()
    if "." in text and "," in text:
        decimal_separator = "." if text.rfind(".") > text.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        normalized.add(text.replace(grouping_separator, "").replace(decimal_separator, "."))
    elif "." in text or "," in text:
        separator = "." if "." in text else ","
        parts = text.split(separator)
        if len(parts) > 2:
            if all(len(item) == 3 for item in parts[1:]):
                normalized.add("".join(parts))
            else:
                normalized.add("".join(parts[:-1]) + "." + parts[-1])
        else:
            normalized.add(parts[0] + "." + parts[1])
            if len(parts[1]) == 3:
                normalized.add("".join(parts))
    else:
        normalized.add(text)

    result: set[Decimal] = set()
    for item in normalized:
        try:
            value = Decimal(item)
        except InvalidOperation:
            continue
        if negative:
            value = -abs(value)
        if value.is_finite():
            result.add(value)
    return result


def _matches_packet_numeric(
    value: Decimal,
    *,
    is_percent: bool,
    report_currency: frozenset[str] | None,
    allowed: set[tuple[Decimal, frozenset[str]]],
) -> bool:
    projected_value = value / Decimal("100") if is_percent else value
    for admitted, admitted_currencies in allowed:
        if report_currency is not None and not (
            report_currency and admitted_currencies and report_currency & admitted_currencies
        ):
            continue
        if projected_value == admitted:
            return True
    return False


def _finding_lines(title: str, findings: list[ConsumerLeafFinding]) -> list[str]:
    if not findings:
        return []
    lines = ["", f"### {title}"]
    for item in findings:
        refs = [*item.claim_ids, *item.binding_ids, *item.witness_ids]
        ref_text = "、".join(f"`{ref}`" for ref in refs) or "无可传递引用"
        facets = "、".join(f"`{facet}`" for facet in item.facet_refs) or "未标注 facet"
        lines.append(
            f"- `{item.root_requirement_id}` / `{item.check_id}`；facet：{facets}；"
            f"Kernel 接纳引用：{ref_text}。"
        )
    return lines


def _reject_execution_or_approval_claims(markdown: str) -> None:
    patterns = (
        r"(?:可|可以|建议|应当)\s*(?:直接)?\s*(?:付款|支付|过账|审批|批准|提交)",
        r"(?:最终审批|付款审批)\s*(?:通过|完成|已完成)",
        r"\b(?:ready|safe|approved)\s+to\s+(?:pay|post|approve|submit)\b",
    )
    if any(re.search(pattern, markdown, re.I) for pattern in patterns):
        raise ValueError("report translates proof into an ERP execution or approval conclusion")


def _reject_partial_overclaim(markdown: str) -> None:
    patterns = (
        r"(?:本案|整体|全部|所有要求|完整审查).{0,12}(?:支持|通过|满足|已核实|已完成)",
        r"(?:审查|审核).{0,4}(?:全部|整体)?(?:已经|已)?完成(?!度)",
        r"\b(?:fully|overall|all requirements).{0,20}(?:supported|verified|complete)\b",
    )
    clauses = re.split(r"[\n。！？；;，,]+", markdown)
    for clause in clauses:
        for index, pattern in enumerate(patterns):
            for match in re.finditer(pattern, clause, re.I):
                if index == 1 and re.search(r"(?:尚未|并未|没有|未|不)", match.group(0)):
                    continue
                raise ValueError("PARTIAL report overstates a whole-case conclusion")


def _execution_status(artifact: Any) -> ExecutionStatus:
    if artifact is None:
        return "FAILED"
    value = str(getattr(artifact, "execution_status", "COMPLETED") or "COMPLETED").upper()
    return value if value in {"COMPLETED", "PARTIAL", "FAILED"} else "FAILED"  # type: ignore[return-value]


def _integrity_rejected(artifact: Any, proof: Any) -> bool:
    if artifact is None or proof is None:
        return False
    declared_hash = str(getattr(artifact, "artifact_hash", "") or "")
    if declared_hash:
        try:
            if declared_hash != str(artifact.content_hash()):
                return True
        except (AttributeError, TypeError, ValueError):
            return True
    fatal_codes = {
        "STALE_PLAN",
        "STALE_EVIDENCE_SNAPSHOT",
        "UNKNOWN_CHECK_ASSESSMENT",
        "NON_CHECK_ASSESSMENT",
        "ARTIFACT_HASH_MISMATCH",
        "PLAN_CONFORMANCE_FAILED",
    }
    for item in list(getattr(proof, "diagnostics", []) or []):
        if not bool(getattr(item, "blocking", True)):
            continue
        code = str(getattr(item, "code", "") or "").upper()
        node_id = str(getattr(item, "node_id", "") or "")
        if code in fatal_codes or any(token in code for token in ("INTEGRITY", "HASH_MISMATCH")):
            return True
        if not node_id and code not in {"POLICY_NOT_CONFIGURED"}:
            return True
    return False


def _reachable_check_ids(root_node_id: str, nodes: dict[str, Any]) -> list[str]:
    reachable = reachable_ids(
        root_node_id,
        lambda node_id: (
            list(getattr(nodes[node_id], "depends_on", []) or [])
            if node_id in nodes and str(getattr(nodes[node_id], "kind", "") or "") != "CHECK"
            else ()
        ),
    )
    return sorted(
        node_id
        for node_id in reachable
        if node_id in nodes and str(getattr(nodes[node_id], "kind", "") or "") == "CHECK"
    )


def _decisive_check_ids(decision: Any) -> set[str]:
    status = str(getattr(decision, "status", "NOT_FOUND") or "NOT_FOUND")
    field = {
        "SUPPORTED": "supporting_check_ids",
        "CONTRADICTED": "contradicting_check_ids",
        "NOT_FOUND": "unresolved_check_ids",
    }[status]
    return {str(item) for item in list(getattr(decision, field, []) or [])}


def _referenced_terms(
    artifact: Any,
    findings: list[ConsumerLeafFinding],
) -> tuple[list[ConsumerClaim], list[ConsumerBinding], list[CalculationWitness], dict[str, str]]:
    evidence_ir = getattr(artifact, "evidence_ir", None)
    claim_by_id = {
        str(getattr(item, "id", "") or ""): item
        for item in list(getattr(evidence_ir, "claims", []) or [])
    }
    binding_by_id = {
        str(getattr(item, "id", "") or ""): item
        for item in list(getattr(artifact, "binding_proposals", []) or [])
    }
    witness_by_id = {
        str(getattr(item, "id", "") or ""): item
        for item in list(getattr(artifact, "calculation_witnesses", []) or [])
    }
    claim_ids = {item for finding in findings for item in finding.claim_ids}
    binding_ids = {item for finding in findings for item in finding.binding_ids}
    witness_ids = {item for finding in findings for item in finding.witness_ids}
    pending_bindings = list(binding_ids)
    pending_witnesses = list(witness_ids)
    while pending_bindings:
        binding_id = pending_bindings.pop()
        binding = binding_by_id.get(binding_id)
        if binding is None:
            continue
        for ref in list(getattr(binding, "term_refs", []) or []):
            kind = str(getattr(ref, "kind", "") or "")
            ref_id = str(getattr(ref, "ref_id", "") or "")
            if kind == "CLAIM":
                claim_ids.add(ref_id)
            elif kind == "WITNESS" and ref_id not in witness_ids:
                witness_ids.add(ref_id)
                pending_witnesses.append(ref_id)
    while pending_witnesses:
        witness_id = pending_witnesses.pop()
        witness = witness_by_id.get(witness_id)
        if witness is None:
            continue
        for operand in list(getattr(witness, "operands", []) or []):
            ref = getattr(operand, "ref", None)
            kind = str(getattr(ref, "kind", "") or "")
            ref_id = str(getattr(ref, "ref_id", "") or "")
            if kind == "CLAIM":
                claim_ids.add(ref_id)
            elif kind == "WITNESS" and ref_id not in witness_ids:
                witness_ids.add(ref_id)
                pending_witnesses.append(ref_id)

    claims = [_consumer_claim(claim_by_id[item]) for item in sorted(claim_ids) if item in claim_by_id]
    bindings = [
        ConsumerBinding(
            id=str(getattr(binding_by_id[item], "id", "") or ""),
            check_id=str(getattr(binding_by_id[item], "check_id", "") or ""),
            facet_ref=str(getattr(binding_by_id[item], "facet_ref", "") or ""),
            relation=str(getattr(binding_by_id[item], "relation", "") or ""),
            term_refs=list(getattr(binding_by_id[item], "term_refs", []) or []),
        )
        for item in sorted(binding_ids)
        if item in binding_by_id
    ]
    witnesses = [witness_by_id[item] for item in sorted(witness_ids) if item in witness_by_id]
    source_ids = {
        *[item.source_id for item in claims],
        *[source_id for finding in findings for source_id in finding.source_ids],
    }
    fingerprints = {
        str(source_id): str(fingerprint)
        for source_id, fingerprint in dict(getattr(evidence_ir, "source_fingerprints", {}) or {}).items()
        if str(source_id) in source_ids
    }
    return claims, bindings, witnesses, fingerprints


def _consumer_claim(claim: Any) -> ConsumerClaim:
    attributes = dict(getattr(claim, "attributes", {}) or {})
    return ConsumerClaim(
        id=str(getattr(claim, "id", "") or ""),
        subject=str(getattr(claim, "subject", "") or ""),
        predicate=str(getattr(claim, "predicate", "") or ""),
        value=getattr(claim, "value", None),
        source_id=str(getattr(claim, "source_id", "") or ""),
        quote=str(getattr(claim, "quote", "") or ""),
        locator=str(getattr(claim, "locator", "") or ""),
        confidence=str(getattr(claim, "confidence", "") or ""),
        currency=str(attributes.get("currency") or ""),
        unit=str(attributes.get("unit") or ""),
    )


def _artifact_hash(artifact: Any) -> str:
    if artifact is None:
        return ""
    declared = str(getattr(artifact, "artifact_hash", "") or "")
    if declared:
        return declared
    try:
        return str(artifact.content_hash())
    except (AttributeError, TypeError, ValueError):
        return _stable_hash(_model_dump(artifact))


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalConsumerPacket",
    "ConsumerBinding",
    "ConsumerClaim",
    "ConsumerLeafFinding",
    "ConsumerLineage",
    "ConsumerObligation",
    "ConsumerRootDecision",
    "ExecutionStatus",
    "Reportability",
    "derive_consumer_packet",
    "finalize_consumer_report",
    "partial_report_appendix",
    "render_consumer_report",
    "validate_canonical_report_projection",
]
