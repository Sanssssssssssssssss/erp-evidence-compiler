"""Counterexamples for the actual source/number interface errors, without a model."""
import hashlib
import asyncio
import json
import pytest
from app.compiler_runtime.models import RecordFieldLocator, EvidenceIR, EvidenceSourceDescriptor
from app.compiler_runtime.proof_terms import replay_calculation_witness
from app.compiler_runtime.runtime import _sandbox_tools
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord


def sandbox(text="Limit: 100.\nQuantity: 12."):
    records = [SourceRecord(source_id="doc", kind="document", content=text),
               SourceRecord(source_id="record", kind="record", content="", record_model="request", record_revision="r1", structured_fields={"amount": 90.25})]
    hashes = {source.source_id: hashlib.sha256(source.content.encode()).hexdigest() for source in records}
    result = EvidenceSandbox(
        sources=records,
        evidence_ir=EvidenceIR(source_ids=list(hashes), source_fingerprints=hashes, source_revisions={"record": "r1"}, source_descriptors={source.source_id: EvidenceSourceDescriptor(source_type=source.kind, fingerprint=hashes[source.source_id], revision=source.record_revision, record_model=source.record_model) for source in records}),
        allowed_check_ids=["c"], allowed_check_facets={"c": ["f"]},
        policy_snapshot_hash="policy:fixed",
    )
    for source in result.source_records:
        result.read_source(source.source_id)
    return result


def bind(result, quote="Limit: 100.", **kwargs):
    return result.bind_claim(source_id="doc", subject="policy", predicate="limit", value="100", quote=quote, **kwargs)


def test_unique_and_cross_line_quotes_get_real_locations_not_model_guesses():
    result = sandbox()
    first = bind(result)
    assert first["ok"], first
    assert result.evidence_ir.claims[0].locator == "line 1"
    multi = bind(result, quote="Limit: 100.\nQuantity: 12.")
    assert multi["ok"], multi
    assert result.evidence_ir.claims[-1].locator == "line 1-2"


def test_ambiguous_quote_and_explicit_wrong_line_remain_rejected():
    ambiguous = bind(sandbox("Limit: 100.\nLimit: 100."))
    assert not ambiguous["ok"] and ambiguous["error"]["code"] == "LOCATOR_AMBIGUOUS"
    wrong = bind(sandbox(), locator="line 9")
    assert not wrong["ok"] and wrong["error"]["code"] == "LOCATOR_OUT_OF_RANGE"
    in_range_wrong = bind(sandbox(), locator="line 2")
    assert not in_range_wrong["ok"] and in_range_wrong["error"]["code"] == "LOCATOR_QUOTE_MISMATCH"
    overlap = bind(sandbox("banana"), quote="ana")
    assert not overlap["ok"] and overlap["error"]["code"] == "LOCATOR_AMBIGUOUS"


def test_real_json_decimal_record_can_compute_without_changing_claim_value():
    result = sandbox()
    limit = bind(result, claim_id="limit")
    assert limit["ok"]
    locator = RecordFieldLocator(record_ref="record", record_revision="r1", field_path="/amount")
    amount = result.bind_record_field_claim(source_id="record", subject="record", predicate="amount", value=90.25, locator=locator, claim_id="amount")
    assert amount["ok"], amount
    computed = result.compute_witness(check_id="c", facet_ref="f", operation="LTE", refs=[{"kind": "CLAIM", "ref_id": "amount"}, {"kind": "CLAIM", "ref_id": "limit"}])
    assert computed["ok"], computed
    assert result.calculation_witnesses[0].result is True
    assert replay_calculation_witness(result.calculation_witnesses[0], claims={claim.id: claim for claim in result.evidence_ir.claims}, witnesses={}, policy_values={})
    assert next(claim for claim in result.evidence_ir.claims if claim.id == "amount").value == 90.25
    wrong = result.bind_record_field_claim(source_id="record", subject="record", predicate="amount", value=91.25, locator=locator)
    assert not wrong["ok"]
    floating_document = result.bind_claim(source_id="doc", subject="policy", predicate="limit", value=100.0, quote="Limit: 100.")
    assert not floating_document["ok"]


def test_reference_ids_resolve_to_the_same_canonical_witness():
    result = sandbox()
    assert bind(result, claim_id="limit")["ok"]
    args = dict(check_id="c", facet_ref="f", operation="EQUAL")
    typed = result.compute_witness(**args, refs=[{"kind": "CLAIM", "ref_id": "limit"}] * 2)
    ids = result.compute_witness(**args, refs=["limit", "limit"])
    assert ids["ok"], ids
    assert ids["witness"] == typed["witness"]
    assert ids["duplicate"] is True


@pytest.mark.parametrize("collision", [False, True])
def test_reference_ids_reject_unknown_or_ambiguous_without_mutation(collision):
    result = sandbox()
    assert bind(result, claim_id="limit")["ok"]
    if collision:
        result._allowed_check_policy_refs = {"c": frozenset({"limit"})}
        result._policy_values = {"limit": "100"}
    before = result.evidence_ir.content_hash()
    failed = result.compute_witness(check_id="c", facet_ref="f", operation="EQUAL", refs=["limit" if collision else "unknown", "limit"])
    assert not failed["ok"]
    assert failed["error"]["code"] == ("WITNESS_REFERENCE_AMBIGUOUS" if collision else "WITNESS_REFERENCE_UNKNOWN")
    assert result.evidence_ir.content_hash() == before
    assert not result.calculation_witnesses


def test_id_only_tool_does_not_change_units_or_expose_reference_kind():
    result = sandbox()
    assert bind(result, claim_id="scalar")["ok"]
    assert bind(result, claim_id="usd", attributes={"currency": "USD"})["ok"]
    tools = _sandbox_tools(result, reference_ids_only=True)
    tool = next(item for item in tools if item.name == "compute_witness")
    assert tool.params_json_schema["properties"]["refs"]["items"]["type"] == "string"
    args = {"check_id": "c", "facet_ref": "f", "operation": "EQUAL", "refs": ["scalar", "usd"]}
    failed = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
    assert not failed["ok"]
    assert "dimensioned and dimensionless" in failed["error"]["message"]
    assert not result.calculation_witnesses


@pytest.mark.parametrize("defect", ["", "value", "revision", "source_scope", "duplicate_identity"])
def test_record_field_tool_derives_identity_but_still_validates_observations(defect):
    result = sandbox()
    tool = next(item for item in _sandbox_tools(result, allowed_source_ids={"record"} if defect != "source_scope" else {"doc"})
        if item.name == "bind_record_field_claim")
    assert not {"subject", "source_id", "value"} & tool.params_json_schema["properties"].keys()
    args = dict(predicate="amount", claim_id="amount",
        locator=dict(record_ref="record", record_revision="r1", field_path="/amount"))
    if defect == "value":
        args["value"] = 91.25
    if defect == "revision":
        args["locator"]["record_revision"] = "stale"
    if defect == "duplicate_identity":
        args["subject"] = "another-record"
    response = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
    assert response["ok"] == (not defect), response
    if not defect:
        claim = result.evidence_ir.claims[0]
        assert claim.source_id == claim.subject == claim.locator.record_ref == "record"
        assert type(claim.value) is float and claim.value == 90.25
    else:
        assert not result.evidence_ir.claims
