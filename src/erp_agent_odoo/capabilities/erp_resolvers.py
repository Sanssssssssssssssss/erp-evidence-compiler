from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from app.compiler_runtime.models import ERPReviewContract
from app.compiler_runtime.sandbox import SourceRecord

from .purchase_order_cancellation import FACT_VIEW_MODEL as PO_CANCELLATION_VIEW_MODEL


class ERPResolverContractError(ValueError):
    pass


def resolve_erp_check(
    contract: ERPReviewContract,
    sources: Mapping[str, SourceRecord],
) -> tuple[dict[str, Any], bool, list[str]]:
    """Select frozen inputs once, then run the registered pure resolver."""

    inputs = select_erp_inputs(contract, sources)
    result, diagnostics = replay_erp_check(
        resolver_id=contract.resolver_program.resolver_id,
        resolver_version=contract.resolver_program.resolver_version,
        local_check_id=contract.local_check_id,
        resolved_inputs=inputs,
    )
    return inputs, result, diagnostics


def select_erp_inputs(
    contract: ERPReviewContract,
    sources: Mapping[str, SourceRecord],
) -> dict[str, Any]:
    """Validate and freeze the source inputs required by one registered CHECK."""

    if set(sources) != set(contract.source_refs):
        raise ValueError("ERP resolver source closure differs from its contract")
    _validate_resolver_identity(
        resolver_id=contract.resolver_program.resolver_id,
        resolver_version=contract.resolver_program.resolver_version,
        local_check_id=contract.local_check_id,
    )
    selector = _INPUT_SELECTORS.get(contract.template_id)
    if selector is None:
        raise ERPResolverContractError(
            f"no registered input selector for ERP template {contract.template_id!r}"
        )
    return selector(contract, sources)


def replay_erp_check(
    *,
    resolver_id: str,
    resolver_version: str,
    local_check_id: str,
    resolved_inputs: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """Replay one versioned ERP resolver without I/O or side effects."""

    _validate_resolver_identity(
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        local_check_id=local_check_id,
    )

    rows = list(resolved_inputs.get("target_records") or [])
    broken_refs = set(resolved_inputs.get("broken_record_refs") or [])
    target_refs = {str(row.get("record_ref") or "") for row in rows}
    diagnostics: list[str] = []

    if local_check_id == "cancellation_scope_is_exact":
        missing = sorted(broken_refs - target_refs)
        extra = sorted(target_refs - broken_refs)
        if missing:
            diagnostics.append(f"missing broken commitments: {missing}")
        if extra:
            diagnostics.append(f"unaffected commitments selected: {extra}")
        return not missing and not extra and bool(broken_refs), diagnostics

    if not rows:
        return False, ["no cancellation target records were sealed"]

    for row in rows:
        record_ref = str(row.get("record_ref") or "")
        model = str(row.get("model") or "")
        proposal = dict(row.get("proposal_values") or {})
        observed = dict(row.get("observed_values") or {})
        if record_ref not in broken_refs:
            diagnostics.append(f"{record_ref}: target is not a registered broken commitment")
            continue
        if not observed:
            diagnostics.append(f"{record_ref}: target is absent from the frozen scenario")
            continue
        if local_check_id == "target_is_exact_disrupted_commitment":
            comparison_fields = (
                (
                    ("vendor_ref", "vendor_ref"),
                    ("product_code", "product_code"),
                    ("quantity", "quantity"),
                    ("unit_cost", "price_unit"),
                    ("planned_arrival_days", "planned_arrival_days"),
                    ("origin_refs", "origin_customer_refs"),
                )
                if model == "purchase.order"
                else (
                    ("product_code", "product_code"),
                    ("bom_ref", "bom_ref"),
                    ("quantity", "quantity"),
                    ("workcenter_code", "workcenter_code"),
                    ("planned_start_days", "planned_start_days"),
                    ("planned_due_days", "planned_due_days"),
                    ("origin_refs", "origin_customer_refs"),
                )
            )
            for proposal_key, observed_key in comparison_fields:
                if proposal.get(proposal_key) != observed.get(observed_key):
                    diagnostics.append(
                        f"{record_ref}: {proposal_key} differs from frozen commitment"
                    )
            if model == "purchase.order":
                broken_vendor = str(resolved_inputs.get("broken_vendor_ref") or "")
                if proposal.get("vendor_ref") != broken_vendor:
                    diagnostics.append(f"{record_ref}: vendor is not the disrupted supplier")
            else:
                broken_workcenter = str(
                    resolved_inputs.get("broken_workcenter_code") or ""
                )
                if proposal.get("workcenter_code") != broken_workcenter:
                    diagnostics.append(
                        f"{record_ref}: workcenter is not the disrupted workcenter"
                    )
        else:
            state = str(observed.get("state") or "").lower()
            if model != "purchase.order" and state not in {
                "draft",
                "confirmed",
                "progress",
                "to_close",
            }:
                diagnostics.append(f"{record_ref}: state {state!r} is not cancellable")
            if model == "purchase.order":
                if observed["locked"]:
                    diagnostics.append(f"{record_ref}: purchase order is locked")
                blocking_bills = list(observed["blocking_vendor_bill_refs"])
                if blocking_bills:
                    diagnostics.append(
                        f"{record_ref}: active vendor bills block cancellation: "
                        f"{sorted(str(item) for item in blocking_bills)}"
                    )
            if proposal.get("state") != observed.get("state"):
                diagnostics.append(f"{record_ref}: proposal state is stale")
            observed_revision = str(row.get("observed_revision") or "")
            if observed_revision and row.get("record_revision") != observed_revision:
                diagnostics.append(f"{record_ref}: proposal revision is stale")
    return not diagnostics, diagnostics


def _scenario_data(sources: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for source in sources:
        if source.record_model == "erpbench.scenario_data" and source.record_fields is not None:
            candidates.append(dict(source.record_fields))
            continue
        if source.kind != "record":
            continue
        try:
            value = json.loads(source.content)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping) and "repair_context" in value:
            candidates.append(dict(value))
    if len(candidates) != 1:
        raise ValueError(
            "cancellation resolver requires exactly one admitted scenario_data record; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _validate_resolver_identity(
    *,
    resolver_id: str,
    resolver_version: str,
    local_check_id: str,
) -> None:
    if resolver_version != "1":
        raise ERPResolverContractError(
            f"unsupported ERP resolver version: {resolver_version!r}"
        )
    expected = _RESOLVER_BY_CHECK.get(local_check_id)
    if expected is None or resolver_id != expected:
        raise ERPResolverContractError(
            f"unimplemented ERP resolver contract: {local_check_id!r}/{resolver_id!r}"
        )


def _rows_by_ref(scenario: Mapping[str, Any], field: str) -> dict[str, dict[str, Any]]:
    raw_rows = scenario.get(field)
    if not isinstance(raw_rows, list):
        raise ValueError(f"scenario_data.{field} must be a list")
    rows: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"scenario_data.{field}[{index}] must be an object")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"scenario_data.{field}[{index}] requires a non-empty ref")
        if ref in rows:
            raise ValueError(f"scenario_data.{field} contains duplicate ref {ref!r}")
        rows[ref] = dict(item)
    return rows


