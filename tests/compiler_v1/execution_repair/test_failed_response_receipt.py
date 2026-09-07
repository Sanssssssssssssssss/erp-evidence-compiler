"""Offline SDK failure injection through the real wrapper and Compiler recorder."""
from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest
from agents import function_tool
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, RunErrorDetails

from app.compiler_runtime import runtime as runtime_module
from app.compiler_runtime.runtime import EvidenceCompilerRuntime, ExecutorSummary
from app.config import Settings
from app.llm import LlmClient
from app.runtime import agents_sdk


def test_evidence_verifier_uses_configured_reasoning_effort(monkeypatch):
    from test_kernel_evidence_review import _fixture
    from app.compiler_runtime.sandbox import EvidenceSandbox
    artifact, sources, pack = _fixture()
    runtime = EvidenceCompilerRuntime(SimpleNamespace(settings=SimpleNamespace(llm_model="offline")), requirement_pack=pack)
    observed = []
    def phase(**kwargs):
        observed.append(kwargs)
        return kwargs["output_type"](assessments=[{**item.model_dump(exclude={
            "claim_ids", "accepted_witness_ids", "source_ids", "examined_source_ids",
        }), "source_scope_reviewed": True} for item in artifact.assessments])
    monkeypatch.setattr(runtime, "_run_phase", phase)
    runtime.verify(plan=artifact.plan, sandbox=EvidenceSandbox.from_artifact(artifact=artifact, sources=sources.values()), policy_excerpt=pack.policy, focus_check_id=artifact.plan.nodes[0].id)
    assert observed[0].get("thinking_override") is None
    assert observed[0]["max_turns"] is None and not observed[0].get("tools")
    assert observed[0]["max_output_tokens"] is None


@function_tool
def offline_marker() -> str:
    """Unused tool that selects the tool-follow-up transport path."""
    return "unused"


@pytest.mark.parametrize("error_type", [RuntimeError, MaxTurnsExceeded, ModelBehaviorError])
def test_failed_stream_preserves_partial_receipt_without_protocol_retry(
    monkeypatch, error_type,
):
    failure = error_type("injected incomplete response")
    reasoning = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read the supplied evidence before deciding."}]}
    response = SimpleNamespace(output=[reasoning], usage={
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "input_tokens_details": {"cached_tokens": 30},
        "output_tokens_details": {"reasoning_tokens": 7},
    })
    streamed = SimpleNamespace(
        input="frozen input", new_items=[SimpleNamespace(raw_item=reasoning)],
        raw_responses=[response], last_agent=object(), context_wrapper=object(),
        input_guardrail_results=[], output_guardrail_results=[],
    )

    async def stream_events():
        yield object()
        raise failure

    streamed.stream_events = stream_events
    original_details = None
    if error_type is ModelBehaviorError:
        original_details = RunErrorDetails(**{
            field.name: getattr(streamed, field.name) for field in fields(RunErrorDetails)
        })
        failure.run_data = original_details

    closed = []
    attempts = []
    captured = []

    async def close():
        closed.append(True)

    config = SimpleNamespace(_invoice_openai_client=SimpleNamespace(close=close))

    def run_streamed(*_args, **_kwargs):
        attempts.append(True)
        return streamed

    monkeypatch.setattr(agents_sdk.Runner, "run_streamed", run_streamed)
    monkeypatch.setattr(runtime_module, "build_run_config", lambda *_args, **_kwargs: config)
    settings = Settings(
        _env_file=None, llm_provider="compatible", llm_api_key="offline-test-only",
        llm_model="deepseek/deepseek-v4-flash",
        llm_base_url="https://api.commandcode.ai/provider/v1",
    )
    llm = LlmClient(settings)
    with pytest.raises(error_type) as raised:
        EvidenceCompilerRuntime(llm)._run_phase(
            name="executor", prompt_file="evidence_executor.md",
            payload={"test": "partial response receipt"},
            output_type=ExecutorSummary, max_turns=1, result_sink=captured.append,
            tools=[offline_marker],
        )

    assert raised.value is failure
    assert attempts == [True]
    assert closed == [True]
    assert isinstance(failure.run_data, RunErrorDetails)
    if original_details is not None:
        assert failure.run_data is original_details
    for field in fields(RunErrorDetails):
        assert getattr(failure.run_data, field.name) is getattr(streamed, field.name)
    assert captured == [failure.run_data]
    assert len(llm.calls) == 1
    record = llm.calls[0]
    assert record.error == f"{error_type.__name__}: injected incomplete response"
    assert record.transport_attempt == 1
    assert record.provider_turn_count == 1
    assert record.logical_invocation_id.startswith("executor:revision-")
    assert not record.recovered_by
    assert record.usage == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "cached_tokens": 30, "reasoning_tokens": 7,
    }
    assert record.reasoning_full == "Read the supplied evidence before deciding."


