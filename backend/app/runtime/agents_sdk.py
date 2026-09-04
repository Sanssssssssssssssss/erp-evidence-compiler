from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from agents import AgentOutputSchema, RunConfig, Runner
from agents.exceptions import RunErrorDetails
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from agents.models.reasoning_content_replay import (
    ReasoningContentReplayContext,
    default_should_replay_reasoning_content,
)
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.config import Settings


_SHARED_CLIENTS_ENABLED = False
_SHARED_CLIENTS: dict[tuple[str, str, float | None], AsyncOpenAI] = {}


class FencedJsonOutputSchema(AgentOutputSchema):
    def validate_json(self, json_str: str) -> Any:
        lines = json_str.strip().splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            json_str = "\n".join(lines).strip()
        return super().validate_json(json_str)


def build_run_config(
    settings: Settings,
    *,
    workflow_name: str,
    trace_metadata: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    disable_timeout: bool = False,
    replay_streamed_reasoning: bool = False,
) -> RunConfig:
    base_url = settings.llm_base_url or "https://api.openai.com/v1"
    timeout = None if disable_timeout else float(
        timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds
    )
    client, should_close = _client_for(settings, base_url=base_url, timeout_seconds=timeout)
    adapt_reasoning = replay_streamed_reasoning or urlsplit(base_url).hostname == "api.commandcode.ai"
    config = RunConfig(
        model_provider=(_ReasoningReplayProvider if adapt_reasoning else OpenAIProvider)(
            openai_client=client,
            use_responses=_use_responses_api(settings.llm_provider, base_url),
        ),
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        workflow_name=workflow_name,
        trace_metadata=trace_metadata or {},
    )
    setattr(config, "_invoice_openai_client", client)
    setattr(config, "_invoice_close_openai_client", should_close)
    return config


class _ReasoningReplayProvider(OpenAIProvider):
    def get_model(self, model_name: str | None) -> Any:
        model = super().get_model(model_name)
        if isinstance(model, OpenAIChatCompletionsModel):
            return _ReasoningReplayChatCompletionsModel(
                model=model.model,
                openai_client=model._get_client(),
                should_replay_reasoning_content=_replay_streamed_deepseek_reasoning,
            )
        return model


class _ReasoningReplayChatCompletionsModel(OpenAIChatCompletionsModel):
    async def _fetch_response(
        self,
        system_instructions: Any,
        input: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(input, list):
            input = _without_empty_tool_call_messages(input)
        response = await super()._fetch_response(system_instructions, input, *args, **kwargs)
        if isinstance(response, ChatCompletion) and urlsplit(str(self._get_client().base_url)).hostname == "api.commandcode.ai":
            for choice in response.choices:
                message = choice.message
                reasoning = getattr(message, "reasoning", None)
                if getattr(message, "reasoning_content", None) is None and isinstance(reasoning, str):
                    # Preserve the provider's exact text for the SDK's standard converter.
                    message.reasoning_content = reasoning
        return response


def _replay_streamed_deepseek_reasoning(context: ReasoningContentReplayContext) -> bool:
    if not default_should_replay_reasoning_content(context):
        return False
    item = context.reasoning.item
    if isinstance(item, dict) and not item.get("summary"):
        texts = [
            str(part.get("text") or "")
            for part in item.get("content", [])
            if isinstance(part, dict) and part.get("type") == "reasoning_text" and part.get("text")
        ]
        if texts:
            item["summary"] = [{"type": "summary_text", "text": "\n".join(texts)}]
    return bool(isinstance(item, dict) and item.get("summary"))


def _without_empty_tool_call_messages(items: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    for index, item in enumerate(items):
        previous = items[index - 1] if index else None
        following = items[index + 1] if index + 1 < len(items) else None
        content = item.get("content") if isinstance(item, dict) else None
        empty_assistant = (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
            and isinstance(content, list)
            and all(not str(part.get("text") or "") for part in content if isinstance(part, dict))
        )
        between_tool_call_and_output = (
            isinstance(previous, dict)
            and previous.get("type") == "function_call"
            and isinstance(following, dict)
            and following.get("type") == "function_call_output"
        )
        if not (empty_assistant and between_tool_call_and_output):
            cleaned.append(item)
    return cleaned


def run_agent_sync(*args: Any, **kwargs: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_agent_and_close(*args, **kwargs))
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(_run_agent_and_close(*args, **kwargs))).result()


async def _run_agent_and_close(*args: Any, **kwargs: Any) -> Any:
    config = kwargs.get("run_config")
    stream_response = bool(kwargs.pop("stream_response", False))
    try:
        if not stream_response:
            return await Runner.run(*args, **kwargs)
        result = Runner.run_streamed(*args, **kwargs)
        try:
            async for _event in result.stream_events():
                pass
        except Exception as exc:
            # Transport errors are not AgentsException, but the streaming runner
            # still owns completed responses and tool items. Preserve, do not retry.
            if getattr(exc, "run_data", None) is None:
                exc.run_data = RunErrorDetails(
                    input=result.input, new_items=result.new_items,
                    raw_responses=result.raw_responses, last_agent=result.last_agent,
                    context_wrapper=result.context_wrapper,
                    input_guardrail_results=result.input_guardrail_results,
                    output_guardrail_results=result.output_guardrail_results,
                )
            raise
        return result
    finally:
        await close_run_config_client(config)


async def close_run_config_client(config: Any) -> None:
    if not getattr(config, "_invoice_close_openai_client", True):
        return
    client = getattr(config, "_invoice_openai_client", None)
    if client is not None:
        await _safe_close_openai_client(client)


def enable_shared_openai_clients() -> None:
    global _SHARED_CLIENTS_ENABLED
    _SHARED_CLIENTS_ENABLED = True


async def close_shared_openai_clients() -> None:
    global _SHARED_CLIENTS_ENABLED
    clients = list(_SHARED_CLIENTS.values())
    _SHARED_CLIENTS.clear()
    _SHARED_CLIENTS_ENABLED = False
    for client in clients:
        await _safe_close_openai_client(client)


def _client_for(settings: Settings, *, base_url: str, timeout_seconds: float | None) -> tuple[AsyncOpenAI, bool]:
    if not _SHARED_CLIENTS_ENABLED:
        return (
            AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=0,
            ),
            True,
        )
    key = (settings.llm_api_key or "", base_url.rstrip("/"), timeout_seconds)
    client = _SHARED_CLIENTS.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        _SHARED_CLIENTS[key] = client
    return client, False


def _use_responses_api(provider: str, base_url: str) -> bool:
    target = (str(provider or "").lower(), str(base_url or "").rstrip("/"))
    return target in {("openai", "https://api.openai.com/v1"), ("deepseek", "https://api.deepseek.com")}


async def _safe_close_openai_client(client: AsyncOpenAI) -> None:
    try:
        await client.close()
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise
