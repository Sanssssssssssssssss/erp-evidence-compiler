from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src"), str(ROOT)]

from app.compiler_runtime.runtime import PreparedSource
from app.config import get_settings
from app.observability.model_metrics import summarize_model_metrics
from app.state.persistence import atomic_write_text

from erp_agent_odoo.compiler_child.extension import _run_compiler
from tests.compiler_v1.cases import vendor_bill_posting_run_case

COMMANDCODE_BASE_URL = "https://api.commandcode.ai/provider/v1"
COMMANDCODE_MODEL = "deepseek/deepseek-v4-flash"
PHASE7_REQUEST = (
    Path(__file__).parent
    / "artifacts"
    / "phase7"
    / "probe_20260830T223631Z_0c7cb652"
    / "child-runs"
    / "probe_20260830T223631Z_0c7cb652"
    / "compiler_b7d7bf90b6e8"
    / "request.json"
)


def _source_request(source: PreparedSource) -> dict[str, Any]:
    return {
        "already_persisted": True,
        "evidence_type": source.record.kind,
        "name": source.record.title,
        "provenance": dict(source.record.provenance),
        "record_model": source.record.record_model,
        "record_revision": source.record.record_revision,
        "source_content": source.canonical_content,
        "source_fingerprint": source.source_fingerprint,
        "source_id": source.source_id,
        "target_record_ref": source.metadata["target_record_ref"],
        "upstream_revision": source.upstream_revision,
    }


def _vendor_request(*, received_quantity: str) -> dict[str, Any]:
    _view, proposal, policy, pack, plan, prepared, _runtime = (
        vendor_bill_posting_run_case(received_quantity=received_quantity)
    )
    return {
        "task_objective": "Review whether the proposed vendor bill posting is evidence-supported.",
        "requirement_pack_id": pack.pack_id,
        "requirement_pack_version": pack.version,
        "requirement_pack_hash": pack.content_hash,
        "active_requirement_ids": list(plan.active_requirement_ids),
        "sources": [_source_request(prepared)],
        "policy_excerpt": policy.to_policy_excerpt(),
        "requirement_requiredness": {
            requirement_id: pack.default_required(requirement_id)
            for requirement_id in plan.active_requirement_ids
        },
        "action_proposal": proposal.model_dump(mode="json"),
    }


def _load_case(name: str) -> tuple[dict[str, Any], str]:
    if name == "2032-batch":
        return json.loads(PHASE7_REQUEST.read_text(encoding="utf-8")), "SUPPORTED"
    if name == "vendor-supported":
        return _vendor_request(received_quantity="10"), "SUPPORTED"
    if name == "vendor-contradicted":
        return _vendor_request(received_quantity="8"), "CONTRADICTED"
    raise ValueError(f"Unknown Compiler-only case: {name}")


def _reasoning_tokens(call: dict[str, Any]) -> int | None:
    usage = call.get("usage") or {}
    details = usage.get("output_tokens_details") or usage.get(
        "completion_tokens_details"
    ) or {}
    value = details.get("reasoning_tokens", usage.get("reasoning_tokens"))
    return int(value) if value is not None else None


