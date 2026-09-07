"""Trust-boundary checks for the optional tax-invoice observation tool."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.compiler_runtime.models import EvidenceIR, EvidenceSourceDescriptor
from app.compiler_runtime.runtime import _sandbox_tools
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord
from erp_agent_odoo import tax_invoice as tax
from erp_agent_odoo.capabilities.proof_dag import compile_task_compiler_plan, lower_erp_stage_to_proof_plan, load_proof_catalog
from erp_agent_odoo.compiler_child import extension
from tests.compiler_v1.proof_corpus.test_proof_dag_compiler import _bindings_for_request
from tests.compiler_child.tax_invoice_example import example


def invoice(**changes):
    fields = dict(invoice_type="01", invoice_code="011001800001", invoice_number="12345678",
        invoice_date="20260907", amount_untaxed="100.00", amount_tax="13.00", amount_total="113.00")
    fields.update(changes)
    return SourceRecord(source_id="invoice:offline-test", content="", kind="record", record_model=tax.INVOICE_MODEL,
        record_revision="r1", structured_fields=fields, provenance={"role": "evidence", "verify_tax_invoice": "aliyun"})


def test_documented_request_fields_and_sdk(monkeypatch):
    legacy = tax.request_fields(invoice().record_fields)
    digital = tax.request_fields(invoice(invoice_type="31", invoice_number="26000000000000000001").record_fields)
    ordinary = tax.request_fields(invoice(invoice_type="10", verify_code="123456").record_fields)
    assert legacy["InvoiceSum"] == "100.00" and digital["InvoiceSum"] == "113.00"
    assert "InvoiceCode" not in digital and "InvoiceSum" not in ordinary and ordinary["VerifyCode"] == "123456"
    for change in ({"invoice_number": "1234XXXX"}, {"invoice_type": []}, {"invoice_type": "51"}, {"invoice_date": "20260230"},
                   {"invoice_type": "04", "verify_code": "123"}, {"amount_untaxed": "NaN"}):
        with pytest.raises(ValueError):
            tax.request_fields(invoice(**change).record_fields)
    client = pytest.importorskip("alibabacloud_ocr_api20210707.client")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "offline-test")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "offline-test")
    calls = []
    def fake(self, request, runtime):
        calls.append(request.to_map())
        assert runtime.autoretry is False and runtime.read_timeout == 15000
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {"Data": '{"code":"009","data":null}'}))
    monkeypatch.setattr(client.Client, "verify_vatinvoice_with_options", fake)
    assert tax._call_aliyun(legacy)["Data"] and calls == [legacy]


def test_provider_receipts_and_documentation_are_not_conflated():
    for code, state in (("001", "MATCH"), ("006", "MISMATCH"), ("009", "NOT_FOUND"), ("104", "UNAVAILABLE"), ("105", "UNAVAILABLE")):
        data = dict(code=code, data={"invoiceNumber": "12345678"} if code == "001" else None)
        for payload in (data, json.dumps(data)):
            receipt = tax.receipt_source(invoice(), {"Data": payload}, mode="live", observed_at="now")
            assert receipt.record_fields["verification_state"] == state
            assert "invalid_mark" not in receipt.record_fields
    for payload in ("broken json", [], {"data": "bad"}, {"code": "001", "data": {}}):
        r = tax.receipt_source(invoice(), {"Data": payload}, mode="live", observed_at="now")
        assert r.record_fields["verification_state"] == "UNAVAILABLE" and r.record_fields["error"]
    _, sources = example()
    assert sources[-1].record_fields["verification_state"] == "DOCUMENTATION_ONLY"
    assert sources[-1].record_fields["invoice"]["amount_untaxed"] == "322.XX"


def test_capture_is_opt_in_preseal_and_durable(tmp_path, monkeypatch):
    raw = {"RequestId": "offline-test", "Data": "broken json"}
    calls = []
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "offline-test")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "offline-test")
    monkeypatch.setattr(tax, "_call_aliyun", lambda q: calls.append(q) or raw)
    original = tax.wire(invoice())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [original]}), encoding="utf-8")
    monkeypatch.setenv("ERP_COMPILER_SOURCE_MANIFEST", str(manifest))
    captured = extension._resolve_sources([original["source_id"]], tax_receipt_dir=tmp_path / "receipts")
    assert len(captured) == 2 and len(calls) == 1
    assert captured == extension._resolve_sources([original["source_id"]], tax_receipt_dir=tmp_path / "receipts")
    assert len(calls) == 1
    path = next((tmp_path / "receipts").glob("*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["raw_response"] == raw
    fields = json.loads(saved["receipt"]["source_content"])["fields"]
    assert fields["error"] == "MALFORMED_PROVIDER_RESPONSE" and fields["verification_state"] == "UNAVAILABLE"
    saved["raw_response"] = {}
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="hashes"):
        tax.collect_tax_invoice_sources([original], tmp_path / "receipts")
    assert len(calls) == 1
    ordinary = dict(original, provenance={"role": "evidence"})
    assert tax.collect_tax_invoice_sources([ordinary], tmp_path / "unused") == [ordinary]
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    unavailable = tax.collect_tax_invoice_sources([original], tmp_path / "missing")[-1]
    assert json.loads(unavailable["source_content"])["fields"]["error"] == "CREDENTIALS_NOT_CONFIGURED"
    assert len(calls) == 1
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "offline-test")
    def fail(q):
        raise ValueError("signed-request-secret-must-not-be-logged")
    monkeypatch.setattr(tax, "_call_aliyun", fail)
    failed = tax.collect_tax_invoice_sources([original], tmp_path / "failed")[-1]
    assert "signed-request-secret" not in failed["source_content"]
    assert json.loads(failed["source_content"])["fields"]["error"] == "ValueError"


def tool_setup(defect):
    proposal, records = example()
    if defect == "amount_mismatch":
        original, prior = records[-2:]
        records[-2] = SourceRecord(source_id=original.source_id, content="", kind="record",
            record_model=original.record_model, record_revision=original.record_revision,
            structured_fields=dict(original.record_fields, amount_total="333.00"),
            provenance={"role": "evidence", "example": True})
        records[-1] = SourceRecord(source_id=prior.source_id, content="", kind="record",
            record_model=prior.record_model, record_revision=prior.record_revision,
            structured_fields=dict(prior.record_fields, invoice_fingerprint=tax.fingerprint(records[-2])))
    sources = {s.source_id: s for s in records}
    routing = dict(scenario_id=proposal["scenario_id"], selected_template_ids=[tax.TEMPLATE_ID],
        action_bindings=[dict(proposal_action_id="review", template_id=tax.TEMPLATE_ID, source_ids=list(sources))],
        unresolved_manager_inputs=[])
    expanded = compile_task_compiler_plan(compiler_output=routing, manager_request=proposal,
        catalog=load_proof_catalog(), binding_values=_bindings_for_request(proposal))
    plan = lower_erp_stage_to_proof_plan(expanded, source_records=sources)
    checks = [n for n in plan.nodes if n.kind == "CHECK"]
    assert len(checks) == 2
    node = checks[-1]
    if defect == "revision":
        node.action_contract.proposal_records[0].record_revision = "old"
    if defect == "receipt_binding":
        prior = records[-1]
        records[-1] = SourceRecord(source_id=prior.source_id, content="", kind="record", record_model=prior.record_model,
            record_revision=prior.record_revision, structured_fields=dict(prior.record_fields, invoice_fingerprint="other"))
    hashes = {s.source_id: tax.fingerprint(s) for s in records}
    if defect == "fingerprint":
        hashes[records[-1].source_id] = "0" * 64
    state = EvidenceSandbox(sources=records, evidence_ir=EvidenceIR(source_ids=list(sources), source_fingerprints=hashes,
        source_revisions={s.source_id: s.record_revision for s in records if s.record_revision},
        source_descriptors={s.source_id: EvidenceSourceDescriptor(source_type=s.kind, fingerprint=hashes[s.source_id],
            revision=s.record_revision, record_model=s.record_model, provenance=dict(s.provenance)) for s in records}),
        allowed_check_ids=[node.id], allowed_check_facets={node.id: node.facet_refs}, policy_snapshot_hash="policy")
    tools = _sandbox_tools(state, reference_ids_only=True, numeric_checks=[node],
        allowed_source_ids=frozenset(set(sources) - ({records[-1].source_id} if defect == "scope" else set())))
    return node, state, next(t for t in tools if t.name == "verify_tax_invoice")


@pytest.mark.parametrize("defect", ["", "amount_mismatch", "revision", "receipt_binding", "fingerprint", "scope", "check", "forged_value"])
def test_executor_tool_binds_exact_sides_without_a_verdict(defect):
    node, state, tool = tool_setup(defect)
    arguments = {"check_id": "other" if defect == "check" else node.id}
    if defect == "forged_value":
        arguments["value"] = "trusted"
    result = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments))))
    assert not state.submissions and not state.binding_proposals
    if defect and defect != "amount_mismatch":
        assert not result["ok"], result
        assert not state.calculation_witnesses
        return
    assert result["ok"] and result["verification_state"] == "DOCUMENTATION_ONLY"
    rows = {r["field"]: r for r in result["comparisons"]}
    assert rows["amount_untaxed"]["equal"] is None and rows["amount_untaxed"]["gap"]
    assert rows["amount_total"]["equal"] is (defect != "amount_mismatch") and len(state.calculation_witnesses) == 2
    for row in rows.values():
        assert len(set(row["claim_ids"])) == 2
    before = state.evidence_ir.content_hash(), len(state.calculation_witnesses)
    assert result == json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments))))
    assert before == (state.evidence_ir.content_hash(), len(state.calculation_witnesses))
    assert "verify_tax_invoice" not in {t.name for t in _sandbox_tools(state, reference_ids_only=True)}
