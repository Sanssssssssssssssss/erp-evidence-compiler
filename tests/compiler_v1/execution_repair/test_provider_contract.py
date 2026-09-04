"""Offline request-body contract; never contacts a provider."""
from __future__ import annotations

import pytest

from app.agents.thinking import model_extra_body_for_thinking


COMMANDCODE = "https://api.commandcode.ai/provider/v1"


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek/deepseek-v4-flash"])
@pytest.mark.parametrize("mode", ["low", "high", "max"])
def test_commandcode_chat_uses_native_thinking_and_effort(model, mode):
    assert model_extra_body_for_thinking(model, mode, COMMANDCODE) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": mode,
    }


@pytest.mark.parametrize("mode", ["disabled", "none", None])
def test_commandcode_disabled_does_not_send_responses_reasoning(mode):
    assert model_extra_body_for_thinking("deepseek/deepseek-v4-pro", mode, COMMANDCODE) == {
        "thinking": {"type": "disabled"},
    }


def test_commandcode_hostname_is_case_insensitive_and_accepts_explicit_port():
    assert model_extra_body_for_thinking(
        " DeepSeek/DeepSeek-V4-Flash ", " HIGH ",
        "https://API.COMMANDCODE.AI:443/provider/v1",
    ) == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


@pytest.mark.parametrize("base_url", [
    "https://api.commandcode.ai.evil.invalid/provider/v1",
    "https://evil.invalid/api.commandcode.ai/provider/v1",
    "https://api.commandcode.ai@evil.invalid/provider/v1",
    "https://evil.invalid/?upstream=https://api.commandcode.ai",
    "https://api.deepseek.com",
    "https://api.deepseek.com/v1/responses",
    "",
])
def test_non_commandcode_host_preserves_existing_responses_mapping(base_url):
    assert model_extra_body_for_thinking("deepseek-v4-flash", "low", base_url) == {
        "reasoning": {"effort": "low"},
    }
    assert model_extra_body_for_thinking("deepseek-v4-flash", "disabled", base_url) == {
        "reasoning": {"effort": "none"},
    }


@pytest.mark.parametrize("mode, enabled", [("low", True), ("disabled", False)])
def test_amd_branch_is_unchanged(mode, enabled):
    assert model_extra_body_for_thinking(
        "deepseek-v4-flash", mode, "https://developer.amd.com.cn/radeon/api/v1",
    ) == {"chat_template_kwargs": {"thinking": enabled}}


@pytest.mark.parametrize("model, expected", [
    ("other-model", None),
    ("deepseek-v3", None),
    ("kimi-k2.5", {"thinking": {"type": "disabled"}}),
])
def test_other_model_contract_is_unchanged(model, expected):
    assert model_extra_body_for_thinking(model, "disabled", COMMANDCODE) == expected
