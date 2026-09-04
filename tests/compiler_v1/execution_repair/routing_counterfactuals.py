"""Small source-only routing controls. Expected routes never enter model input."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from routing_probe import load_proof_catalog, save


def material(policy):
    target = "sale.order:request-x"
    return {
        "manager_request": {
            "scenario_id": "review-material",
            "proposal_id": "manager:proposal:r1",
            "task_objective": "Review the proposed confirmation against all supplied materials. Do not execute it or change the proposed action.",
            "actions": [{
                "action_id": "confirm-x", "action_kind": "sale.order.action_confirm",
                "stage": "release", "target_record_refs": [target],
                "action_payload": {
                    "payload_version": 1, "snapshot_revision": "candidate:r1",
                    "records": [{"record_ref": target, "record_revision": "record:r1", "values": {
                        "state": "draft", "customer_ref": "customer:x", "product_code": "item:a",
                        "quantity": 4, "price_unit": 125, "commitment_days": 10,
                    }}],
                },
            }],
        },
        "sources": [
            {"source_id": "policy", "role": "instruction", "content": policy},
            {"source_id": "order-material", "role": "evidence", "content": json.dumps({
                "record_ref": target, "record_revision": "record:r1", "state": "draft",
                "customer_ref": "customer:x", "product_code": "item:a",
                "requested_quantity": 4, "customer_budget": 350, "list_price": 100,
                "requested_due_days": 7, "stock_available": 4,
            }, sort_keys=True)},
        ],
        "catalog": load_proof_catalog(),
    }


def seal(folder, payload, provenance):
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "input.json", payload)
    save(folder / "seal.json", {"input_sha256": hashlib.sha256((folder / "input.json").read_bytes()).hexdigest()})
    save(folder / "provenance.json", provenance)


def prepare(root):
    first = material("Cover every listed customer order in full. Sell at list price, stay within the customer's budget and meet the requested due date. Do not drop customer orders to improve margin. Use net-30 terms on retained sales orders.")
    second = material("Screen the listed customer requests before accepting them. Accept only requests whose budget covers full list price, whose quantity is between 2 and 8, and whose lead time is at least 5 days. Cancel draft requests that fail any condition; do not fulfill them. Sell accepted orders at list price and meet their due dates.")
    assert first["manager_request"] == second["manager_request"]
    assert first["sources"][1] == second["sources"][1]
    assert first["catalog"] == second["catalog"] and len(first["catalog"]["templates"]) == 6
    for name, payload in (("a", first), ("b", second)):
        seal(root / name, payload, {"kind": "synthetic counterfactual, NOT a benchmark or live Odoo snapshot", "changed_between_pair": "instruction source only", "proposal_deliberately_inconsistent": True})
    save(root / "expected-routes.json", {"a": "sales_order_release.v1", "b": "sales_order_disposition.v1"})


def check(root):
    expected = json.loads((root / "expected-routes.json").read_text(encoding="utf-8"))
    for name, template in expected.items():
        receipt = root / name / "receipt"
        summary = json.loads((receipt / "trial_summary.json").read_text(encoding="utf-8"))
        answer = json.loads((receipt / "compiler-answer.json").read_text(encoding="utf-8"))["routing"]
        assert summary["status"] == "PLAN_READY" and summary["template_count"] == 6
        assert summary["logical_calls"] == {"task_compiler": 1}
        assert answer["selected_template_ids"] == [template]
        assert len(answer["action_bindings"]) == 1 and not answer["unresolved_manager_inputs"]
        assert set(answer["action_bindings"][0]["source_ids"]) == {"policy", "order-material"}
    print("Both policy-only routing controls passed; no business verdict tested")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "check", "refresh-catalog"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--original", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.root)
    elif args.mode == "check":
        check(args.root)
    else:
        raw = (args.original / "input.json").read_bytes()
        old_seal = json.loads((args.original / "seal.json").read_text(encoding="utf-8"))
        assert hashlib.sha256(raw).hexdigest() == old_seal["input_sha256"]
        payload = json.loads(raw)
        old_catalog = payload["catalog"]
        payload["catalog"] = load_proof_catalog()
        # Only applicability prose changes; every registered check remains identical.
        def without_applicability(value):
            return json.loads(json.dumps(value), object_hook=lambda obj: {key: item for key, item in obj.items() if key != "applies_when"})
        assert without_applicability(old_catalog) == without_applicability(payload["catalog"])
        seal(args.root, payload, {"kind": "same sealed offline fixture with updated catalog applicability", "original_input": str(args.original.resolve()), "original_sha256": old_seal["input_sha256"], "manager_and_sources_unchanged": True})
