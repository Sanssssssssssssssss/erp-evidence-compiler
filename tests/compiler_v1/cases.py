from __future__ import annotations

import hashlib

from app.compiler_runtime.models import ActionProposal, ProposedAction
from app.compiler_runtime.runtime import EvidenceCompilerRuntime, PreparedSource
from app.config import Settings
from app.llm import LlmClient

from erp_agent_odoo.capabilities.sales_order_acceptance import (
    ODOO_READ_SCHEMA,
    FactProvenance,
    OrderAcceptanceBatch,
    OrderAcceptanceFacts,
    OrderAcceptancePolicy,
    OrderAcceptanceView,
    admit_order_acceptance_reads,
    compile_order_acceptance,
    compile_order_acceptance_plan,
    order_acceptance_source,
)
from erp_agent_odoo.capabilities.vendor_bill_posting import (
    VendorBillPostingFacts,
    VendorBillPostingPolicy,
    VendorBillPostingView,
    compile_vendor_bill_posting,
    compile_vendor_bill_posting_plan,
    vendor_bill_posting_source,
)


def provenance(
    source_ref: str,
    field_path: str,
    *,
    revision: str = "seed-r1",
) -> FactProvenance:
    return FactProvenance(
        source_ref=source_ref,
        field_path=field_path,
        revision=revision,
        fingerprint=hashlib.sha256(
            f"{source_ref}:{field_path}:{revision}".encode()
        ).hexdigest(),
        source_kind="odoo_record" if source_ref.startswith("odoo:") else "policy",
    )


def order_view(*, quantity: str = "25", provenance_revision: str = "seed-r1") -> OrderAcceptanceView:
    return OrderAcceptanceView(
        record_ref="sale.order:o04",
        facts=OrderAcceptanceFacts(
            requested_quantity=quantity,
            unit_list_price="2095.12",
            pretax_budget="55956.51",
            lead_days="12",
            fact_provenance={
                "requested_quantity": (
                    provenance(
                        "odoo:sale.order.line:41",
                        "/product_uom_qty",
                        revision=provenance_revision,
                    ),
                ),
                "unit_list_price": (
                    provenance(
                        "odoo:product.product:7",
                        "/list_price",
                        revision=provenance_revision,
                    ),
                ),
                "pretax_budget": (
                    provenance(
                        "policy:erp-bench:2032",
                        "/orders/o04/pretax_budget",
                        revision=provenance_revision,
                    ),
                ),
                "lead_days": (
                    provenance(
                        "policy:erp-bench:2032",
                        "/orders/o04/minimum_lead_days",
                        revision=provenance_revision,
                    ),
                ),
            },
        ),
    )


def registered_run_case():
    view = order_view()
    proposal = ActionProposal(
        proposal_id="proposal:registered-plan",
        actions=[
            ProposedAction(
                record_ref=view.record_ref,
                action="confirm_sales_order",
            )
        ],
        target_record_refs=[view.record_ref],
        expected_preconditions={
            view.record_ref: {"upstream_revision": view.revision}
        },
    )
    policy = OrderAcceptancePolicy(
        policy_id="erp-bench:2032",
        minimum_quantity="17",
        maximum_quantity="25",
        minimum_lead_days="10",
    )
    from app.compiler_runtime.requirement_pack import SALES_ORDER_ACCEPTANCE_PACK

    plan = compile_order_acceptance_plan(
        compile_order_acceptance(
            proposal,
            OrderAcceptanceBatch(records=(view,)),
            policy,
        )
    )
    source = order_acceptance_source(view)
    prepared = PreparedSource(
        record=source,
        metadata={
            "source_fingerprint": hashlib.sha256(source.content.encode()).hexdigest(),
            "target_record_ref": view.record_ref,
            "upstream_revision": view.revision,
        },
    )
    runtime = EvidenceCompilerRuntime(
        LlmClient(Settings()),
        requirement_pack=SALES_ORDER_ACCEPTANCE_PACK,
    )
    return view, proposal, policy, SALES_ORDER_ACCEPTANCE_PACK, plan, prepared, runtime


