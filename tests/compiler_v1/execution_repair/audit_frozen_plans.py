"""Re-expand previous real Compiler selections offline; never re-call a model."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]
from erp_agent_odoo.capabilities.proof_dag import compile_task_compiler_plan, load_proof_catalog, lower_erp_stage_to_proof_plan
from app.state.persistence import atomic_write_text


def audit():
    base = ROOT / "tests/compiler_v1/artifacts/compiler_current_validation_20260903"
    receipts = sorted((base / "repaired_cases").glob("*/receipt")) + sorted((base / "supplemental_cases").glob("*/receipt")) + [base / "external_catalog/receipt"]
    rows = []
    for receipt in receipts:
        def read(name):
            return json.loads((receipt / name).read_text(encoding="utf-8"))
        old = read("executor-plan.json")
        catalog = read("catalog.json") if receipt.parent.name == "external_catalog" else load_proof_catalog()
        row = {"receipt": str(receipt), "passed": False, "model_calls": 0}
        try:
            expanded = compile_task_compiler_plan(compiler_output=read("task-compiler-output.json"), manager_request=read("manager-proposal.json"), catalog=catalog, binding_values=old["bindings"])
            plan = lower_erp_stage_to_proof_plan(expanded)
            checks = [node for node in plan.nodes if node.kind == "CHECK"]
            row.update(passed=True, first_frontier_checks=len(checks), live_source_checks=sum(any(group.source == "LIVE_ODOO" for group in node.action_contract.resolver_program.evidence) for node in checks), modes=sorted({node.action_contract.execution_mode for node in checks}))
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    report = {"scope": "OFFLINE replay of frozen model routing; structural expansion only, not new model or Executor success", "results": rows}
    target = ROOT / "tests/compiler_v1/artifacts/execution_repair/frozen-plan-audit.json"
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return all(row["passed"] for row in rows)


if __name__ == "__main__":
    raise SystemExit(0 if audit() else 1)
