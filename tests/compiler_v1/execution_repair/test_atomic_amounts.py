"""Fixed-field and plan composition checks; semantic model outputs are not simulated here."""
from types import SimpleNamespace

import pytest

from app.compiler_runtime.kernel import _numeric_program_failure
from app.compiler_runtime.models import Claim, ERPProposalRecord, NumericDecisionContract, RecordFieldLocator
from app.compiler_runtime.proof_terms import CalculationRequest, ProofTermRef, compute_witness
from app.compiler_runtime.sandbox import SourceRecord
from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog, compile_task_compiler_plan, lower_erp_stage_to_proof_plan
from tests.compiler_v1.proof_corpus.test_proof_dag_compiler import _payload, _bindings_for_request

CATALOG = load_proof_catalog()
PROGRAMS = {key: value["numeric_decision"] for key, value in CATALOG["proof_recipes"].items() if value.get("numeric_decision", {}).get("steps")}
FIELDS = dict(quantity="10", price_unit="5", list_price="5", budget="60", min_quantity="2", max_quantity="20",
    lead_days="8", min_lead_days="3", horizon_quantity="10", vendor_cap="20", unit_cost="5", tier_price="5",
    amount_untaxed="50", order_untaxed_total="100", fixed_amount="50", downpayment_ratio="0.5",
    authorized_downpayment_amount="50", amount_tax="5", odoo_amount_tax="5", amount_total="55", currency_tolerance="0.01")


def program_terms(key, *, defect=""):
    decision = NumericDecisionContract.model_validate(PROGRAMS[key])
    source = SourceRecord(source_id="target",kind="record",content="",record_model=decision.record_model,
        record_revision="r1",structured_fields=FIELDS,provenance={"role":"evidence"})
    claims = {}
    for step in decision.steps:
        for operand in step.operands:
            if operand.kind == "RECORD_FIELD":
                pointer = operand.ref_id
                claims[pointer] = Claim(id=pointer, subject="target", predicate=pointer, value=FIELDS[pointer[1:]],source_id="target",
                    locator=RecordFieldLocator(record_ref="target",record_revision="r1",field_path=pointer))
    if defect == "wrong_field":
        claim = claims[next(iter(claims))]
        claim.locator = claim.locator.model_copy(update={"field_path":"/unrelated_amount"})
    if defect == "wrong_target":
        claim = claims[next(iter(claims))]
        claim.source_id = claim.subject = "another_target"
        claim.locator = claim.locator.model_copy(update={"record_ref":"another_target"})
    if defect == "quote_instead_of_field":
        claims[next(iter(claims))].locator = "line 1"
    values = {key:FIELDS[pointer[1:]] for pointer,key in decision.proposal_fields.items()}
    if defect == "proposal_changed":
        values[next(iter(values))] = "999"
    contract = SimpleNamespace(numeric_decision=decision,target_record_refs=["target"],source_refs=["target"],
        proposal_records=[ERPProposalRecord(action_id="release",action_kind="confirm",record_ref="target",record_revision="r1",values=values)])
    witnesses = {}
    for step in decision.steps:
        operands = [ProofTermRef(kind="WITNESS" if item.kind=="STEP" else "CLAIM",ref_id=item.ref_id) for item in step.operands]
        if defect == "wrong_formula" and step is decision.steps[0]:
            operands.reverse()
        witnesses[step.step_id] = compute_witness(CalculationRequest(id=step.step_id,check_id="check",facet_ref="complete_action_plan",operation=step.operation,operands=operands),
            claims=claims,witnesses=witnesses,policy_values={},evidence_snapshot_hash="s",policy_snapshot_hash="p")
    return contract, witnesses[decision.steps[-1].step_id], witnesses, claims, {"target":source}


@pytest.mark.parametrize("key", PROGRAMS)
def test_every_default_numeric_program_matches_its_exact_fields(key):
    assert _numeric_program_failure(*program_terms(key)) is None


