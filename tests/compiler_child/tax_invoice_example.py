"""Official masked documentation replay. --run uses one real model child review."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.compiler_runtime.sandbox import SourceRecord
from erp_agent_odoo.tax_invoice import FIELD_MAP, INVOICE_MODEL, receipt_source, wire

HERE = Path(__file__).parent
API_DOC = "https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-verifyvatinvoice"
FORM_DOC = "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5236067/content.html"


def example():
    response = json.loads((HERE / "tax_invoice_official_response.json").read_text(encoding="utf-8"))
    fields = {key: response["Data"]["data"][value] for key, value in FIELD_MAP.items()}
    fields["invoice_type"] = str(fields["invoice_type"]).zfill(2)
    invoice = SourceRecord(source_id="invoice:aliyun-documentation", content="", kind="record",
        record_model=INVOICE_MODEL, record_revision="example:r1", structured_fields=fields,
        provenance={"role": "evidence", "example": True, "origin": API_DOC,
                    "extraction": "Copied from the provider documentation response; no original issued invoice is supplied."})
    receipt = receipt_source(invoice, response, mode="documentation", observed_at="2026-09-07T00:00:00+00:00",
                             raw_ref=str(HERE / "tax_invoice_official_response.json"))
    policy = SourceRecord(source_id="policy:tax-review", kind="document", content=(
        "Review the proposed tax invoice without paying or posting it. Registry verification requires a live response "
        "for the same original invoice, observed on the review date, with matching invoice identity and normal status N. "
        "Compare invoice type, number, date, applicable legacy code, buyer and seller names and tax identifiers, "
        "untaxed amount, tax and total with the independently obtained provider record. Required identities must be "
        "complete; amounts must be equal in CNY. Official form references explain field meaning and apply only to "
        "their stated invoice type. Review date: 2026-09-07."), provenance={"role": "instruction"})
    form = SourceRecord(source_id="reference:official-tax-form", kind="document", content=(
        "Reference notes from State Taxation Administration announcement 2024 No.11, sections 2-4 and attachment 1. "
        "Scope: fully digital invoices (数电发票), including VAT special and ordinary invoices. The invoice number has "
        "20 digits. Header fields include 发票名称, 发票号码, 开票日期; party blocks contain 购买方信息 and 销售方信息. "
        "Amount (金额/合计), tax (税额) and tax-inclusive total (价税合计) are separate fields. "
        "Line items also include 项目名称, 规格型号, 单位, 数量, 单价 and 税率/征收率; the form includes 备注 and 开票人. "
        "These are format references, not issued transaction evidence. This note is a field-level reference, not OCR "
        "or a visual comparison of a supplied invoice."), provenance={"role": "evidence", "origin": FORM_DOC, "reference_only": True})
    proposal = dict(scenario_id="aliyun-documentation-review", proposal_id="proposal:aliyun-documentation:r1",
        task_objective="Review whether the supplied invoice can pass tax-invoice review using all admitted materials.",
        actions=[dict(action_id="review", action_kind="cn.tax_invoice.review", stage="verify",
            target_record_refs=[invoice.source_id], action_payload=dict(payload_version=1, snapshot_revision="example:r1",
                records=[dict(record_ref=invoice.source_id, record_revision=invoice.record_revision,
                              values={"invoice_source_id": invoice.source_id})]))])
    return proposal, [policy, form, invoice, receipt]


def run(output):
    from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK as pack
    from erp_agent_odoo.capabilities.proof_dag import action_proposal_from_manager_request, load_proof_catalog
    from erp_agent_odoo.compiler_child.extension import _run_compiler, _write_json

    # Credentials stay in the environment; this file never loads or copies a secret file.
    if os.environ.get("LLM_MODEL") != "deepseek/deepseek-v4-flash":
        raise ValueError("Set the existing DeepSeek test profile before this explicitly paid probe")
    output.mkdir(parents=True, exist_ok=False)
    proposal, sources = example()
    request = dict(task_objective=proposal["task_objective"], requirement_pack_id=pack.pack_id,
        requirement_pack_version=pack.version, requirement_pack_hash=pack.content_hash,
        active_requirement_ids=["erp_action_plan_valid"], sources=[wire(s) for s in sources],
        catalog=load_proof_catalog(), policy_excerpt=pack.policy_excerpt_for(["erp_action_plan_valid"]),
        requirement_requiredness={"erp_action_plan_valid": True},
        action_proposal=action_proposal_from_manager_request(proposal).model_dump(mode="json"))
    _write_json(output / "request.json", request)
    def progress(kind, payload, summary):
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(kind=kind, payload=payload, summary=summary), ensure_ascii=False, default=str) + "\n")
    result = _run_compiler(request, None, proposal["scenario_id"], output,
                           lambda c: _write_json(output / "checkpoint.json", c), progress)
    _write_json(output / "result.json", result)
    checkpoint = result["checkpoint"]
    print(json.dumps({key: checkpoint.get(key) for key in ("status", "compile_status", "semantic_status")}))
    print((output / "stage-usage.json").read_text(encoding="utf-8"))
    # Expected result is test-only; never sent to the model or admitted as evidence.
    assert checkpoint["compile_status"] == "COMMITTED" and checkpoint["semantic_status"] == "NOT_FOUND"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, help="One real-model child run in a new output directory")
    args = parser.parse_args()
    if args.run:
        run(args.run)
    else:
        proposal, sources = example()
        print(json.dumps(dict(manager_request=proposal, sources=[wire(s) for s in sources]), ensure_ascii=False, indent=2))