def _require_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(
        field for field in fields if field not in value or value[field] is None
    )
    if missing:
        raise ValueError(f"{label}: missing admitted fields: {missing}")


def _ref_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty record refs")
    return value


def _validate_field_types(
    value: Mapping[str, Any],
    label: str,
    *,
    strings: set[str] = frozenset(),
    numbers: set[str] = frozenset(),
    ref_lists: set[str] = frozenset(),
) -> None:
    for field in strings:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"{label}.{field} must be a non-empty string")
    for field in numbers:
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{label}.{field} must be a finite number")
    for field in ref_lists:
        _ref_list(value[field], f"{label}.{field}")


def _cancellation_inputs(
    contract: ERPReviewContract,
    sources: Mapping[str, SourceRecord],
) -> dict[str, Any]:
    scenario = _scenario_data(sources.values())
    raw_repair = scenario.get("repair_context")
    if not isinstance(raw_repair, Mapping):
        raise ValueError("scenario_data.repair_context must be an object")
    repair = dict(raw_repair)
    _require_fields(
        repair,
        {"broken_purchase_order_refs", "broken_manufacturing_order_refs"},
        "scenario_data.repair_context",
    )
    broken_purchase_refs = _ref_list(
        repair["broken_purchase_order_refs"],
        "repair_context.broken_purchase_order_refs",
    )
    broken_manufacturing_refs = _ref_list(
        repair["broken_manufacturing_order_refs"],
        "repair_context.broken_manufacturing_order_refs",
    )
    purchase_rows = _rows_by_ref(scenario, "existing_purchase_orders")
    manufacturing_rows = _rows_by_ref(scenario, "existing_manufacturing_orders")
    cancellation_sources = {
        source.source_id: source
        for source in sources.values()
        if source.record_model == PO_CANCELLATION_VIEW_MODEL
    }
    broken_refs = [
        *(f"purchase.order:{item}" for item in broken_purchase_refs),
        *(
            f"mrp.production:{item}"
            for item in broken_manufacturing_refs
        ),
    ]
    target_records = []
    for record in contract.proposal_records:
        model, _, raw_ref = record.record_ref.partition(":")
        if model == "purchase.order":
            observed = purchase_rows.get(raw_ref)
        elif model == "mrp.production":
            observed = manufacturing_rows.get(raw_ref)
        else:
            raise ValueError(f"unsupported cancellation target model: {model!r}")
        if observed is None:
            raise ValueError(f"{record.record_ref}: target is absent from the frozen scenario")
        if contract.local_check_id == "target_is_exact_disrupted_commitment":
            if model == "purchase.order":
                required = {
                    "vendor_ref",
                    "product_code",
                    "quantity",
                    "price_unit",
                    "planned_arrival_days",
                    "origin_customer_refs",
                }
                _require_fields(observed, required, record.record_ref)
                _require_fields(repair, {"broken_supplier_vendor_ref"}, "repair_context")
                _validate_field_types(
                    repair,
                    "repair_context",
                    strings={"broken_supplier_vendor_ref"},
                )
                _validate_field_types(
                    observed,
                    record.record_ref,
                    strings={"vendor_ref", "product_code"},
                    numbers={
                        "quantity",
                        "price_unit",
                        "planned_arrival_days",
                    },
                    ref_lists={"origin_customer_refs"},
                )
            else:
                required = {
                    "product_code",
                    "bom_ref",
                    "quantity",
                    "workcenter_code",
                    "planned_start_days",
                    "planned_due_days",
                    "origin_customer_refs",
                }
                _require_fields(observed, required, record.record_ref)
                _require_fields(repair, {"broken_workcenter_code"}, "repair_context")
                _validate_field_types(
                    repair,
                    "repair_context",
                    strings={"broken_workcenter_code"},
                )
                _validate_field_types(
                    observed,
                    record.record_ref,
                    strings={"product_code", "bom_ref", "workcenter_code"},
                    numbers={
                        "quantity",
                        "planned_start_days",
                        "planned_due_days",
                    },
                    ref_lists={"origin_customer_refs"},
                )
        elif contract.local_check_id == "target_is_fresh_and_natively_cancellable":
            if model == "purchase.order":
                live_source = cancellation_sources.get(record.record_ref)
                if live_source is None or live_source.record_fields is None:
                    raise ValueError(
                        f"{record.record_ref}: missing admitted Odoo cancellation snapshot"
                    )
                observed = dict(live_source.record_fields)
                observed_revision = live_source.record_revision
            else:
                observed_revision = ""
            required = {"state"}
            if model == "purchase.order":
                required |= {"locked", "blocking_vendor_bill_refs"}
            _require_fields(observed, required, record.record_ref)
            _validate_field_types(
                observed,
                record.record_ref,
                strings={"state"},
                ref_lists=(
                    {"blocking_vendor_bill_refs"}
                    if model == "purchase.order"
                    else set()
                ),
            )
        target_records.append(
            {
                "record_ref": record.record_ref,
                "model": model,
                "record_revision": record.record_revision,
                "observed_revision": (
                    observed_revision
                    if contract.local_check_id
                    == "target_is_fresh_and_natively_cancellable"
                    else ""
                ),
                "proposal_values": dict(record.values),
                "observed_values": observed,
            }
        )
    if contract.local_check_id == "target_is_fresh_and_natively_cancellable":
        for row in target_records:
            if row["model"] != "purchase.order" or not row["observed_values"]:
                continue
            observed = row["observed_values"]
            if not isinstance(observed["locked"], bool) or not isinstance(
                observed["blocking_vendor_bill_refs"], list
            ):
                raise ValueError(
                    f"{row['record_ref']}: cancellation safety fields have invalid types"
                )
    return {
        "broken_record_refs": sorted(broken_refs),
        "broken_vendor_ref": repair.get("broken_supplier_vendor_ref"),
        "broken_workcenter_code": repair.get("broken_workcenter_code"),
        "target_records": target_records,
    }


_RESOLVER_BY_CHECK = {
    "target_is_exact_disrupted_commitment": "deterministic_field_match",
    "target_is_fresh_and_natively_cancellable": "odoo_native_transition",
    "cancellation_scope_is_exact": "deterministic_set_comparison",
}

_INPUT_SELECTORS = {"cancel_and_repair_supply.v1": _cancellation_inputs}


__all__ = [
    "ERPResolverContractError",
    "replay_erp_check",
    "resolve_erp_check",
    "select_erp_inputs",
]