def _summary(
    calls: list[dict[str, Any]],
    wall_seconds: float,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Debug metrics may infer totals or discard zero. Raw normalized usage is
    # authoritative when present; legacy flat receipts remain readable.
    token_keys = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")
    calls = [{**call, **({key: (call.get("usage") or {}).get(key) for key in token_keys} if "usage" in call else {})} for call in calls]
    metrics = summarize_model_metrics(calls)
    prompt_tokens = int(metrics["prompt_tokens"])
    cached_tokens = int(metrics["cached_tokens"])
    logical_ids = {
        str(call.get("logical_invocation_id") or "")
        for call in calls
        if call.get("logical_invocation_id")
    }
    executor_ids = {
        str(call.get("logical_invocation_id") or "")
        for call in calls
        if call.get("role") == "executor" and call.get("logical_invocation_id")
    }
    verifier_ids = {
        str(call.get("logical_invocation_id") or "")
        for call in calls
        if call.get("role") == "fine_verifier"
        and call.get("logical_invocation_id")
    }
    provider_turns = [
        int(call["provider_turn_count"])
        for call in calls
        if call.get("provider_turn_count") is not None
    ]
    transport_attempts = [
        int(call["transport_attempt"])
        for call in calls
        if call.get("transport_attempt") is not None
    ]
    checkpoint = checkpoint or {}
    revision = int(checkpoint.get("revision") or 1)
    coverage = {
        key: bool(calls) and all(call.get(key) is not None and key not in (call.get("usage") or {}).get("partial_metrics", []) for call in calls)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "ttft_ms")
    }
    reasoning = [_reasoning_tokens(call) for call in calls]
    coverage["reasoning_tokens"] = bool(calls) and all(value is not None and "reasoning_tokens" not in (call.get("usage") or {}).get("partial_metrics", []) for call, value in zip(calls, reasoning))
    complete_turns = bool(calls) and len(provider_turns) == len(calls) and not any(call.get("error") and call.get("provider_turn_count") == 0 for call in calls)
    return {
        **metrics,
        **{key: metrics.get(key) if covered else None for key, covered in coverage.items() if key != "reasoning_tokens"},
        "known_token_totals": {key: metrics[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")},
        "uncached_prompt_tokens": max(0, prompt_tokens - cached_tokens) if coverage["prompt_tokens"] and coverage["cached_tokens"] else None,
        "reasoning_tokens": sum(reasoning) if coverage["reasoning_tokens"] else None,
        "cache_hit_ratio": cached_tokens / prompt_tokens if coverage["cached_tokens"] and coverage["prompt_tokens"] and prompt_tokens else None,
        "model_latency_ms": round(
            sum(float(call.get("latency_ms") or 0) for call in calls), 2
        ),
        "wall_seconds": round(wall_seconds, 3),
        "roles": [str(call.get("role") or "") for call in calls],
        "logical_executor_invocations": len(executor_ids),
        "logical_verifier_invocations": len(verifier_ids),
        "outer_phase_count": len(logical_ids),
        "provider_turn_count": sum(provider_turns) if complete_turns else None,
        "observed_provider_responses": sum(provider_turns),
        "transport_attempt_count": len(transport_attempts) if transport_attempts else None,
        "proof_repair_revision_count": max(0, revision - 1),
        "provisional_proof_status": checkpoint.get("compile_status"),
        "published_semantic_status": checkpoint.get("semantic_status"),
        "usage_coverage": coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        required=True,
        choices=("2032-batch", "vendor-supported", "vendor-contradicted"),
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or (
        Path(__file__).parent
        / "artifacts"
        / "phase9"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{args.case}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    request, expected_semantic_status = _load_case(args.case)
    run_id = f"{args.case}_{uuid4().hex[:12]}"
    atomic_write_text(
        output_dir / "request.json",
        json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True),
    )
    events_path = output_dir / "events.jsonl"
    checkpoint_path = output_dir / "checkpoint.json"
    receipt: dict[str, Any] = {
        "case": args.case,
        "run_id": run_id,
        "expected_semantic_status": expected_semantic_status,
        "profile": {},
        "outcome": None,
        "metrics": None,
        "error": None,
        "passed": False,
    }

    def checkpoint_sink(payload: dict[str, Any]) -> None:
        atomic_write_text(
            checkpoint_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def progress_sink(kind: str, payload: dict[str, Any], summary: str) -> bool:
        with events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {"kind": kind, "summary": summary, "payload": payload},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            )
        return False

    settings = get_settings()
    receipt["profile"] = {
        "provider": settings.llm_provider,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "thinking_type": settings.llm_thinking_type,
    }
    started = time.perf_counter()
    try:
        if settings.llm_base_url.rstrip("/") != COMMANDCODE_BASE_URL:
            raise ValueError("Compiler-only probe is CommandCode-only")
        if settings.llm_model != COMMANDCODE_MODEL:
            raise ValueError("Compiler-only probe requires the pinned CommandCode model")
        if not settings.llm_api_key:
            raise ValueError("Compiler-only probe requires LLM_API_KEY")
        outcome = _run_compiler(
            request,
            None,
            run_id,
            output_dir,
            checkpoint_sink,
            progress_sink,
        )
        checkpoint = outcome.get("checkpoint") or {}
        receipt["outcome"] = {
            "status": outcome.get("status"),
            "compile_status": checkpoint.get("compile_status"),
            "semantic_status": checkpoint.get("semantic_status"),
            "completed_check_ids": checkpoint.get("completed_check_ids") or [],
        }
        receipt["passed"] = bool(
            outcome.get("status") == "completed"
            and checkpoint.get("compile_status") == "COMMITTED"
            and checkpoint.get("semantic_status") == expected_semantic_status
        )
    except Exception as exc:  # noqa: BLE001 - persist the real child failure
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        wall_seconds = time.perf_counter() - started
        model_log = output_dir / "model-calls.jsonl"
        calls = (
            [json.loads(line) for line in model_log.read_text(encoding="utf-8").splitlines()]
            if model_log.is_file()
            else []
        )
        receipt["metrics"] = _summary(calls, wall_seconds, checkpoint=checkpoint)
        atomic_write_text(
            output_dir / "summary.json",
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True),
        )
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "case": args.case,
                "outcome": receipt["outcome"],
                "metrics": receipt["metrics"],
                "error": receipt["error"],
                "summary": str(output_dir / "summary.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
