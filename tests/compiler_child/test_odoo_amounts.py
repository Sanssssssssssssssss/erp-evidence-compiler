"""Native field admission, not simulated model correctness."""
from dataclasses import replace

import pytest

from app.compiler_runtime.sandbox import SourceRecord
from erp_agent_odoo.capabilities.odoo_amounts import erp_amount_source, native_record_source


def inputs(model="sale.order"):
    def row(kind, id, **values):
        return native_record_source(kind, {"id": id, "write_date": "2026-09-05 12:00:00", **values}, instance="isolated-odoo")
    currency=row("res.currency",1,name="USD",rounding=0.01)
    product=row("product.product",1,list_price=12.5,currency_id=[1,"USD"],uom_id=[1,"Units"],product_tmpl_id=[1,"P"])
    sale=row("sale.order",1,order_line=[1],currency_id=[1,"USD"],date_order="2026-09-05 12:00:00",commitment_date="2026-09-08 12:00:00",amount_untaxed=125.0,invoice_ids=[2])
    sale_line=row("sale.order.line",1,order_id=[1,"SO"],product_id=[1,"P"],product_uom_id=[1,"Units"],product_uom_qty=10.0,price_unit=12.5,discount=0)
    sources=[currency,product,sale,sale_line]
    rules=dict(target_refs=[f"{model}:1"],budget="150",min_quantity="2",max_quantity="20",quantity_range_enabled=True)
    if model=="purchase.order":
        sources += [row(model,1,order_line=[1],currency_id=[1,"USD"],partner_id=[3,"Vendor"]),
            row(model+".line",1,order_id=[1,"PO"],product_id=[1,"P"],product_uom_id=[1,"Units"],product_qty=10.0,price_unit=8.0,discount=0),
            row("product.supplierinfo",1,partner_id=[3,"Vendor"],currency_id=[1,"USD"],product_tmpl_id=[1,"P"],product_id=False,min_qty=2,price=8.0,discount=0)]
        rules.update(supplierinfo_ref="odoo:product.supplierinfo:1",horizon_order_refs=["purchase.order:1"])
    if model=="account.move":
        sources += [row(model,1,currency_id=[1,"USD"],move_type="out_invoice",invoice_line_ids=[1],amount_untaxed=125.0,amount_tax=12.5,amount_total=137.5,tax_totals={"tax_amount_currency":12.5}),
            row(model,2,state="posted",move_type="out_invoice",invoice_line_ids=[2],currency_id=[1,"USD"],amount_untaxed=25.0),
            row("account.move.line",1,sale_line_ids=[1]),row("account.move.line",2,is_downpayment=True)]
        rules.update(authorized_downpayment_refs=["account.move:2"])
    policy=SourceRecord(source_id="policy",kind="record",content="",record_model="policy.erp_review",record_revision="p1",structured_fields=rules,provenance={"role":"instruction"})
    return sources,policy


@pytest.mark.parametrize("model,expected",[("sale.order",{"quantity":"10.0","price_unit":"12.5","list_price":"12.5","lead_days":"3.0"}),
    ("purchase.order",{"horizon_quantity":"10.0","tier_price":"8.0","min_quantity":"2"}),
    ("account.move",{"amount_tax":"12.5","odoo_amount_tax":"12.5","authorized_downpayment_amount":"25.0"})])
def test_native_projection_has_original_fields_and_provenance(model,expected):
    sources,policy=inputs(model)
    view=erp_amount_source(f"{model}:1",native_sources=sources,policy=policy)
    assert expected.items() <= view.record_fields.items()
    assert set(view.record_fields)==set(view.provenance["fact_provenance"])
    if model=="account.move":
        assert view.provenance["fact_provenance"]["odoo_amount_tax"][0]["field_path"]=="/tax_totals/tax_amount_currency"


@pytest.mark.parametrize("defect",["stale","foreign_instance","wrong_line","wrong_unit","wrong_currency","discount","missing_tax","wrong_downpayment","regular_as_downpayment","partial_horizon"])
def test_unsupported_or_changed_native_reads_do_not_become_amount_facts(defect):
    model="account.move" if defect in {"missing_tax","wrong_downpayment","regular_as_downpayment"} else "purchase.order" if defect=="partial_horizon" else "sale.order"
    sources,policy=inputs(model)
    key="odoo:sale.order.line:1"
    changes={"wrong_line":{"order_id":[2,"Other"]},"wrong_unit":{"product_uom_id":[2,"Boxes"]},"discount":{"discount":5}}
    if defect in {"stale","foreign_instance"}:
        sources[0]=replace(sources[0],provenance={**sources[0].provenance,**({"source_records":{}} if defect=="stale" else {"odoo_instance":"other"})})
    else:
        if defect=="wrong_currency":key="odoo:product.product:1";changes[defect]={"currency_id":[2,"EUR"]}
        if defect=="missing_tax":key="odoo:account.move:1";changes[defect]={"tax_totals":{}}
        if defect=="wrong_downpayment":key="odoo:account.move:2";changes[defect]={"state":"draft"}
        if defect=="regular_as_downpayment":key="odoo:account.move.line:2";changes[defect]={"is_downpayment":False}
        if defect=="partial_horizon":key="odoo:purchase.order:1";changes[defect]={"order_line":[1,99]}
        index=next(i for i,s in enumerate(sources) if s.source_id==key)
        source=sources[index]
        sources[index]=native_record_source(source.record_model,{**source.record_fields,**changes[defect]},instance="isolated-odoo")
    with pytest.raises((ValueError,KeyError)):
        erp_amount_source(f"{model}:1",native_sources=sources,policy=policy)


def test_missing_policy_amount_stays_missing_instead_of_defaulting():
    sources,policy=inputs()
    fields=dict(policy.record_fields);fields.pop("budget")
    policy=replace(policy,content="",structured_fields=fields)
    view=erp_amount_source("sale.order:1",native_sources=sources,policy=policy)
    assert "budget" not in view.record_fields
