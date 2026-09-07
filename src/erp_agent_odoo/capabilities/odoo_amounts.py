"""Read-only admission of native Odoo records into the fixed ERP amount view."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord

from .provenance import FactProvenance
from .sales_order_acceptance import _relation_id


def native_record_source(model: str, row: Mapping[str, Any], *, instance: str) -> SourceRecord:
    """Wrap an actual read result; never label a proposal or a generated row native."""
    record_id = _relation_id(row.get("id"), field=f"{model}.id")
    revision = row.get("write_date")
    if record_id <= 0 or not instance.strip() or not isinstance(revision, str) or not revision:
        raise ValueError("Native admission requires id, instance and write_date")
    datetime.fromisoformat(revision)
    source = SourceRecord(source_id=f"odoo:{model}:{record_id}", kind="record", content="",
        record_model=model, record_revision=revision, structured_fields=row)
    return replace(source, provenance={"role": "evidence", "odoo_instance": instance,
        "source_records": {source.source_id: _lineage(source)}})


def _lineage(source: SourceRecord) -> dict[str, str]:
    return {"revision": source.record_revision, "fingerprint": hashlib.sha256(source.content.encode()).hexdigest()}


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected an observed decimal number")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Amount admission requires finite nonnegative numbers")
    return result


def erp_amount_source(
    target_ref: str,
    *,
    native_sources: Sequence[SourceRecord],
    policy: SourceRecord,
) -> SourceRecord:
    """Project fixed fields, preserving every contributing observation's provenance.

    Policy is a structured admitted instruction with a scoped target list. Missing
    policy numbers stay absent; unsupported native shapes fail admission explicitly.
    """
    if policy.provenance.get("role") != "instruction" or policy.record_fields is None:
        raise ValueError("Amount admission requires a structured instruction source")
    rules = policy.record_fields
    if target_ref not in rules.get("target_refs", []):
        raise ValueError("Amount target is outside the instruction scope")
    sources = {source.source_id: source for source in native_sources}
    if len(sources) != len(native_sources):
        raise ValueError("Native source ids must be unique")
    instances = {source.provenance.get("odoo_instance") for source in native_sources}
    if len(instances) != 1 or not next(iter(instances), None):
        raise ValueError("Native reads must come from one identified Odoo instance")
    for source in native_sources:
        if source.provenance.get("source_records", {}).get(source.source_id) != _lineage(source):
            raise ValueError("Native source lineage changed")
    target = sources[f"odoo:{target_ref}"]
    model = target.record_model
    if target_ref != f"{model}:{target.record_fields['id']}":
        raise ValueError("Native target identity changed")
    values: dict[str, Any] = {}
    provenance: dict[str, list[dict[str, Any]]] = {}
    used = {target.source_id: target, policy.source_id: policy}

    def fact(name: str, source: SourceRecord, pointer: str, *, numeric: bool = True) -> Any:
        value = EvidenceSandbox._resolve_json_pointer(source.record_fields, pointer)
        values[name] = str(_number(value)) if numeric else value
        used[source.source_id] = source
        provenance[name] = [FactProvenance(source_ref=source.source_id, field_path=pointer,
            source_kind="policy" if source is policy else "odoo_record", **_lineage(source)).model_dump(mode="json")]
        return values[name]

    def related(model: str, value: Any) -> SourceRecord:
        return sources[f"odoo:{model}:{_relation_id(value, field=model)}"]

    def single_line(order: SourceRecord, line_model: str) -> SourceRecord:
        # ponytail: one commercial line; add explicit multi-line programs before admitting mixed prices/units.
        ids = order.record_fields["order_line"]
        if len(ids) != 1:
            raise ValueError("Amount admission currently supports one commercial order line")
        line = related(line_model, ids[0])
        if _relation_id(line.record_fields["order_id"], field="order_id") != order.record_fields["id"]:
            raise ValueError("Admitted line belongs to another order")
        if _number(line.record_fields["discount"]) != 0:
            raise ValueError("Discounted order lines require a different registered amount program")
        return line

    currency = related("res.currency", target.record_fields["currency_id"])
    fact("currency", currency, "/name", numeric=False)
    fact("write_date", target, "/write_date", numeric=False)
    for name in ("budget", "min_quantity", "max_quantity", "min_lead_days", "vendor_cap",
                 "fixed_amount", "downpayment_ratio"):
        if name in rules:
            fact(name, policy, f"/{name}")
    for name in ("budget_enabled", "quantity_range_enabled", "minimum_lead_days_enabled", "vendor_cap_enabled"):
        if name in rules:
            if not isinstance(rules[name], bool):
                raise ValueError(f"{name} must be an explicit boolean")
            fact(name, policy, f"/{name}", numeric=False)
    if "mode" in rules:
        fact("mode", policy, "/mode", numeric=False)

    if model in {"sale.order", "purchase.order"}:
        line = single_line(target, f"{model}.line")
        product = related("product.product", line.record_fields["product_id"])
        unit = _relation_id(line.record_fields["product_uom_id"], field="product_uom_id")
        if unit != _relation_id(product.record_fields["uom_id"], field="uom_id"):
            raise ValueError("Unit conversion is outside the registered amount programs")
        fact("unit", line, "/product_uom_id", numeric=False)
        fact("quantity", line, "/product_uom_qty" if model == "sale.order" else "/product_qty")
        fact("price_unit" if model == "sale.order" else "unit_cost", line, "/price_unit")
        if model == "sale.order":
            if _relation_id(product.record_fields["currency_id"], field="currency_id") != currency.record_fields["id"]:
                raise ValueError("List-price currency conversion is outside this adapter")
            fact("list_price", product, "/list_price")
            if target.record_fields.get("commitment_date") and target.record_fields.get("date_order"):
                start = datetime.fromisoformat(fact("lead_days", target, "/date_order", numeric=False))
                finish = datetime.fromisoformat(fact("lead_end", target, "/commitment_date", numeric=False))
                values["lead_days"] = str(_number((finish - start).total_seconds()) / Decimal(86400))
                provenance["lead_days"] += provenance.pop("lead_end")
                values.pop("lead_end")
        else:
            offer = sources[rules["supplierinfo_ref"]]
            if offer.record_model != "product.supplierinfo":
                raise ValueError("The admitted tier must identify product.supplierinfo")
            checks = ((offer.record_fields["partner_id"], target.record_fields["partner_id"]),
                (offer.record_fields["currency_id"], target.record_fields["currency_id"]),
                (offer.record_fields["product_tmpl_id"], product.record_fields["product_tmpl_id"]))
            if any(_relation_id(a, field="offer") != _relation_id(b, field="order") for a, b in checks):
                raise ValueError("Supplier tier vendor, currency or product does not match")
            if offer.record_fields.get("product_id") and _relation_id(offer.record_fields["product_id"], field="product_id") != product.record_fields["id"]:
                raise ValueError("Supplier tier belongs to another product variant")
            if _number(offer.record_fields["discount"]) != 0:
                raise ValueError("Discounted supplier tiers are outside this adapter")
            if "min_quantity" in rules and _number(rules["min_quantity"]) != _number(offer.record_fields["min_qty"]):
                raise ValueError("Instruction minimum differs from the selected native tier")
            fact("tier_price", offer, "/price")
            fact("min_quantity", offer, "/min_qty")
            refs = rules.get("horizon_order_refs", [])
            if not refs or len(refs) != len(set(refs)) or target_ref not in refs:
                raise ValueError("Purchase horizon requires an explicit unique closed order set")
            quantities = []
            origins = []
            for ref in refs:
                order = sources[f"odoo:{ref}"]
                if order.record_model != model or any(_relation_id(order.record_fields[key], field=key) != _relation_id(target.record_fields[key], field=key) for key in ("partner_id", "currency_id")):
                    raise ValueError("Purchase horizon mixes vendors or currencies")
                part = single_line(order, "purchase.order.line")
                if _relation_id(part.record_fields["product_id"], field="product_id") != product.record_fields["id"] or _relation_id(part.record_fields["product_uom_id"], field="unit") != unit:
                    raise ValueError("Purchase horizon mixes products or units")
                quantities.append(_number(fact("horizon_quantity", part, "/product_qty")))
                origins += provenance["horizon_quantity"]
                used[order.source_id] = order
            fact("horizon_scope", policy, "/horizon_order_refs", numeric=False)
            values["horizon_quantity"] = str(sum(quantities, Decimal(0)))
            provenance["horizon_quantity"] = origins + provenance.pop("horizon_scope")
            values.pop("horizon_scope")
    elif model == "account.move":
        if target.record_fields["move_type"] != "out_invoice":
            raise ValueError("Only customer invoices use this amount program")
        order_ids = set()
        for line_id in target.record_fields["invoice_line_ids"]:
            line = related("account.move.line", line_id)
            if line.record_fields.get("display_type") in {"line_section", "line_note"}:
                continue
            sale_ids = line.record_fields["sale_line_ids"]
            if not sale_ids:
                raise ValueError("Invoice line has no admitted sales-order lineage")
            for sale_id in sale_ids:
                sale_line = related("sale.order.line", sale_id)
                order_ids.add(_relation_id(sale_line.record_fields["order_id"], field="order_id"))
                used[sale_line.source_id] = sale_line
            used[line.source_id] = line
        if len(order_ids) != 1:
            raise ValueError("Invoice amount program requires exactly one source sales order")
        order = related("sale.order", next(iter(order_ids)))
        if _relation_id(order.record_fields["currency_id"], field="currency_id") != currency.record_fields["id"]:
            raise ValueError("Invoice and sales-order currencies differ")
        for name in ("amount_untaxed", "amount_tax", "amount_total"):
            fact(name, target, f"/{name}")
        fact("order_untaxed_total", order, "/amount_untaxed")
        fact("odoo_amount_tax", target, "/tax_totals/tax_amount_currency")
        fact("currency_tolerance", currency, "/rounding")
        if "authorized_downpayment_refs" in rules:
            refs = rules["authorized_downpayment_refs"]
            if not isinstance(refs, list) or len(refs) != len(set(refs)):
                raise ValueError("Authorized downpayment refs must be unique")
            amounts, origins = [], []
            for ref in refs:
                move = sources[f"odoo:{ref}"]
                if move.record_model != model or move.record_fields["state"] != "posted" or move.record_fields["move_type"] != "out_invoice" or move.record_fields["id"] not in order.record_fields["invoice_ids"] or _relation_id(move.record_fields["currency_id"], field="currency_id") != currency.record_fields["id"]:
                    raise ValueError("Authorized downpayment is not a posted invoice for this order/currency")
                commercial = [related("account.move.line", line_id) for line_id in move.record_fields["invoice_line_ids"]]
                commercial = [line for line in commercial if line.record_fields.get("display_type") not in {"line_section", "line_note"}]
                if not commercial or any(line.record_fields["is_downpayment"] is not True for line in commercial):
                    raise ValueError("Authorized downpayment must contain only native downpayment lines")
                used.update({line.source_id: line for line in commercial})
                amounts.append(_number(fact("authorized_downpayment_amount", move, "/amount_untaxed")))
                origins += provenance["authorized_downpayment_amount"]
            fact("downpayment_scope", policy, "/authorized_downpayment_refs", numeric=False)
            values["authorized_downpayment_amount"] = str(sum(amounts, Decimal(0)))
            provenance["authorized_downpayment_amount"] = origins + provenance.pop("downpayment_scope")
            values.pop("downpayment_scope")
    else:
        raise ValueError(f"No amount view is registered for {model}")
    return SourceRecord(source_id=target_ref, kind="record", content="", record_model="derived.erp_amount_facts",
        record_revision=target.record_revision, structured_fields=values,
        provenance={"role": "evidence", "odoo_instance": next(iter(instances)),
            "source_records": {key: _lineage(source) for key, source in sorted(used.items())},
            "fact_provenance": provenance})
