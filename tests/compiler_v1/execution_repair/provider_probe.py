"""One bounded provider request: wire format or the real numeric tool interface."""
import asyncio
import argparse
import json
import time
from pathlib import Path

from openai import OpenAI
from probe import get_settings, save
from app.agents.thinking import model_extra_body_for_thinking


def run(output_dir):
    settings = get_settings()
    assert settings.llm_base_url.rstrip("/") == "https://api.commandcode.ai/provider/v1"
    output_dir.mkdir(parents=True, exist_ok=False)
    body = model_extra_body_for_thinking(settings.llm_model, "disabled", settings.llm_base_url)
    assert body == {"thinking": {"type": "disabled"}}, body
    started = time.perf_counter()
    with OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=30, max_retries=0) as client:
        raw = client.chat.completions.with_raw_response.create(
            model=settings.llm_model, messages=[{"role": "user", "content": 'Return only this JSON: {"ok": true}'}],
            max_tokens=128, extra_body=body,
        )
        save(output_dir / "request.json", json.loads(raw.http_response.request.content))
        save(output_dir / "raw-response.json", raw.http_response.json())
        response = raw.parse()
    message = response.choices[0].message
    summary = {"passed": json.loads(message.content or "null") == {"ok": True}, "wall_seconds": round(time.perf_counter() - started, 3), "usage": response.usage.model_dump(), "finish_reason": response.choices[0].finish_reason}
    save(output_dir / "summary.json", summary)
    print(json.dumps(summary))
    return summary


def run_reference_probe(output_dir):
    from app.compiler_runtime.runtime import _sandbox_tools
    from app.compiler_runtime.models import RecordFieldLocator
    from app.compiler_runtime.proof_terms import CalculationWitness, replay_calculation_witness
    from test_input_contracts import sandbox, bind

    settings = get_settings()
    assert settings.llm_base_url.rstrip("/") == "https://api.commandcode.ai/provider/v1"
    assert settings.llm_model == "deepseek/deepseek-v4-flash"
    output_dir.mkdir(parents=True, exist_ok=False)
    state = sandbox()
    assert bind(state, claim_id="policy_limit")["ok"]
    assert state.bind_record_field_claim(source_id="record", subject="record", predicate="amount", value=90.25,
        locator=RecordFieldLocator(record_ref="record", record_revision="r1", field_path="/amount"), claim_id="requested_amount")["ok"]
    tool = next(item for item in _sandbox_tools(state, reference_ids_only=True) if item.name == "compute_witness")
    payload = {"instruction": "Call the tool to test whether the requested amount is at most the limit observed in the policy document. This is only a calculation, not authorization or a business verdict.",
        "check_id": "c", "facet_ref": "f", "prebound_claims": state.evidence_ir.model_dump(mode="json"),
        "sources": [item.content for item in state.source_records]}
    save(output_dir / "fixture.json", payload)
    started = time.perf_counter()
    with OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=45, max_retries=0) as client:
        raw = client.chat.completions.with_raw_response.create(
            model=settings.llm_model, messages=[{"role": "user", "content": json.dumps(payload)}],
            tools=[{"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.params_json_schema}}],
            tool_choice="auto",
            max_tokens=2048, extra_body=model_extra_body_for_thinking(settings.llm_model, "low", settings.llm_base_url),
        )
        save(output_dir / "request.json", json.loads(raw.http_response.request.content))
        save(output_dir / "raw-response.json", raw.http_response.json())
        response = raw.parse()
    calls = response.choices[0].message.tool_calls or []
    results = [json.loads(asyncio.run(tool.on_invoke_tool(None, call.function.arguments))) for call in calls if call.function.name == tool.name]
    save(output_dir / "tool-results.json", results)
    witness = CalculationWitness.model_validate(results[0]["witness"]) if len(calls) == len(results) == 1 and results[0].get("ok") else None
    summary = {"scope": "One real model tool selection/execution over prebound fixture Claims; not a full child or an efficiency rerun.",
        "passed": bool(witness and witness.result is True and witness.operation == "LTE"
            and witness.check_id == "c" and witness.facet_ref == "f"
            and [(term.ref.kind, term.ref.ref_id) for term in witness.operands] == [("CLAIM", "requested_amount"), ("CLAIM", "policy_limit")]
            and replay_calculation_witness(witness, claims={claim.id: claim for claim in state.evidence_ir.claims}, witnesses={}, policy_values={})),
        "provider_requests": 1, "transport_attempts": 1, "max_output_tokens": 2048,
        "wall_seconds": round(time.perf_counter() - started, 3), "usage": response.usage.model_dump() if response.usage else None}
    save(output_dir / "summary.json", summary)
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=["wire", "references"], default="wire")
    args = parser.parse_args()
    raise SystemExit(0 if (run_reference_probe if args.mode == "references" else run)(args.output_dir)["passed"] else 1)
