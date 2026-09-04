"""Complete Compiler/Executor/Verifier probes; no Odoo or oracle access."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src"), str(ROOT)]

from app.compiler_runtime import runtime as runtime_module
from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK
from app.compiler_runtime.runtime import EvidenceCompilerRuntime, PreparedSource
from app.compiler_runtime.sandbox import SourceRecord
from app.config import get_settings
from app.llm import LlmClient
from app.state.persistence import atomic_write_text
from erp_agent_odoo.evidence_review import compile_review
from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog
from tests.compiler_v1.compiler_only_probe import _summary


def jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def save(path, value):
    atomic_write_text(path, json.dumps(jsonable(value), ensure_ascii=False, indent=2))


def control(variant="supported"):
    """Synthetic materials, never presented as live Odoo/benchmark evidence."""
    amount = 6000 if variant == "contradicted" else 4000
    policy = SourceRecord(
        source_id="policy:controls", title="Release policy", kind="document",
        content="Per transfer limit is USD 5000.00. A transfer needs approval by both Mira and Leon and a CLEAR screening result tied to that request. Disposal needs a quality officer's signed authorization for the same quarantined batch and quantity.",
        provenance={"role": "instruction"},
    )
    fields = {"request_id": "payment:q7", "currency": "USD", "amount": amount, "approvers": ["Mira", "Leon"]}
    if variant != "missing":
        fields["screening"] = {"request_id": "payment:q7", "result": "CLEAR", "revision": "screen:r3"}
    payment = SourceRecord(source_id="payment:q7", title="Payment request", kind="record", content="", record_model="treasury.request", record_revision="request:r3", structured_fields=fields, provenance={"role": "evidence", "fixture": True})
    disposal = SourceRecord(source_id="disposal:b8", title="Quality authorization", kind="document", content="Batch b8: 12 units quarantined for water damage. Quality officer Inez authorizes destruction of these same 12 units. Signed Inez, 2026-09-03. No other batch is authorized.", provenance={"role": "evidence", "fixture": True})
    prepared = [PreparedSource(record=record, metadata={"source_fingerprint": hashlib.sha256(record.content.encode()).hexdigest()}) for record in (policy, payment, disposal)]
    proposal = {"scenario_id": "external-control", "proposal_id": "proposal:control:r3", "task_objective": "Review the proposed transfer and batch destruction against all supplied materials; do not execute either action.", "actions": []}
    for action_id, kind, target, values in (
        ("transfer", "treasury.payment.release", "payment:q7", {"amount": amount, "currency": "USD"}),
        ("destroy", "warehouse.stock.destroy", "batch:b8", {"quantity": 12}),
    ):
        proposal["actions"].append({"action_id": action_id, "action_kind": kind, "stage": "release", "target_record_refs": [target], "action_payload": {"payload_version": 1, "snapshot_revision": "proposal:r3", "records": [{"record_ref": target, "record_revision": "candidate:r3", "values": values}]}})
    observations = {
        "amount_limit": "The proposed transfer amount and currency match the request and satisfy the policy's per-transfer limit. Use the numeric comparison tool.",
        "transfer_authorization": "The same payment request has both distinct required approvers and a CLEAR screening result explicitly tied to it. A screening revision without the result is insufficient.",
        "disposal_authorization": "The proposed destruction is for the same quarantined batch and quantity that the quality officer explicitly authorized and signed; no additional batch or quantity is authorized.",
    }
    recipes = {}
    for check_id in observations:
        recipes[check_id] = {"resolver": "semantic_evidence", "requires_calculation": check_id == "amount_limit", "evidence": [
            {"group_id": "policy", "source": "BOUND_SOURCE", "source_role": "instruction", "method": "READ_AND_EXTRACT", "facts": ["applicable rule", "scope and exceptions"]},
            {"group_id": "material", "source": "BOUND_SOURCE", "source_role": "evidence", "method": "READ_AND_EXTRACT", "facts": ["target identity", "amount or quantity", "authorization", "screening result if transfer"]},
        ]}
    templates = []
    for template_id, action_kind, check_ids in (
        ("treasury_controls.v1", "treasury.payment.release", ["amount_limit", "transfer_authorization"]),
        ("stock_disposal.v1", "warehouse.stock.destroy", ["disposal_authorization"]),
    ):
        templates.append({"id": template_id, "protects": [action_kind], "applicable_stages": ["release"], "requires_shared": [], "checks": [{"id": key, "observation": observations[key], "resolver": "semantic_evidence"} for key in check_ids]})
    return proposal, prepared, {"templates": templates, "shared_nodes": [], "proof_recipes": recipes}


class ReceiptRuntime(EvidenceCompilerRuntime):
    def __init__(self, *args, output_dir, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_dir = output_dir
        self.phase_counts = {}

    def _run_phase(self, **kwargs):
        name = kwargs["name"]
        self.phase_counts[name] = self.phase_counts.get(name, 0) + 1
        if self.phase_counts[name] > 1:
            raise RuntimeError(f"Logical phase budget exceeded: {name}")
        previous_sink = kwargs.get("result_sink")
        captures = 0
        def capture(result):
            nonlocal captures
            captures += 1
            save(self.output_dir / f"{name}-attempt-{captures}-sdk-responses.json", getattr(result, "raw_responses", []))
            save(self.output_dir / f"{name}-attempt-{captures}-items.json", [getattr(item, "raw_item", item) for item in getattr(result, "new_items", [])])
            save(self.output_dir / f"{name}-sdk-responses.json", getattr(result, "raw_responses", []))
            save(self.output_dir / f"{name}-items.json", [getattr(item, "raw_item", item) for item in getattr(result, "new_items", [])])
            if previous_sink:
                previous_sink(result)
        kwargs["result_sink"] = capture
        return super()._run_phase(**kwargs)


def load_fixture(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    extra = json.loads((path.parent / data["catalog_file"]).read_text(encoding="utf-8"))
    catalog = load_proof_catalog()
    assert not {item["id"] for item in catalog["templates"]} & {item["id"] for item in extra["templates"]}
    assert not set(catalog["proof_recipes"]) & set(extra["proof_recipes"])
    catalog["templates"].extend(extra["templates"])
    catalog["proof_recipes"].update(extra["proof_recipes"])
    records = [SourceRecord(**item) for item in data["sources"]]
    prepared = [PreparedSource(record=item, metadata={"source_fingerprint": hashlib.sha256(item.content.encode()).hexdigest()}) for item in records]
    return data["manager_request"], prepared, catalog


def run(variant, output_dir, *, fixture_path=None):
    if output_dir.exists():
        raise ValueError("Use a fresh receipt directory; never overwrite a run")
    output_dir.mkdir(parents=True)
    proposal, prepared, catalog = load_fixture(fixture_path) if fixture_path else control(variant)
    save(output_dir / "request.json", proposal)
    save(output_dir / "catalog.json", catalog)
    save(output_dir / "source-snapshot.json", runtime_module._source_snapshot(prepared))
    code_paths = [*ROOT.joinpath("backend/app/compiler_runtime").rglob("*.py"), *ROOT.joinpath("backend/app/compiler_runtime/prompts").glob("*.md"), ROOT / "backend/app/runtime/agents_sdk.py", ROOT / "src/erp_agent_odoo/evidence_review.py", ROOT / "src/erp_agent_odoo/capabilities/proof_dag.py", ROOT / "src/erp_agent_odoo/capabilities/proof_templates.json", ROOT / "policies/evidence_action_review_v1.json", Path(__file__)]
    code_paths.extend([ROOT / "backend/app/agents/thinking.py", ROOT / "backend/app/runtime/context_partition.py", ROOT / "backend/app/runtime/reasoning_capture.py"])
    if fixture_path:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        code_paths.extend([fixture_path.resolve(), (fixture_path.parent / data["catalog_file"]).resolve()])
    save(output_dir / "code-hashes.json", {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in code_paths})
    settings = get_settings()
    if settings.llm_base_url.rstrip("/") != "https://api.commandcode.ai/provider/v1" or settings.llm_model != "deepseek/deepseek-v4-flash" or not settings.llm_api_key:
        raise ValueError("This probe requires the configured CommandCode profile")
    llm = LlmClient(settings)
    def progress(kind, payload, summary):
        with (output_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "payload": jsonable(payload), "summary": summary}, ensure_ascii=False) + "\n")
    runtime = ReceiptRuntime(llm, settings=settings, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK, output_dir=output_dir, progress_sink=progress, executor_session_db_path=output_dir / "session.sqlite")
    started = time.perf_counter()
    result = None
    failure = None
    try:
        plan, action_proposal, frontend = compile_review(runtime, manager_request=proposal, sources=prepared, catalog=catalog)
        save(output_dir / "compiler-answer.json", frontend)
        save(output_dir / "proof-plan.json", plan)
        if fixture_path:
            # Test-only gate: never put expected routes or verdicts in a model payload.
            expected_routes = json.loads(fixture_path.with_suffix(".expected.json").read_text(encoding="utf-8"))["routes"]
            actual_routes = {item["proposal_action_id"]: item["template_id"] for item in frontend["routing"]["action_bindings"]}
            if actual_routes != expected_routes:
                raise AssertionError(f"Compiler route mismatch: {actual_routes}")
        result = runtime.run(active_requirement_ids=["erp_action_plan_valid"], prepared_sources=prepared, action_proposal=action_proposal, proof_plan=plan, compiler_run_id=proposal["scenario_id"] if fixture_path else f"control-{variant}", checkpoint_sink=lambda checkpoint: save(output_dir / "checkpoint.json", checkpoint))
        save(output_dir / "artifact.json", result.artifact)
        save(output_dir / "proof.json", result.proof)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        with (output_dir / "model-calls.jsonl").open("w", encoding="utf-8") as stream:
            for call in llm.calls:
                stream.write(json.dumps(jsonable(call), ensure_ascii=False) + "\n")
        atomic_write_text(output_dir / "reasoning.txt", "\n\n".join(f"{call.role}\n{call.reasoning_full}" for call in llm.calls))
    expected = {"supported": "SUPPORTED", "contradicted": "CONTRADICTED", "missing": "NOT_FOUND"}[variant]
    turns = [call.provider_turn_count for call in llm.calls]
    summary = {"variant": variant, "fixture_kind": "synthetic complete-material control, not live Odoo", "expected": expected, "compile_status": result.compile_status if result else None, "semantic_status": result.semantic_status if result else None, "failure": failure, "logical_calls": runtime.phase_counts, "provider_turns": sum(turns) if turns and all(value is not None for value in turns) else None, "usage": [jsonable(call.usage) for call in llm.calls], "wall_seconds": round(time.perf_counter()-started, 3), "passed": bool(result and result.compile_status == "COMMITTED" and result.semantic_status == expected)}
    summary["metrics"] = _summary([call.to_debug_dict() for call in llm.calls], summary["wall_seconds"], checkpoint=jsonable(result.checkpoint) if result else None)
    summary["provider_turns"] = summary["metrics"]["provider_turn_count"]
    summary["correctness_passed"] = summary["passed"]
    if (output_dir / "session.sqlite").exists():
        with sqlite3.connect(output_dir / "session.sqlite") as db:
            summary["sqlite_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    save(output_dir / "trial_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["supported", "contradicted", "missing"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    summary = run(args.variant, args.output_dir, fixture_path=args.fixture)
    raise SystemExit(0 if summary["correctness_passed" if args.fixture else "passed"] else 1)