@pytest.mark.parametrize("defect", ["wrong_field","wrong_target","quote_instead_of_field","proposal_changed","wrong_formula"])
def test_numeric_program_rejects_plausible_but_wrong_operand_choices(defect):
    failure = _numeric_program_failure(*program_terms("sales_untaxed_total_within_budget",defect=defect))
    assert failure and failure.code == "NUMERIC_FIELD_PROGRAM_MISMATCH"


def plan_for(template, action, stage, fields, targets=("target",), bind_targets=True):
    request = {"scenario_id":"atomic-plan","proposal_id":"atomic-plan:r1","actions":[dict(action_id="review",action_kind=action,stage=stage,target_record_refs=list(targets),action_payload=_payload(list(targets)))]}
    routing = dict(scenario_id=request["scenario_id"],selected_template_ids=[template],action_bindings=[dict(proposal_action_id="review",template_id=template,source_ids=["policy",*targets] if bind_targets else ["policy"])],unresolved_manager_inputs=[])
    expanded = compile_task_compiler_plan(compiler_output=routing,manager_request=request,catalog=CATALOG,binding_values=_bindings_for_request(request))
    sources = {target:SourceRecord(source_id=target,kind="record",content="",record_model="derived.erp_amount_facts",record_revision="r1",structured_fields=fields) for target in targets}
    return lower_erp_stage_to_proof_plan(expanded,source_records=sources)


@pytest.mark.parametrize("stage,mode,expected", [
    ("regular","fixed_amount",{"invoice_regular_amount_matches_order"}),
    ("downpayment","fixed_amount",{"invoice_fixed_downpayment_matches_policy"}),
    ("downpayment","percentage",{"invoice_percentage_downpayment_matches_policy"}),
    ("downpayment","unknown",{"invoice_fixed_downpayment_matches_policy","invoice_percentage_downpayment_matches_policy"}),
])
def test_invoice_mode_selects_only_registered_branch_and_unknown_keeps_gaps(stage,mode,expected):
    plan=plan_for("customer_invoice_post.v1","account.move.action_post",stage,{"mode":mode})
    actual={node.action_contract.local_check_id for node in plan.nodes if node.kind=="CHECK" and node.action_contract.numeric_decision}
    assert actual == expected | {"invoice_tax_amount_matches_odoo","invoice_total_matches_untaxed_and_tax"}


@pytest.mark.parametrize("action,stage,kind,polarity",[("sale.order.action_confirm","confirm","ALL","SUPPORTED"),("sale.order.action_cancel","cancel","ANY","CONTRADICTED")])
def test_screening_composition_is_per_target_and_disabled_checks_are_not_invented(action,stage,kind,polarity):
    plan=plan_for("sales_order_disposition.v1",action,stage,{"minimum_lead_days_enabled":False},targets=("first","second"))
    numeric=[node for node in plan.nodes if node.kind=="CHECK" and node.action_contract.numeric_decision]
    groups=[node for node in plan.nodes if node.id.startswith("eligibility:")]
    assert len(numeric)==6 and len(groups)==2
    assert all(len(node.action_contract.target_record_refs)==1 for node in numeric)
    assert all(node.action_contract.numeric_decision.true_status==polarity for node in numeric)
    assert all(node.kind==kind and len(node.depends_on)==3 for node in groups)
    root=next(node for node in plan.nodes if node.id==plan.roots["erp_action_plan_valid"])
    assert all(node.id not in root.depends_on for node in numeric)
    assert all(node.id in root.depends_on for node in groups)


def test_empty_eligibility_does_not_allow_cancellation():
    with pytest.raises(ValueError,match="active eligibility"):
        plan_for("sales_order_disposition.v1","sale.order.action_cancel","cancel",dict(budget_enabled=False,quantity_range_enabled=False,minimum_lead_days_enabled=False))


def test_unbound_source_cannot_remove_numeric_obligations():
    plan = plan_for("sales_order_disposition.v1", "sale.order.action_cancel", "cancel",
        dict(budget_enabled=False, quantity_range_enabled=False, minimum_lead_days_enabled=False), bind_targets=False)
    assert sum(bool(node.action_contract.numeric_decision) for node in plan.nodes if node.kind == "CHECK") == 4
