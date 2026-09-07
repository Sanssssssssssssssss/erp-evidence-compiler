from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TAU_SRC = ROOT.parent / "erp-harness-tau" / "src"
if TAU_SRC.is_dir():
    sys.path.insert(0, str(TAU_SRC))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "src"))
pytest.importorskip("tau_agent")

from app.runtime.reasoning_capture import extract_reasoning_from_result
from erp_agent_odoo.capabilities.proof_dag import action_proposal_from_manager_request

from erp_agent_odoo.compiler_child import extension


def test_reasoning_receipt_deduplicates_sdk_and_provider_views() -> None:
    reasoning = "inspect evidence, test the claim"
    result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                raw_item=SimpleNamespace(type="reasoning", content=[{"text": reasoning}])
            )
        ],
        raw_responses=[{"output": [{"type": "reasoning", "content": [{"text": reasoning}]}]}],
    )

    capture = extract_reasoning_from_result(result)

    assert capture is not None
    assert capture.text == reasoning
    assert capture.chars == len(reasoning)
    assert capture.chunks == 1


def test_retry_exhaustion_requires_revision_before_resume(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "child_paused",
                "payload": {
                    "pause": {
                        "status": "frontier_rolled_back",
                        "diagnostic_codes": ["TERMINAL_WITNESS_POLICY_MISSING"],
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert extension._resume_block(events)["diagnostic_codes"] == [
        "TERMINAL_WITNESS_POLICY_MISSING"
    ]

    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "checkpoint_revised", "payload": {}}) + "\n")
    assert extension._resume_block(events) is None


@pytest.mark.parametrize("status", ["batch_rolled_back", "batch_partially_committed", "frontier_rolled_back"])
def test_failed_batch_cannot_resume_until_explicit_recheck(tmp_path, status) -> None:
    events = tmp_path / "events.jsonl"
    failure = {"kind": "child_paused", "payload": {"pause": {"status": status}}} if status == "frontier_rolled_back" else {"kind": "model_thinking", "payload": {"status": status}}
    trail = [failure, {"kind": "checkpoint_saved", "payload": {}}, {"kind": "model_thinking", "payload": {"status": "completed"}}]
    events.write_text("\n".join(json.dumps(event) for event in trail) + "\n", encoding="utf-8")
    assert extension._resume_block(events)["status"] == status
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "checkpoint_revised", "payload": {}}) + "\n")
    assert extension._resume_block(events) is None


def test_parent_gets_repair_status_and_repeat_resume_makes_no_model_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERP_COMPILER_RUN_ROOT", str(tmp_path))
    directory = tmp_path / "parent_session" / "test-run"
    directory.mkdir(parents=True)
    checkpoint = {"compiler_run_id": "test-run", "revision": 1, "status": "running", "compile_status": "NON_CONVERGED"}
    (directory / "request.json").write_text("{}", encoding="utf-8")
    (directory / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []

    def compiler(request, saved, run_id, run_dir, checkpoint_sink, progress_sink):
        calls.append(run_id)
        progress_sink("model_thinking", {"status": "batch_partially_committed"}, "Batch needs repair")
        checkpoint_sink(checkpoint)
        return {"status": "completed"}

    monkeypatch.setattr(extension, "_run_compiler", compiler)
    monkeypatch.setattr(extension, "_proof_snapshot", lambda saved: {**extension._checkpoint_summary(saved), "compile_status": saved["compile_status"]})
    tau = _Tau(tmp_path)
    extension.setup(tau)

    async def scenario():
        first = await tau.tools["evidence_reviewer"].execute("first", {"compiler_run_id": "test-run"})
        assert first.details["status"] == "requires_revision"
        assert first.details["next_action"] == "recheck_evidence_review"
        assert json.loads(first.content[0].text)["compile_status"] == "NON_CONVERGED"
        second = await tau.tools["evidence_reviewer"].execute("repeat", {"compiler_run_id": "test-run"})
        assert second.details["status"] == "requires_revision"
        assert calls == ["test-run"]

    asyncio.run(scenario())


def test_typed_manager_proposal_round_trips_into_child_request() -> None:
    manager_request = {
        "scenario_id": "repair-one-order",
        "proposal_id": "proposal:repair:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:42"],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": "snapshot:r1",
                    "records": [
                        {
                            "record_ref": "purchase.order:42",
                            "record_revision": "write-date:r1",
                            "values": {"state": "purchase"},
                        }
                    ],
                },
            }
        ],
    }
    proposal = action_proposal_from_manager_request(manager_request)

    rebuilt = extension._manager_request_from_proposal(proposal)

    assert rebuilt["scenario_id"] == proposal.proposal_id
    assert rebuilt["actions"][0]["action_kind"] == "purchase.order.button_cancel"
    assert rebuilt["actions"][0]["target_record_refs"] == ["purchase.order:42"]
    assert action_proposal_from_manager_request(rebuilt).proposal_hash == proposal.proposal_hash