@pytest.mark.parametrize("with_tools", [False, True])
def test_parse_failure_keeps_pre_parse_response_in_both_transport_modes(monkeypatch, with_tools):
    """SDK on_llm_end precedes parsing and raw_responses append on both paths."""
    response = SimpleNamespace(
        output=[{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Completed reasoning before truncated JSON."}]}],
        usage={"input_tokens": 51, "output_tokens": 9, "total_tokens": 60},
    )
    closed, routes, captured = [], [], []
    existing_hooks = object()

    async def close():
        closed.append(True)

    async def fail_parse(agent, input_value, kwargs):
        assert kwargs["hooks"] is existing_hooks
        details = RunErrorDetails(
            input=input_value, new_items=[], raw_responses=[], last_agent=agent,
            context_wrapper=object(), input_guardrail_results=[], output_guardrail_results=[],
        )
        # Use the actual schema parser; only the model transport is replaced.
        if agent.hooks is not None:
            await agent.hooks.on_llm_end(details.context_wrapper, agent, response)
        try:
            agent.output_type.validate_json("{")
        except ModelBehaviorError as exc:
            exc.run_data = details
            raise
        pytest.fail("Malformed JSON unexpectedly accepted")

    async def run(agent, input_value, **kwargs):
        routes.append("nonstream")
        return await fail_parse(agent, input_value, kwargs)

    def run_streamed(agent, input_value, **kwargs):
        routes.append("stream")

        async def stream_events():
            yield object()
            await fail_parse(agent, input_value, kwargs)

        return SimpleNamespace(stream_events=stream_events)

    config = SimpleNamespace(_invoice_openai_client=SimpleNamespace(close=close))
    monkeypatch.setattr(agents_sdk.Runner, "run", run)
    monkeypatch.setattr(agents_sdk.Runner, "run_streamed", run_streamed)
    monkeypatch.setattr(runtime_module, "build_run_config", lambda *_args, **_kwargs: config)
    settings = Settings(
        _env_file=None, llm_provider="compatible", llm_api_key="offline-test-only",
        llm_model="deepseek/deepseek-v4-flash",
        llm_base_url="https://api.commandcode.ai/provider/v1",
    )
    llm = LlmClient(settings)
    with pytest.raises(ModelBehaviorError) as raised:
        EvidenceCompilerRuntime(llm, hooks=existing_hooks)._run_phase(
            name="executor" if with_tools else "fine_verifier",
            prompt_file="evidence_executor.md" if with_tools else "evidence_verifier.md",
            payload={"test": "pre-parse receipt"}, output_type=ExecutorSummary,
            max_turns=1, result_sink=captured.append,
            tools=[offline_marker] if with_tools else [],
        )
    assert routes == ["stream"]
    assert closed == [True]
    assert captured == [raised.value.run_data]
    assert captured[0].raw_responses == [response]
    assert len(llm.calls) == 1
    record = llm.calls[0]
    assert record.transport_attempt == record.provider_turn_count == 1
    assert record.usage == {"prompt_tokens": 51, "completion_tokens": 9, "total_tokens": 60}
    assert record.reasoning_full == "Completed reasoning before truncated JSON."


def test_error_chain_keeps_underlying_os_reason_without_secrets():
    from app.runtime.retry import error_chain
    cause = OSError(11001, "private request URL must not be logged")
    failure = RuntimeError("outer wrapper")
    failure.__cause__ = cause
    assert error_chain(failure) == [{"type": "RuntimeError"}, {"type": "OSError", "errno": 11001}]
