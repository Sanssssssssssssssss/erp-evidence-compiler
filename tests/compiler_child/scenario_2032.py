from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from erp_agent_odoo.capabilities.sales_order_acceptance import (
    OrderAcceptanceFacts,
    OrderAcceptanceView,
    order_acceptance_source,
)

TASK_DIR = (
    Path(__file__).resolve().parents[2]
    / ".."
    / "erp-agent-odoo-deps"
    / "erp-bench"
    / "tasks_ui"
    / "2032_medium_05_screened_buy_only_all_seeded"
).resolve()


def build_source_manifest() -> dict[str, Any]:
    scenario_path = TASK_DIR / "environment" / "scenario_data.json"
    scenario_text = scenario_path.read_text(encoding="utf-8")
    scenario = json.loads(scenario_text)
    scenario_fingerprint = hashlib.sha256(scenario_text.encode()).hexdigest()
    customers = {item["ref"]: item for item in scenario["customers"]}
    product = next(
        item
        for item in scenario["products"]
        if item["code"] == "PEA51ED26F3-LAB-HSP-009"
    )
    revision = f"erp-bench-seed:{scenario['seed']}"
    sources = []
    for order in scenario["existing_sales_orders"]:
        customer = customers[order["customer_ref"]]
        quantity = Decimal(str(order["quantity"]))
        list_price = Decimal(str(product["list_price"]))
        budget = Decimal(str(customer["budget_dollars"]))
        lead_days = Decimal(str(order["commitment_days"]))
        record_ref = str(order["ref"])
        fact_provenance = {
            "requested_quantity": [
                {
                    "source_ref": f"erp-bench:2032:order:{record_ref}",
                    "field_path": "/quantity",
                    "revision": revision,
                    "fingerprint": scenario_fingerprint,
                    "source_kind": "document",
                }
            ],
            "unit_list_price": [
                {
                    "source_ref": f"erp-bench:2032:product:{product['code']}",
                    "field_path": "/list_price",
                    "revision": revision,
                    "fingerprint": scenario_fingerprint,
                    "source_kind": "document",
                }
            ],
            "pretax_budget": [
                {
                    "source_ref": f"erp-bench:2032:customer:{customer['ref']}",
                    "field_path": "/budget_dollars",
                    "revision": revision,
                    "fingerprint": scenario_fingerprint,
                    "source_kind": "policy",
                }
            ],
            "lead_days": [
                {
                    "source_ref": f"erp-bench:2032:order:{record_ref}",
                    "field_path": "/commitment_days",
                    "revision": revision,
                    "fingerprint": scenario_fingerprint,
                    "source_kind": "document",
                }
            ],
        }
        view = OrderAcceptanceView(
            record_ref=record_ref,
            facts=OrderAcceptanceFacts(
                requested_quantity=quantity,
                unit_list_price=list_price,
                pretax_budget=budget,
                lead_days=lead_days,
                fact_provenance=fact_provenance,
            ),
        )
        source = order_acceptance_source(view)
        source_fingerprint = hashlib.sha256(source.content.encode()).hexdigest()
        sources.append(
            {
                "source_id": record_ref,
                "name": f"order acceptance facts {record_ref}",
                "evidence_type": "record",
                "source_content": source.content,
                "source_fingerprint": source_fingerprint,
                "already_persisted": True,
                "record_model": source.record_model,
                "record_revision": source.record_revision,
                "upstream_revision": source.record_revision,
                "target_record_ref": record_ref,
                "provenance": {
                    **source.provenance,
                    "erp_bench_task": "2032",
                    "scenario_seed": str(scenario["seed"]),
                },
            }
        )
    return {
        "sources": sources,
        "policy_excerpts": {
            "erp_bench.sales_order_acceptance.v1@1": {
                "policy_version": "erp-bench-2032-task-v1",
                "policy_basis": {
                    "source": "ERP-Bench task 2032 admitted instruction"
                },
                "values": {
                    "minimum_quantity": {"configured": True, "value": "17"},
                    "maximum_quantity": {"configured": True, "value": "25"},
                    "minimum_lead_days": {"configured": True, "value": "10"},
                },
            }
        },
    }


def assert_proposal_matches_oracle(proposal: dict[str, Any]) -> None:
    oracle = json.loads((TASK_DIR / "solution" / "optimal_plan.json").read_text(encoding="utf-8"))
    expected = {
        item["task_order_ref"]: (
            "confirm_sales_order" if item["accepted"] else "cancel_sales_order"
        )
        for item in oracle["allocations"]
    }
    actual = {
        item["record_ref"]: item["action"]
        for item in proposal["actions"]
    }
    assert actual == expected


__all__ = [
    "TASK_DIR",
    "assert_proposal_matches_oracle",
    "build_source_manifest",
]