def sales_order_batch_run_case():
    views = (
        order_view().model_copy(update={"record_ref": "sale.order:o04"}),
        order_view(quantity="15").model_copy(update={"record_ref": "sale.order:o02"}),
    )
    policy = OrderAcceptancePolicy(
        policy_id="erp-bench:2032",
        minimum_quantity="17",
        maximum_quantity="25",
        minimum_lead_days="10",
    )
    proposal = ActionProposal(
        proposal_id="proposal:registered-batch",
        actions=[
            ProposedAction(
                record_ref=view.record_ref,
                action=(
                    "confirm_sales_order"
                    if view.facts.requested_quantity >= 17
                    else "cancel_sales_order"
                ),
            )
            for view in views
        ],
        target_record_refs=[view.record_ref for view in views],
        expected_preconditions={
            view.record_ref: {"upstream_revision": view.revision} for view in views
        },
    )
    from app.compiler_runtime.requirement_pack import SALES_ORDER_ACCEPTANCE_PACK

    plan = compile_order_acceptance_plan(
        compile_order_acceptance(
            proposal,
            OrderAcceptanceBatch(records=views),
            policy,
        )
    )
    prepared = []
    for view in views:
        source = order_acceptance_source(view)
        prepared.append(
            PreparedSource(
                record=source,
                metadata={
                    "source_fingerprint": hashlib.sha256(source.content.encode()).hexdigest(),
                    "target_record_ref": view.record_ref,
                    "upstream_revision": view.revision,
                },
            )
        )
    runtime = EvidenceCompilerRuntime(
        LlmClient(Settings()),
        requirement_pack=SALES_ORDER_ACCEPTANCE_PACK,
    )
    return views, proposal, policy, SALES_ORDER_ACCEPTANCE_PACK, plan, prepared, runtime


def admitted_read_set() -> dict[str, object]:
    return {
        "schemas": {
            model: {
                field: {"type": field_type, "relation": relation or False}
                for field, (field_type, relation) in fields.items()
            }
            for model, fields in ODOO_READ_SCHEMA.items()
        },
        "order": {
            "id": 104,
            "client_order_ref": "rEA51ED26F3_o04",
            "date_order": "2026-08-30 10:00:00",
            "commitment_date": "2026-09-11 10:00:00",
            "state": "draft",
            "order_line": [410],
            "write_date": "2026-08-30 10:01:00",
        },
        "lines": [
            {
                "id": 410,
                "order_id": [104, "S00104"],
                "product_id": [7, "Heavy-Duty Specimen Prep Bench"],
                "product_uom_qty": 25.0,
                "write_date": "2026-08-30 10:01:00",
            }
        ],
        "products": [
            {
                "id": 7,
                "default_code": "PEA51ED26F3-LAB-HSP-009",
                "list_price": 2095.12,
                "write_date": "2026-08-30 09:00:00",
            }
        ],
        "pretax_budget": "55956.51",
        "budget_provenance": FactProvenance(
            source_ref="document:erp-bench:2032:instruction",
            field_path="/orders/o04/pretax_budget",
            revision="task-2032-r1",
            fingerprint=hashlib.sha256(b"task-2032-instruction").hexdigest(),
            source_kind="document",
        ),
    }