def test_admitted_action_receipt_unlocks_only_its_completed_action() -> None:
    from app.compiler_runtime.freshness import ActionOutcome, ActionReceipt
    from app.compiler_runtime.runtime import PreparedSource
    from app.compiler_runtime.sandbox import SourceRecord

    manager_request = {
        "scenario_id": "repair-two-orders",
        "proposal_id": "proposal:repair:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:42"],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": "snapshot:r1",
                    "records": [
                        {
                            "record_ref": "purchase.order:42",
                            "record_revision": "write-date:r1",
                            "values": {"state": "purchase"},
                        }
                    ],
                },
            },
            {
                "action_id": "release_replacement_po",
                "action_kind": "purchase.order.button_confirm",
                "stage": "replacement_release",
                "target_record_refs": ["purchase.order:43"],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": "snapshot:r1",
                    "records": [
                        {
                            "record_ref": "purchase.order:43",
                            "record_revision": "write-date:r1",
                            "values": {"state": "draft"},
                        }
                    ],
                },
            },
        ],
    }
    proposal = action_proposal_from_manager_request(manager_request)
    receipt = ActionReceipt(
        receipt_id="receipt:cancel:r1",
        requirement_id="erp_action_plan_valid",
        proposal_hash=proposal.proposal_hash,
        source_snapshot_hash="prior-source-snapshot",
        policy_hash="policy-hash",
        observed_at="2026-09-02T00:00:00Z",
        outcomes=(
            ActionOutcome(
                record_ref="purchase.order:42",
                action="purchase.order.button_cancel",
                status="SUCCEEDED",
                before_revision="write-date:r1",
                after_revision="write-date:r2",
                result_ref="receipt/raw/cancel.json",
            ),
        ),
    )
    record = SourceRecord(
        source_id="source:action-receipt",
        content=json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
        kind="receipt",
        provenance={"role": "action_receipt"},
    )

    completed = extension._completed_action_ids_from_sources(
        proposal,
        [PreparedSource(record=record, metadata={"source_fingerprint": "unused"})],
        policy_hash="policy-hash",
    )

    assert completed == {"cancel_broken_po"}


class _Tau:
    def __init__(self, cwd: Path) -> None:
        self.context = SimpleNamespace(session_id="parent_session", cwd=cwd)
        self.tools: dict[str, Any] = {}
        self.entries: list[dict[str, Any]] = []
        self.progress_seen = asyncio.Event()
        self.guidelines: list[str] = []

    def register_tool(self, tool: Any) -> None:
        self.tools[tool.name] = tool

    async def append_entry(self, namespace: str, data: dict[str, Any]) -> None:
        self.entries.append({"namespace": namespace, **data})
        if data.get("payload", {}).get("status") == "executor_running":
            self.progress_seen.set()

    def add_prompt_guideline(self, guideline: str) -> None:
        self.guidelines.append(guideline)


