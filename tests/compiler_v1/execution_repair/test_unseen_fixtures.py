"""Offline fixture/wiring checks; these do not count as live model results."""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe import load_fixture, save
from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import ReviewArtifact, CompiledProof
from app.compiler_runtime.requirement_pack import EVIDENCE_ACTION_REVIEW_PACK
from app.compiler_runtime.runtime import _validate_registered_proof_plan, policy_excerpt_for, CompilerRunCheckpoint, prepared_sources_from_checkpoint
from erp_agent_odoo.evidence_review import ReviewRouting, compile_review
from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog


@pytest.mark.parametrize("name", ["material-a", "material-b", "material-c", "material-d"])
def test_unseen_fixture_retains_sources_catalog_and_generic_execution(name):
    path = Path(__file__).parent / "unseen_cases" / f"{name}.json"
    request, sources, catalog = load_fixture(path)
    expected = json.loads(path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    assert len(catalog["templates"]) == len(load_proof_catalog()["templates"]) + 4 and len(sources) == 5
    assert set(request["source_refs"]) == {source.source_id for source in sources}
    assert "pack_id" not in request and "expected" not in request
    assert all(source.source_fingerprint == hashlib.sha256(source.record.content.encode()).hexdigest() for source in sources)
    captured = []
    def phase(**kwargs):
        captured.append(kwargs)
        return ReviewRouting(
            scenario_id=request["scenario_id"],
            selected_template_ids=list(expected["routes"].values()),
            action_bindings=[{"proposal_action_id": action, "template_id": template, "source_ids": request["source_refs"]} for action, template in expected["routes"].items()],
            unresolved_manager_inputs=[],
        )
    runtime = SimpleNamespace(compile_review_route=phase, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)
    plan, proposal, _ = compile_review(runtime, manager_request=request, sources=sources, catalog=catalog)
    payload = captured[0]["payload"]
    assert set(payload) == {"manager_request", "registered_review_catalog", "sources"}
    assert {item["source_id"]: item["content"] for item in payload["sources"]} == {item.source_id: item.record.content for item in sources}
    checks = [node for node in plan.nodes if node.kind == "CHECK"]
    assert len(checks) == 2
    assert all(node.action_contract.execution_mode == "evidence_review" for node in checks)
    _validate_registered_proof_plan(plan, action_proposal=proposal, prepared_sources=sources,
        policy_excerpt=policy_excerpt_for(plan.active_requirement_ids, EVIDENCE_ACTION_REVIEW_PACK),
        requirement_pack=EVIDENCE_ACTION_REVIEW_PACK)


def audit_receipt(directory):
    """Re-run only the deterministic Kernel on the actual saved model proof."""
    artifact = ReviewArtifact.model_validate_json((directory / "artifact.json").read_text(encoding="utf-8"))
    checkpoint = CompilerRunCheckpoint.model_validate_json((directory / "checkpoint.json").read_text(encoding="utf-8"))
    sources = prepared_sources_from_checkpoint(checkpoint)
    replayed = compile_review_artifact(artifact, requirement_pack=EVIDENCE_ACTION_REVIEW_PACK,
        source_records={item.source_id: item.record for item in sources})
    saved = CompiledProof.model_validate_json((directory / "proof.json").read_text(encoding="utf-8"))
    assert replayed == saved and not replayed.diagnostics
    calls = [json.loads(line) for line in (directory / "model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
    original_content = {item.source_id: item.record.content for item in sources}
    for call in calls:
        assert {item["source_id"]: item["content"] for item in call["payload"]["sources"]} == original_content
    invocations = {role: len({call["logical_invocation_id"] for call in calls if call["role"] == role}) for role in ("task_compiler", "executor", "fine_verifier")}
    assert all(count == 1 for count in invocations.values())
    result = {"kernel_replay_identical": True, "complete_source_content_in_all_model_phases": True,
        "source_count": len(sources), "logical_invocations": invocations,
        "statuses": [item.status for item in replayed.decisions], "diagnostics": [],
        "new_model_calls": 0,
        "input_hashes": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("artifact.json", "checkpoint.json", "proof.json", "model-calls.jsonl")}}
    save(directory / "offline-audit.json", result)
    print(json.dumps({"receipt": str(directory), **result}))


if __name__ == "__main__":
    for value in sys.argv[1:]:
        audit_receipt(Path(value))