def registered_admitted_run_case():
    view = admit_order_acceptance_reads(**admitted_read_set())
    proposal = ActionProposal(
        proposal_id="proposal:admitted-plan",
        actions=[
            ProposedAction(
                record_ref=view.record_ref,
                action="confirm_sales_order",
            )
        ],
        target_record_refs=[view.record_ref],
        expected_preconditions={
            view.record_ref: {"upstream_revision": view.revision}
        },
    )
    policy = OrderAcceptancePolicy(
        policy_id="erp-bench:2032",
        minimum_quantity="17",
        maximum_quantity="25",
        minimum_lead_days="10",
    )
    from app.compiler_runtime.requirement_pack import SALES_ORDER_ACCEPTANCE_PACK

    plan = compile_order_acceptance_plan(
        compile_order_acceptance(
            proposal,
            OrderAcceptanceBatch(records=(view,)),
            policy,
        )
    )
    source = order_acceptance_source(view)
    prepared = PreparedSource(
        record=source,
        metadata={
            "source_fingerprint": hashlib.sha256(source.content.encode()).hexdigest(),
            "target_record_ref": view.record_ref,
            "upstream_revision": view.revision,
        },
    )
    return view, proposal, policy, SALES_ORDER_ACCEPTANCE_PACK, plan, prepared


def _bill_provenance(
    source_ref: str,
    field_path: str,
    source_kind: str,
) -> FactProvenance:
    revision = "vendor-bill-fixture-r1"
    return FactProvenance(
        source_ref=source_ref,
        field_path=field_path,
        revision=revision,
        fingerprint=hashlib.sha256(
            f"{source_ref}:{field_path}:{revision}".encode()
        ).hexdigest(),
        source_kind=source_kind,
    )


def vendor_bill_posting_run_case(*, received_quantity: str = "10"):
    bill = "odoo:account.move:31"
    purchase = "odoo:purchase.order:17"
    line = "odoo:purchase.order.line:23"
    document = "document:vendor-bill:INV-9001"
    view = VendorBillPostingView(
        record_ref=bill,
        facts=VendorBillPostingFacts(
            ordered_quantity="10",
            received_quantity=received_quantity,
            purchase_untaxed_total="2500",
            bill_untaxed_total="2500",
            document_untaxed_total="2500",
            vendor_identity_match="1",
            document_reference_match="1",
            fact_provenance={
                "ordered_quantity": (
                    _bill_provenance(line, "/product_qty", "odoo_record"),
                ),
                "received_quantity": (
                    _bill_provenance(line, "/qty_received", "odoo_record"),
                ),
                "purchase_untaxed_total": (
                    _bill_provenance(purchase, "/amount_untaxed", "odoo_record"),
                ),
                "bill_untaxed_total": (
                    _bill_provenance(bill, "/amount_untaxed", "odoo_record"),
                ),
                "document_untaxed_total": (
                    _bill_provenance(document, "/amount_untaxed", "document"),
                ),
                "vendor_identity_match": (
                    _bill_provenance(purchase, "/partner_id", "odoo_record"),
                    _bill_provenance(bill, "/partner_id", "odoo_record"),
                ),
                "document_reference_match": (
                    _bill_provenance(bill, "/ref", "odoo_record"),
                    _bill_provenance(document, "/purchase_order_ref", "document"),
                ),
            },
        ),
    )
    proposal = ActionProposal(
        proposal_id="proposal:vendor-bill-posting",
        actions=[ProposedAction(record_ref=bill, action="post_vendor_bill")],
        target_record_refs=[bill],
        expected_preconditions={bill: {"upstream_revision": view.revision}},
    )
    policy = VendorBillPostingPolicy(policy_id="vendor-bill-three-way-match")
    from app.compiler_runtime.requirement_pack import VENDOR_BILL_POSTING_PACK

    plan = compile_vendor_bill_posting_plan(
        compile_vendor_bill_posting(proposal, view, policy)
    )
    source = vendor_bill_posting_source(view)
    prepared = PreparedSource(
        record=source,
        metadata={
            "source_fingerprint": hashlib.sha256(source.content.encode()).hexdigest(),
            "target_record_ref": view.record_ref,
            "upstream_revision": view.revision,
        },
    )
    runtime = EvidenceCompilerRuntime(
        LlmClient(Settings()),
        requirement_pack=VENDOR_BILL_POSTING_PACK,
    )
    return (
        view,
        proposal,
        policy,
        VENDOR_BILL_POSTING_PACK,
        plan,
        prepared,
        runtime,
    )