class CompilerSupervisionPause(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__("paused")


def test_child_is_traced_while_running_and_resumes_after_error(tmp_path, monkeypatch) -> None:
    release = threading.Event()
    calls: list[dict[str, Any] | None] = []
    manager_request = {
        "scenario_id": "durable-child",
        "proposal_id": "proposal:durable-child:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:42"],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": "snapshot:r1",
                    "records": [
                        {
                            "record_ref": "purchase.order:42",
                            "record_revision": "write-date:r1",
                            "values": {"state": "purchase"},
                        }
                    ],
                },
            }
        ],
    }
    proposal = action_proposal_from_manager_request(manager_request)
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "instruction-1",
                        "source_content": "Cancel only a reviewed disrupted commitment.",
                        "provenance": {"role": "instruction"},
                    },
                    {
                        "source_id": "order-1",
                        "source_content": "Purchase order 42 is disrupted.",
                        "provenance": {"role": "scenario_data"},
                    },
                ],
                "proposals": [proposal.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ERP_COMPILER_SOURCE_MANIFEST", str(manifest))
    admitted = extension._resolve_sources(["instruction-1", "order-1"])
    assert admitted[0]["already_persisted"] is True
    assert len(admitted[0]["source_fingerprint"]) == 64

    def fake_compiler(
        request: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        run_id: str,
        run_dir: Path,
        checkpoint_sink: Any,
        progress_sink: Any,
    ) -> dict[str, Any]:
        del request, run_dir
        calls.append(checkpoint)
        base = {
            "compiler_run_id": run_id,
            "revision": int((checkpoint or {}).get("revision") or 1),
            "status": "running",
            "active_check_id": "",
            "completed_check_ids": [],
        }
        if len(calls) == 1:
            checkpoint_sink(base)
            payload = {"compiler_run_id": run_id, "status": "plan_ready"}
            assert progress_sink("model_thinking", payload, "plan ready") is True
            raise CompilerSupervisionPause(payload)
        if len(calls) == 2:
            checkpoint_sink({**base, "active_check_id": "check.invoice"})
            progress_sink(
                "model_thinking",
                {"compiler_run_id": run_id, "status": "executor_running"},
                "executor running",
            )
            assert release.wait(timeout=5)
            raise RuntimeError("deterministic child crash")
        completed = {
            **base,
            "status": "completed",
            "completed_check_ids": ["check.invoice"],
        }
        checkpoint_sink(completed)
        return {"status": "completed", "review_result": {"decision": "SUPPORTED"}}

    def fake_recheck(
        checkpoint: dict[str, Any],
        *,
        correction_id: str,
        expected_revision: int,
        check_id: str,
        message: str,
    ) -> dict[str, Any]:
        assert correction_id == "fix-check-invoice"
        assert expected_revision == checkpoint["revision"]
        assert check_id == "check.invoice"
        assert message == "The completed audit check used the wrong boundary."
        return {
            **checkpoint,
            "revision": checkpoint["revision"] + 1,
            "status": "running",
            "completed_check_ids": [],
        }

    monkeypatch.setattr(extension, "_run_compiler", fake_compiler)
    monkeypatch.setattr(extension, "_recheck_compiler", fake_recheck)
    monkeypatch.setenv("ERP_COMPILER_RUN_ROOT", str(tmp_path / "runs"))
    tau = _Tau(tmp_path)
    extension.setup(tau)

    async def scenario() -> None:
        start = await tau.tools["evidence_reviewer"].execute(
            "call-1",
            {
                "task_objective": "Review the proposed cancellation.",
                "proposal_ref": proposal.proposal_id,
                "source_refs": ["instruction-1", "order-1"],
            },
        )
        assert start.details["status"] == "paused"
        run_id = start.details["compiler_run_id"]

        crashing = asyncio.create_task(
            tau.tools["evidence_reviewer"].execute(
                "call-2",
                {"compiler_run_id": run_id},
            )
        )
        await asyncio.wait_for(tau.progress_seen.wait(), timeout=5)
        assert not crashing.done()
        assert any(
            entry.get("payload", {}).get("status") == "executor_running"
            for entry in tau.entries
        )
        release.set()
        failed = await crashing
        assert failed.details["status"] == "error"
        assert failed.details["active_check_id"] == "check.invoice"

        restarted_tau = _Tau(tmp_path)
        extension.setup(restarted_tau)
        resumed = await restarted_tau.tools["evidence_reviewer"].execute(
            "call-3",
            {"compiler_run_id": run_id},
        )
        assert resumed.details["status"] == "completed"
        assert resumed.details["completed_check_ids"] == ["check.invoice"]
        assert calls[1] is not None and calls[1]["active_check_id"] == ""
        assert calls[2] is not None and calls[2]["active_check_id"] == "check.invoice"
        assert calls[2]["compiler_run_id"] == calls[1]["compiler_run_id"]

        revised = await restarted_tau.tools["recheck_evidence_review"].execute(
            "call-4",
            {
                "compiler_run_id": run_id,
                "correction_id": "fix-check-invoice",
                "expected_revision": 1,
                "check_id": "check.invoice",
                "message": "The completed audit check used the wrong boundary.",
            },
        )
        assert revised.details["revision"] == 2
        assert revised.details["checkpoint_status"] == "running"
        rerun = await restarted_tau.tools["evidence_reviewer"].execute(
            "call-5",
            revised.details["resume_input"],
        )
        assert rerun.details["status"] == "completed"
        assert calls[3] is not None and calls[3]["revision"] == 2

        inspected = await restarted_tau.tools["inspect_evidence_review"].execute(
            "call-6",
            {"compiler_run_id": run_id, "after_event_seq": 0},
        )
        assert inspected.details["checkpoint_status"] == "completed"
        assert inspected.details["next_event_seq"] == len(tau.entries) + len(restarted_tau.entries)
        assert (tmp_path / "runs" / "parent_session" / run_id / "request.json").is_file()
        assert (tmp_path / "runs" / "parent_session" / run_id / "checkpoint.json").is_file()

    asyncio.run(scenario())


@pytest.mark.parametrize("custom_catalog", [False, True])
def test_manager_only_erp_call_freezes_the_new_compiler_request(
    tmp_path, monkeypatch, custom_catalog
) -> None:
    manager_request = {
        "scenario_id": "repair-one-order",
        "proposal_id": "proposal:repair:r1",
        "actions": [
            {
                "action_id": "cancel_broken_po",
                "action_kind": "purchase.order.button_cancel",
                "stage": "cancel",
                "target_record_refs": ["purchase.order:42"],
                "action_payload": {
                    "payload_version": 1,
                    "snapshot_revision": "snapshot:r1",
                    "records": [
                        {
                            "record_ref": "purchase.order:42",
                            "record_revision": "write-date:r1",
                            "values": {
                                "state": "purchase",
                                "vendor_ref": "vendor:broken",
                                "product_code": "PRODUCT-1",
                                "quantity": 13,
                                "unit_cost": 127.15,
                                "planned_arrival_days": 8,
                                "origin_refs": ["sale.order:one"],
                            },
                        }
                    ],
                },
            }
        ],
    }
    proposal = action_proposal_from_manager_request(manager_request)
    sources = [
        {
            "source_id": "source:instruction",
            "source_content": "Cancel only the disrupted commitment.",
            "provenance": {"role": "instruction"},
        },
        {
            "source_id": "source:scenario",
            "source_content": "{}",
            "provenance": {"role": "scenario_data"},
        },
    ]
    for source in sources:
        source["source_fingerprint"] = hashlib.sha256(
            source["source_content"].encode()
        ).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": sources,
                "proposals": [proposal.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ERP_COMPILER_SOURCE_MANIFEST", str(manifest))
    monkeypatch.setenv("ERP_COMPILER_RUN_ROOT", str(tmp_path / "runs"))
    from erp_agent_odoo.capabilities.proof_dag import load_proof_catalog

    catalog = load_proof_catalog()
    if custom_catalog:
        catalog["deployment_revision"] = "numeric-controls:r1"
        catalog_path = tmp_path / "trusted-catalog.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        monkeypatch.setenv("ERP_COMPILER_REVIEW_CATALOG", str(catalog_path))
    else:
        monkeypatch.delenv("ERP_COMPILER_REVIEW_CATALOG", raising=False)
    captured: dict[str, Any] = {}

    def fake_compiler(
        request: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        run_id: str,
        run_dir: Path,
        checkpoint_sink: Any,
        progress_sink: Any,
    ) -> dict[str, Any]:
        del checkpoint, run_dir, progress_sink
        captured.update(request)
        checkpoint_sink(
            {
                "compiler_run_id": run_id,
                "revision": 1,
                "status": "completed",
                "active_check_id": "",
                "completed_check_ids": [],
            }
        )
        return {"status": "completed"}

    monkeypatch.setattr(extension, "_run_compiler", fake_compiler)
    tau = _Tau(tmp_path)
    extension.setup(tau)

    result = asyncio.run(
        tau.tools["evidence_reviewer"].execute(
            "call-erp",
            {
                "task_objective": "Review the proposed cancellation.",
                "proposal_ref": proposal.proposal_id,
                "source_refs": ["source:instruction", "source:scenario"],
            },
        )
    )

    assert result.details["status"] == "completed"
    assert "review_mode" not in captured
    assert captured["requirement_pack_id"] == "evidence_action_review_v1"
    assert captured["active_requirement_ids"] == ["erp_action_plan_valid"]
    assert set(captured["catalog"]) >= {"templates", "shared_nodes"}
    assert captured["catalog"] == catalog
    assert captured["action_proposal"]["proposal_hash"] == proposal.proposal_hash
    assert set(tau.tools["evidence_reviewer"].parameters["properties"]) == {
        "compiler_run_id",
        "task_objective",
        "source_refs",
        "proposal_ref",
    }


def test_parent_snapshot_exposes_plan_proof_and_lineage() -> None:
    from app.compiler_runtime.models import (
        CheckAssessment,
        CompiledProof,
        DecisionProof,
        EvidenceIR,
        EvidenceSourceDescriptor,
        ProofNode,
        ProofPlan,
        ReviewArtifact,
    )
    from app.compiler_runtime.runtime import CompilerRunCheckpoint

    plan = ProofPlan(
        plan_id="plan-visible",
        objective="Inspect one fact",
        active_requirement_ids=["invoice"],
        policy_refs=[],
        nodes=[
            ProofNode(
                id="check.invoice",
                kind="CHECK",
                statement="The invoice exists.",
                requirement_refs=["invoice"],
            )
        ],
        roots={"invoice": "check.invoice"},
    )
    fingerprint = hashlib.sha256(b"invoice-1").hexdigest()
    evidence = EvidenceIR(
        source_ids=["invoice-1"],
        source_fingerprints={"invoice-1": fingerprint},
        source_descriptors={
            "invoice-1": EvidenceSourceDescriptor(
                source_type="document",
                fingerprint=fingerprint,
            )
        },
    )
    assessment = CheckAssessment(check_id="check.invoice", status="SUPPORTED")
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        proof_signature_hash="sha256:signature",
        evidence_ir=evidence,
        source_snapshot_hash=hashlib.sha256(b"source-snapshot").hexdigest(),
        evidence_snapshot_hash=evidence.content_hash(),
        assessments=[assessment],
        policy_hash="sha256:policy",
        compiler_version="test",
        model="test",
    )
    artifact = artifact.model_copy(update={"artifact_hash": artifact.content_hash()})
    proof = CompiledProof(
        decisions=[
            DecisionProof(
                requirement_id="invoice",
                root_node_id="check.invoice",
                status="SUPPORTED",
                supporting_check_ids=["check.invoice"],
                plan_hash=artifact.plan_hash,
                evidence_snapshot_hash=artifact.evidence_snapshot_hash,
                policy_hash=artifact.policy_hash,
                stop_reason="supported",
            )
        ]
    )
    snapshot = extension._proof_snapshot(
        CompilerRunCheckpoint(
            compiler_run_id="compiler-visible",
            status="completed",
            compile_status="COMMITTED",
            semantic_status="SUPPORTED",
            completed_check_ids=["check.invoice"],
            artifact=artifact,
            proof=proof,
        ).model_dump(mode="json")
    )

    assert snapshot["checks"][0]["statement"] == "The invoice exists."
    assert snapshot["decisions"][0]["status"] == "SUPPORTED"
    assert snapshot["lineage"]["artifact_hash"] == artifact.artifact_hash
