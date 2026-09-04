"""Synthetic HTTP responses through the real SDK converter; no provider access."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agents import ModelSettings
from agents.models.interface import ModelTracing
from openai import AsyncOpenAI

from app.config import Settings
from app.runtime import agents_sdk


COMMANDCODE = "https://api.commandcode.ai/provider/v1"


@pytest.mark.parametrize("extra, expected", [
    ({"reasoning": "Original reasoning.\n逐字保留。"}, ["Original reasoning.\n逐字保留。"]),
    ({"reasoning": "alias", "reasoning_content": None}, ["alias"]),
    ({"reasoning": "alias", "reasoning_content": "canonical"}, ["canonical"]),
    ({"reasoning": "alias", "reasoning_content": ""}, []),
    ({"reasoning": ""}, []),
    ({"reasoning": {"text": "not a string"}}, []),
    ({}, []),
])
def test_native_converter_retains_only_actual_string_reasoning(extra, expected):
    output, requests = asyncio.run(_response(extra, COMMANDCODE))
    texts = [part.text for item in output.output if item.type == "reasoning" for part in item.summary]
    assert texts == expected
    assert [part.text for item in output.output if item.type == "message" for part in item.content] == ['{"ok":true}']
    assert (output.usage.input_tokens, output.usage.output_tokens, output.usage.total_tokens) == (93, 26, 119)
    assert output.usage.output_tokens_details.reasoning_tokens == 20
    assert len(requests) == 1
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["messages"] == [{"role": "user", "content": "Synthetic input"}]
    assert "reasoning_content" not in requests[0]


async def _response(extra, base_url):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic-response", "object": "chat.completion", "created": 0,
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"ok":true}', **extra,
            }}],
            "usage": {"prompt_tokens": 93, "completion_tokens": 26, "total_tokens": 119,
                      "prompt_tokens_details": {"cached_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 20}},
        })

    async with AsyncOpenAI(
        api_key="offline-test-only", base_url=base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = agents_sdk._ReasoningReplayProvider(openai_client=client, use_responses=False).get_model("deepseek/deepseek-v4-flash")
        output = await model.get_response(
            system_instructions=None, input="Synthetic input",
            model_settings=ModelSettings(extra_body={"thinking": {"type": "disabled"}}),
            tools=[], output_schema=None, handoffs=[], tracing=ModelTracing.DISABLED,
        )
    return output, requests


@pytest.mark.parametrize("base_url", [
    "https://api.commandcode.ai.evil.invalid/provider/v1",
    "https://evil.invalid/api.commandcode.ai/provider/v1",
    "https://api.deepseek.com",
])
def test_alias_does_not_change_other_hosts(base_url):
    output, _requests = asyncio.run(_response({"reasoning": "provider extra"}, base_url))
    assert not any(item.type == "reasoning" for item in output.output)


def test_tool_call_survives_reasoning_alias():
    output, _requests = asyncio.run(_response({
        "reasoning": "Read the supplied record.",
        "tool_calls": [{"id": "synthetic-tool", "type": "function", "function": {
            "name": "read_record", "arguments": '{"id":"R9"}',
        }}],
    }, COMMANDCODE))
    calls = [item for item in output.output if item.type == "function_call"]
    assert [(item.call_id, item.name, item.arguments) for item in calls] == [
        ("synthetic-tool", "read_record", '{"id":"R9"}'),
    ]


def test_stream_tuple_is_returned_unchanged(monkeypatch):
    response = (object(), object())

    async def fetch(*_args, **_kwargs):
        return response

    monkeypatch.setattr(agents_sdk.OpenAIChatCompletionsModel, "_fetch_response", fetch)
    model = agents_sdk._ReasoningReplayChatCompletionsModel(
        model="deepseek/deepseek-v4-flash", openai_client=SimpleNamespace(base_url=COMMANDCODE),
    )
    assert asyncio.run(model._fetch_response(None, "Synthetic input", stream=True)) is response


@pytest.mark.parametrize("base_url, replay, selected", [
    (COMMANDCODE, False, True),
    ("https://API.COMMANDCODE.AI:443/provider/v1", False, True),
    ("https://api.commandcode.ai.evil.invalid/provider/v1", False, False),
    ("https://other.invalid/v1", False, False),
    ("https://other.invalid/v1", True, True),
])
def test_existing_provider_is_selected_for_commandcode_without_tools(monkeypatch, base_url, replay, selected):
    monkeypatch.setattr(agents_sdk, "_client_for", lambda *_args, **_kwargs: (SimpleNamespace(base_url=base_url), False))
    settings = Settings(_env_file=None, llm_provider="compatible", llm_api_key="offline-test-only", llm_base_url=base_url)
    config = agents_sdk.build_run_config(settings, workflow_name="offline", replay_streamed_reasoning=replay)
    assert isinstance(config.model_provider, agents_sdk._ReasoningReplayProvider) is selected
