"""The planned tool reuses the calculator; it cannot invent fields or verdicts."""
import asyncio
import hashlib
import json

import pytest

from app.compiler_runtime.kernel import _numeric_program_failure
from app.compiler_runtime.models import EvidenceIR, EvidenceSourceDescriptor, NumericDecisionContract, ERPProposalRecord
from app.compiler_runtime.runtime import _sandbox_tools
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord
from test_atomic_amounts import FIELDS, PROGRAMS, plan_for


def setup(key, defect=""):
    fields = dict(FIELDS)
    decision = NumericDecisionContract.model_validate(PROGRAMS[key])
    if defect == "missing":
        del fields[decision.steps[0].operands[0].ref_id[1:]]
    source = SourceRecord(source_id="target", kind="record", content="", record_model=decision.record_model,
                          record_revision="r1", structured_fields=fields)
    node = next(n for n in plan_for("customer_invoice_post.v1", "account.move.action_post", "regular", FIELDS).nodes
                if n.kind == "CHECK" and n.action_contract.numeric_decision)
    proposal = ERPProposalRecord(action_id="review", action_kind="account.move.action_post", record_ref="target",
                                 record_revision="r1", values={v: FIELDS[k[1:]] for k, v in decision.proposal_fields.items()})
    node.action_contract = node.action_contract.model_copy(update={"numeric_decision": decision, "proposal_records": [proposal]})
    if defect == "revision":
        node.action_contract = node.action_contract.model_copy(update={"proposal_records": [proposal.model_copy(update={"record_revision": "old"})]})
    if defect == "target":
        node.action_contract = node.action_contract.model_copy(update={"target_record_refs": ["other"]})
    fingerprint = hashlib.sha256(source.content.encode()).hexdigest()
    state = EvidenceSandbox(sources=[source], evidence_ir=EvidenceIR(source_ids=["target"],
        source_fingerprints={"target": fingerprint}, source_revisions={"target": "r1"},
        source_descriptors={"target": EvidenceSourceDescriptor(source_type="derived_fact", fingerprint=fingerprint,
            revision="r1", record_model=decision.record_model)}),
        allowed_check_ids=[node.id], allowed_check_facets={node.id: node.facet_refs}, policy_snapshot_hash="fixed")
    state.read_source("target")
    if defect == "fingerprint":
        state._base_ir.source_fingerprints["target"] = "tampered"
    tool = next(t for t in _sandbox_tools(state, reference_ids_only=True, numeric_checks=[node],
        allowed_source_ids=frozenset({"other"} if defect == "scope" else {"target"})) if t.name == "compute_planned_witnesses")
    return node, source, state, tool


@pytest.mark.parametrize("key", PROGRAMS)
def test_all_planned_programs_match_existing_engine_and_kernel(key):
    node, source, state, tool = setup(key)
    result = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps({"check_id": node.id}))))
    assert result["ok"], result
    witnesses = {w.id: w for w in state.calculation_witnesses}
    assert _numeric_program_failure(node.action_contract, witnesses[result["terminal_witness_id"]], witnesses,
        {c.id: c for c in state.evidence_ir.claims}, {"target": source}) is None
    assert not state.submissions and not state.binding_proposals
    before = state.evidence_ir.content_hash(), [w.model_dump(mode="json") for w in state.calculation_witnesses]
    repeated = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps({"check_id": node.id}))))
    assert repeated == result
    assert before == (state.evidence_ir.content_hash(), [w.model_dump(mode="json") for w in state.calculation_witnesses])


@pytest.mark.parametrize("defect", ["missing", "target", "scope", "fingerprint", "revision", "check", "forged_value"])
def test_planned_tool_rejects_missing_or_out_of_scope_inputs(defect):
    node, _, state, tool = setup("invoice_total_matches_untaxed_and_tax", defect)
    args = {"check_id": "outside" if defect == "check" else node.id}
    if defect == "forged_value":
        args["value"] = 0
    result = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps(args))))
    assert not result["ok"], result
    assert not state.calculation_witnesses and not state.submissions and not state.binding_proposals
