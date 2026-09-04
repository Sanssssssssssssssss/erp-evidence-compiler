"""One independent Verifier/Kernel call on an unchanged saved Executor candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from probe import ROOT, ReceiptRuntime, get_settings, LlmClient, save, jsonable
from app.compiler_runtime.models import ReviewArtifact
from app.compiler_runtime.runtime import CompilerRunCheckpoint, prepared_sources_from_checkpoint
from app.compiler_runtime.sandbox import EvidenceSandbox
from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK


def candidate_from_receipt(directory):
    checkpoint = CompilerRunCheckpoint.model_validate_json((directory / "checkpoint.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    candidate = next(row["payload"] for row in reversed(events) if row["kind"] == "rejected_candidate" and row["payload"].get("bindings"))
    submissions = {}
    with sqlite3.connect(f"file:{(directory / 'session.sqlite').as_posix()}?mode=ro", uri=True) as database:
        for (raw,) in database.execute("SELECT message_data FROM agent_messages ORDER BY id"):
            item = json.loads(raw)
            if item.get("type") == "function_call_output":
                output = json.loads(item["output"])
                if output.get("ok") and output.get("action") == "submit_check":
                    submission = output["submission"]
                    submissions[submission["check_id"]] = submission
    data = checkpoint.artifact.model_dump(mode="json")
    data.update(evidence_ir=candidate["evidence_ir"], binding_proposals=candidate["bindings"], calculation_witnesses=candidate["witnesses"], assessments=[], artifact_hash="")
    for field, key in (("submitted_claim_refs", "claim_ids"), ("submitted_binding_refs", "binding_ids"), ("submitted_witness_refs", "witness_ids")):
        data[field] = {check: row[key] for check, row in submissions.items()}
    artifact = ReviewArtifact.model_validate(data)
    artifact = artifact.model_copy(update={"evidence_snapshot_hash": artifact.evidence_ir.content_hash(), "execution_status": "COMPLETED"})
    artifact = artifact.model_copy(update={"artifact_hash": artifact.content_hash()})
    assert set(submissions) == {node.id for node in artifact.plan.nodes if node.kind == "CHECK"}
    assert all(item.record.content and item.source_fingerprint == artifact.evidence_ir.source_fingerprints[item.source_id] for item in prepared_sources_from_checkpoint(checkpoint))
    return checkpoint, artifact


def run(directory, output_dir, expected="SUPPORTED"):
    checkpoint, candidate = candidate_from_receipt(directory)
    output_dir.mkdir(parents=True, exist_ok=False)
    save(output_dir / "code-hashes.json", {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ("backend/app/agents/thinking.py", "backend/app/compiler_runtime/runtime.py", "backend/app/compiler_runtime/kernel.py", "backend/app/compiler_runtime/prompts/evidence_verifier.md", "backend/app/runtime/agents_sdk.py", "backend/app/runtime/context_partition.py", "tests/compiler_v1/execution_repair/verify_receipt.py")})
    save(output_dir / "candidate.json", candidate)
    save(output_dir / "replay-of.json", {"directory": str(directory.resolve()), "input_file_hashes": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("checkpoint.json", "events.jsonl", "session.sqlite")}, "plan_hash": candidate.plan_hash, "proposal_hash": candidate.proposal_hash, "candidate_hash": candidate.artifact_hash, "source_snapshot_hash": candidate.source_snapshot_hash})
    settings = get_settings()
    assert settings.llm_base_url.rstrip("/") == "https://api.commandcode.ai/provider/v1" and settings.llm_model == "deepseek/deepseek-v4-flash" and settings.llm_api_key
    settings = settings.model_copy(update={"evidence_reviewer_timeout_seconds": 120.0})
    llm = LlmClient(settings)
    runtime = ReceiptRuntime(llm, settings=settings, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK, output_dir=output_dir)
    sources = prepared_sources_from_checkpoint(checkpoint)
    sandbox = EvidenceSandbox.from_artifact(artifact=candidate, sources=[item.record for item in sources])
    started = time.perf_counter()
    proof, failure = None, None
    try:
        assessments = runtime.verify(plan=candidate.plan, sandbox=sandbox, policy_excerpt=candidate.policy_snapshot, focus_check_id=list(candidate.submitted_claim_refs))
        artifact = candidate.model_copy(update={"assessments": assessments})
        artifact = artifact.model_copy(update={"artifact_hash": artifact.content_hash()})
        proof = compile_review_artifact(artifact, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK, source_records={item.source_id: item.record for item in sources})
        save(output_dir / "artifact.json", artifact)
        save(output_dir / "proof.json", proof)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        save(output_dir / "model-calls.json", llm.calls)
    summary = {"scope": "Verifier/Kernel isolation; not a new full child", "logical_calls": runtime.phase_counts, "failure": failure, "statuses": [item.status for item in proof.decisions] if proof else [], "diagnostics": jsonable(proof.diagnostics) if proof else [], "wall_seconds": round(time.perf_counter() - started, 3), "usage": [jsonable(call.usage) for call in llm.calls], "provider_turns": [call.provider_turn_count for call in llm.calls]}
    summary["expected"] = expected
    summary["passed"] = bool(proof and proof.decisions and not proof.diagnostics and all(item.status == expected for item in proof.decisions))
    save(output_dir / "trial_summary.json", summary)
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected", choices=["SUPPORTED", "CONTRADICTED", "NOT_FOUND"], default="SUPPORTED")
    args = parser.parse_args()
    raise SystemExit(0 if run(args.receipt, args.output_dir, args.expected)["passed"] else 1)
