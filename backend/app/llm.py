from __future__ import annotations

import json
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.agents.thinking import model_extra_body_for_thinking, role_thinking_type, temperature_for_thinking
from app.config import Settings, get_settings
from app.observability.langfuse_tracer import (
    LangfuseTracer,
    cost_details,
    generation_hash_metadata,
    generation_input,
    generation_output,
    usage_details,
)
from app.observability.model_metrics import build_model_metrics
from app.runtime.reasoning_capture import extract_reasoning_from_result
from app.runtime.context_partition import (
    build_context_packet,
    prompt_cache_model_settings_kwargs,
    usage_from_result,
    with_usage_metrics,
)


@dataclass
class ModelCallRecord:
    role: str
    model: str
    prompt_version: str
    input_preview: str
    output_preview: str
    error: str = ""
    error_details: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    payload: Any = None
    raw_response: str = ""
    usage: dict[str, Any] | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    provider_request_id: str = ""
    finish_reason: str = ""
    schema_validation_error: str = ""
    schema_retry_attempted: bool = False
    retry_of: str = ""
    content_chars: int = 0
    recovered_by: str = ""
    runtime: str = "openai_agents_sdk"
    reasoning_excerpt: str = ""
    reasoning_full: str = ""
    reasoning_chars: int = 0
    reasoning_chunks: int = 0
    thinking_type: str = ""
    reasoning_source: str = ""
    logical_invocation_id: str = ""
    transport_attempt: int | None = None
    provider_turn_count: int | None = None
    prompt_partition: dict[str, Any] = field(default_factory=dict)

    def metrics(self) -> dict[str, Any]:
        return build_model_metrics(
            usage=self.usage,
            prompt_partition=self.prompt_partition,
            latency_ms=self.latency_ms,
            ttft_ms=self.ttft_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "runtime": self.runtime,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "schema_validation_error": self.schema_validation_error,
            "schema_retry_attempted": self.schema_retry_attempted,
            "retry_of": self.retry_of,
            "error": self.error,
            "error_details": self.error_details,
            "content_chars": self.content_chars,
            "recovered_by": self.recovered_by,
            "reasoning_chars": self.reasoning_chars,
            "reasoning_chunks": self.reasoning_chunks,
            "thinking_type": self.thinking_type,
            "reasoning_source": self.reasoning_source,
            "logical_invocation_id": self.logical_invocation_id,
            "transport_attempt": self.transport_attempt,
            "provider_turn_count": self.provider_turn_count,
            "prompt_partition": {
                key: self.prompt_partition.get(key)
                for key in (
                    "stable_prefix_hash",
                    "dynamic_context_hash",
                    "volatile_tail_hash",
                    "prompt_cache_key",
                    "prompt_tokens",
                    "cached_tokens",
                    "cache_hit_ratio",
                )
                if key in self.prompt_partition
            },
        }
        payload.update(self.metrics())
        return payload

    def to_debug_dict(self) -> dict[str, Any]:
        payload = {
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "runtime": self.runtime,
            "system_prompt": self.system_prompt,
            "payload": self.payload,
            "raw_response": self.raw_response,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "usage": self.usage or {},
            "provider_request_id": self.provider_request_id,
            "finish_reason": self.finish_reason,
            "schema_validation_error": self.schema_validation_error,
            "schema_retry_attempted": self.schema_retry_attempted,
            "retry_of": self.retry_of,
            "error": self.error,
            "error_details": self.error_details,
            "content_chars": self.content_chars,
            "recovered_by": self.recovered_by,
            "reasoning_excerpt": self.reasoning_excerpt,
            "reasoning_full": self.reasoning_full,
            "reasoning_chars": self.reasoning_chars,
            "reasoning_chunks": self.reasoning_chunks,
            "thinking_type": self.thinking_type,
            "reasoning_source": self.reasoning_source,
            "logical_invocation_id": self.logical_invocation_id,
            "transport_attempt": self.transport_attempt,
            "provider_turn_count": self.provider_turn_count,
            "prompt_partition": self.prompt_partition,
        }
        payload.update(self.metrics())
        return payload


class LlmClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.calls: list[ModelCallRecord] = []
        self.tracer = LangfuseTracer.disabled()
        self._runtime_hooks: ContextVar[Any | None] = ContextVar(
            f"llm_runtime_hooks_{id(self)}",
            default=None,
        )

    def set_tracer(self, tracer: LangfuseTracer) -> None:
        self.tracer = tracer

    def bind_runtime_hooks(self, hooks: Any | None) -> Token[Any | None]:
        return self._runtime_hooks.set(hooks)

    def reset_runtime_hooks(self, token: Token[Any | None]) -> None:
        self._runtime_hooks.reset(token)

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def complete_structured(
        self,
        *,
        role: str,
        system_prompt: str,
        payload: dict[str, Any],
        model_type: type[BaseModel],
        prompt_version: str,
        model: str | None = None,
        hooks: Any | None = None,
    ) -> BaseModel:
        from agents import Agent, ModelSettings
        from app.runtime.agents_sdk import FencedJsonOutputSchema

        hooks = hooks if hooks is not None else self._runtime_hooks.get()
        input_preview = json.dumps(payload, ensure_ascii=False, default=str)[:1400]
        selected_model = model or self.settings.llm_model
        timeout_seconds = self.settings.timeout_for_role(role)
        thinking_type = role_thinking_type(role, payload, self.settings.llm_thinking_type)
        started = time.perf_counter()
        trace_input = generation_input(
            role,
            prompt_version,
            system_prompt,
            payload,
            mode=self.tracer.capture_payloads,
        )
        trace_metadata = {
            "role": role,
            "prompt_version": prompt_version,
            "temperature": temperature_for_thinking(selected_model, self.settings.llm_temperature, thinking_type),
            "thinking_type": thinking_type,
            "provider": self.settings.llm_provider,
            "base_url": self.settings.llm_base_url,
            "schema": model_type.__name__,
            "runtime": "openai_agents_sdk_direct",
            "timeout_seconds": timeout_seconds,
            **generation_hash_metadata(system_prompt, payload),
        }
        packet = build_context_packet(
            role=role,
            prompt_version=prompt_version,
            prompt_file="",
            system_prompt=system_prompt,
            context_payload=payload,
            settings=self.settings,
            output_model=model_type,
        )
        trace_metadata.update(packet.debug_metadata())
        managed_prompt = self.tracer.managed_prompt(
            role=role,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            model=selected_model,
        )
        with self.tracer.generation(
            f"llm.{role}",
            model=selected_model,
            input=trace_input,
            metadata=trace_metadata,
            prompt=managed_prompt,
        ) as generation:
            if not self.available:
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error="llm_unavailable",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                    runtime="openai_agents_sdk_direct",
                    prompt_partition=packet.debug_metadata(),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error="llm_unavailable", mode=self.tracer.capture_payloads),
                    metadata={"runtime": record.runtime, **record.metrics(), **packet.debug_metadata()},
                    level="ERROR",
                    status_message="llm_unavailable",
                )
                raise RuntimeError("LLM_API_KEY is required for structured agent output.")
            try:
                agent = Agent(
                    name=role,
                    instructions=system_prompt,
                    model=selected_model,
                    model_settings=ModelSettings(
                        temperature=temperature_for_thinking(selected_model, self.settings.llm_temperature, thinking_type),
                        extra_body=_model_extra_body(
                            selected_model,
                            thinking_type,
                            self.settings.llm_base_url,
                        ),
                        **prompt_cache_model_settings_kwargs(self.settings, packet),
                    ),
                    output_type=FencedJsonOutputSchema(model_type, strict_json_schema=False),
                )
                from app.runtime.agents_sdk import run_agent_sync

                result = run_agent_sync(
                    agent,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    hooks=hooks,
                    run_config=self._run_config(
                        workflow_name=f"invoice_agent.{role}",
                        trace_metadata={
                            "role": role,
                            "prompt_version": prompt_version,
                            "payload_sha256": trace_metadata["payload_sha256"],
                            "runtime": "openai_agents_sdk_direct",
                            "timeout_seconds": timeout_seconds,
                            **packet.debug_metadata(),
                        },
                        timeout_seconds=timeout_seconds,
                    ),
                )
                parsed = result.final_output
                if not isinstance(parsed, model_type):
                    parsed = model_type.model_validate(parsed)
                raw_response = parsed.model_dump_json()
                usage = usage_from_result(result)
                partition = with_usage_metrics(packet.debug_metadata(), usage)
                reasoning = extract_reasoning_from_result(result, final_output=raw_response)
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview=raw_response[:1400],
                    system_prompt=system_prompt,
                    payload=payload,
                    raw_response=raw_response,
                    usage=usage,
                    latency_ms=_elapsed_ms(started),
                    content_chars=len(raw_response),
                    runtime="openai_agents_sdk_direct",
                    reasoning_excerpt=reasoning.text if reasoning else "",
                    reasoning_full=reasoning.full_text if reasoning else "",
                    reasoning_chars=reasoning.chars if reasoning else 0,
                    reasoning_chunks=reasoning.chunks if reasoning else 0,
                    thinking_type=thinking_type,
                    reasoning_source=reasoning.source if reasoning else "",
                    prompt_partition=partition,
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output(
                        raw_response,
                        parsed_schema=model_type.__name__,
                        mode=self.tracer.capture_payloads,
                    ),
                    usage_details=usage_details(usage),
                    cost_details=cost_details(
                        usage,
                        input_cost_per_1m=self.settings.llm_input_cost_per_1m,
                        output_cost_per_1m=self.settings.llm_output_cost_per_1m,
                        cached_input_cost_per_1m=self.settings.llm_cached_input_cost_per_1m,
                    ),
                    metadata={"runtime": record.runtime, **record.metrics(), **partition},
                )
                return parsed
            except Exception as exc:
                if hooks is not None and hasattr(hooks, "record_error"):
                    hooks.record_error(exc)
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                    runtime="openai_agents_sdk_direct",
                    prompt_partition=packet.debug_metadata(),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error=record.error, mode=self.tracer.capture_payloads),
                    metadata={
                        "schema": model_type.__name__,
                        "runtime": record.runtime,
                        **record.metrics(),
                        **record.prompt_partition,
                    },
                    level="ERROR",
                    status_message=record.error,
                )
                raise

    def _run_config(
        self,
        *,
        workflow_name: str,
        trace_metadata: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ):
        from app.runtime.agents_sdk import build_run_config

        return build_run_config(
            self.settings,
            workflow_name=workflow_name,
            trace_metadata=trace_metadata,
            timeout_seconds=timeout_seconds,
        )

    def _temperature(self, model_name: str | None = None) -> float:
        model = (model_name or self.settings.llm_model).lower()
        if model == "kimi-k2.5" and (self.settings.llm_thinking_type or "").lower() == "disabled":
            return 0.6
        if model.startswith("kimi-k2"):
            return 1.0
        return self.settings.llm_temperature


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _model_extra_body(
    model_name: str,
    thinking_type: str | None,
    base_url: str = "",
) -> dict[str, Any] | None:
    return model_extra_body_for_thinking(model_name, thinking_type, base_url)


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return {}
    if isinstance(value, dict):
        return value
    return {}
