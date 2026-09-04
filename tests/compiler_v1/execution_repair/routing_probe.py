"""Two-step, Compiler-only audit probe. No expected answers or business execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src"), str(ROOT)]

from agents.memory import SQLiteSession
from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK
from app.compiler_runtime.runtime import PreparedSource
from app.compiler_runtime.sandbox import SourceRecord
from app.config import get_settings
from app.llm import LlmClient
from app.state.persistence import atomic_write_text
from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog
from erp_agent_odoo.evidence_review import compile_review
from tests.compiler_v1.compiler_only_probe import _summary
from tests.compiler_v1.execution_repair.probe import ReceiptRuntime, jsonable, save
from tests.compiler_v1.proof_corpus.task_compiler_probe import (
    _load_case_manifest, _load_manager_proposal, _sources,
)

COLLECTIONS = (
    "warehouses", "vendors", "customers", "products", "boms", "workcenters",
    "stock_levels", "existing_sales_orders", "existing_purchase_orders",
    "existing_manufacturing_orders",
)
INTERNAL_KEYS = {"plan_ref", "offer_key", "supply_role", "origin_plan_refs"}


def strip_internal(value):
    if isinstance(value, dict):
        return {key: strip_internal(item) for key, item in value.items() if key not in INTERNAL_KEYS}
    if isinstance(value, list):
        return [strip_internal(item) for item in value]
    return value


def prepare(manifest_path, proposal_path, destination, label):
    """Mechanical fixture projection, not native Odoo extraction or a solution."""
    destination.mkdir(parents=True, exist_ok=False)
    original = _sources(_load_case_manifest(manifest_path))
    proposal = _load_manager_proposal(proposal_path)
    proposal["scenario_id"] = label
    proposal["proposal_id"] = f"manager:candidate:{label}:r1"
    sources, provenance = [], []
    for source in original.values():
        content = source["content"]
        discarded = []
        if source["role"] != "instruction":
            document = json.loads(content)
            discarded = sorted(set(document) - set(COLLECTIONS))
            content = json.dumps(strip_internal({key: document[key] for key in COLLECTIONS}), ensure_ascii=False, indent=2)
        sources.append({"source_id": source["source_id"], "role": "instruction" if source["role"] == "instruction" else "evidence", "content": content})
        provenance.append({"source_id": source["source_id"], "original_path": str(source["path"]), "original_sha256": source["sha256"], "original_chars": source["chars"], "admitted_chars": len(content), "admitted_sha256": hashlib.sha256(content.encode()).hexdigest(), "discarded_top_level_keys": discarded})
    save(destination / "input.json", {"manager_request": proposal, "sources": sources, "catalog": load_proof_catalog()})
    save(destination / "provenance.json", {"kind": "seed-derived offline business fixture, NOT a native Odoo snapshot or a benchmark entrant", "all_records_retained": True, "nested_keys_removed": sorted(INTERNAL_KEYS), "sources": provenance, "original_proposal_path": str(proposal_path.resolve()), "neutral_scenario_label": label})
    save(destination / "seal.json", {"input_sha256": hashlib.sha256((destination / "input.json").read_bytes()).hexdigest()})
    print(json.dumps({"prepared": str(destination), "templates_visible": len(load_proof_catalog()["templates"]), "source_chars": [len(item["content"]) for item in sources]}))


class CompilerReceiptRuntime(ReceiptRuntime):
    def _run_phase(self, **kwargs):
        if kwargs["name"] != "task_compiler":
            raise AssertionError("This probe must not invoke Executor or Verifier")
        kwargs["session"] = self.compiler_session
        return super()._run_phase(**kwargs)


def run(input_dir, output_dir=None):
    raw = (input_dir / "input.json").read_bytes()
    seal = json.loads((input_dir / "seal.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == seal["input_sha256"], "Audited input changed"
    payload = json.loads(raw)
    settings = get_settings()
    if (settings.llm_base_url.rstrip("/"), settings.llm_model, settings.llm_thinking_type) != ("https://api.commandcode.ai/provider/v1", "deepseek/deepseek-v4-flash", "high") or not settings.llm_api_key:
        raise ValueError("Requires existing CommandCode high profile; no fallback provider")
    settings = settings.model_copy(update={"evidence_reviewer_timeout_seconds": 120.0})
    output = output_dir if output_dir is not None else input_dir / "receipt"
    output.mkdir(parents=True, exist_ok=False)
    save(output / "code-hashes.json", {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__), ROOT / "src/erp_agent_odoo/evidence_review.py", ROOT / "src/erp_agent_odoo/capabilities/proof_dag.py", ROOT / "src/erp_agent_odoo/capabilities/proof_templates.json", ROOT / "backend/app/compiler_runtime/prompts/erp_task_compiler.md", ROOT / "backend/app/compiler_runtime/runtime.py")})
    prepared = [PreparedSource(record=SourceRecord(source_id=item["source_id"], title=item["source_id"], kind="document", content=item["content"], provenance={"role": item["role"], "fixture": True}), metadata={"source_fingerprint": hashlib.sha256(item["content"].encode()).hexdigest()}) for item in payload["sources"]]
    llm = LlmClient(settings)
    def event(kind, details, summary):
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "payload": jsonable(details), "summary": summary}, ensure_ascii=False) + "\n")
    runtime = CompilerReceiptRuntime(llm, settings=settings, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK, output_dir=output, progress_sink=event)
    runtime.compiler_session = SQLiteSession("task_compiler", db_path=output / "session.sqlite")
    start = time.perf_counter()
    failure, answer, plan = None, None, None
    event("probe_started", {"input_sha256": seal["input_sha256"]}, "Compiler only; no business execution")
    try:
        plan, proposal, answer = compile_review(runtime, manager_request=payload["manager_request"], sources=prepared, catalog=payload["catalog"])
        save(output / "compiler-answer.json", answer)
        save(output / "proof-plan.json", plan)
        save(output / "checkpoint.json", {"kind": "frontend_plan_receipt", "status": "PLAN_READY", "input_sha256": seal["input_sha256"], "proposal_hash": proposal.proposal_hash, "proof_plan": plan, "executor_invocations": 0, "verifier_invocations": 0, "semantic_status": None})
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        atomic_write_text(output / "model-calls.jsonl", "".join(json.dumps(jsonable(call), ensure_ascii=False) + "\n" for call in llm.calls))
        atomic_write_text(output / "reasoning.txt", "\n\n".join(call.reasoning_full for call in llm.calls))
    summary = {"status": "PLAN_READY" if plan else "FAILED", "failure": failure, "scope": "Task Compiler only, not business approval or full child", "input_dir": str(input_dir.resolve()), "input_sha256": seal["input_sha256"], "logical_calls": runtime.phase_counts, "metrics": _summary([jsonable(call) for call in llm.calls], time.perf_counter() - start), "template_count": len(payload["catalog"]["templates"]), "source_chars": [len(item["content"]) for item in payload["sources"]], "selected_templates": answer["routing"]["selected_template_ids"] if answer else None, "frontier_check_count": len([node for node in plan.nodes if node.kind == "CHECK"]) if plan else None}
    with sqlite3.connect(output / "session.sqlite") as db:
        summary["sqlite_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    save(output / "trial_summary.json", summary)
    event("probe_finished", summary, "No Executor/Verifier called")
    print(json.dumps(summary, ensure_ascii=False))
    return plan is not None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "run", "check"])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.mode == "check":
        assert strip_internal({"note": "supplier cancelled", "records": [{"ref": "a", "plan_ref": "hidden"}, {"ref": "b", "offer_key": "hidden"}]}) == {"note": "supplier cancelled", "records": [{"ref": "a"}, {"ref": "b"}]}
        print("Projection check passed; no model call")
    elif args.mode == "prepare":
        prepare(args.manifest, args.proposal, args.input_dir, args.label)
    else:
        raise SystemExit(0 if run(args.input_dir, args.output_dir) else 1)
