from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def planner_thinking_type(configured: str | None) -> str:
    return _normalized_thinking_type(configured)


def manager_tool_loop_thinking_type(model_name: str, configured: str | None) -> str:
    if _requires_reasoning_content_tool_history(model_name):
        return "disabled"
    return planner_thinking_type(configured)


def role_thinking_type(role: str, payload: dict[str, Any] | None, configured: str | None) -> str:
    _ = payload
    if role == "fine_verifier":
        return "high"
    return _normalized_thinking_type(configured)


def temperature_for_thinking(model_name: str, default: float, thinking_type: str | None) -> float:
    model = str(model_name or "").lower()
    if model == "kimi-k2.5" and str(thinking_type or "").lower() == "disabled":
        return 0.6
    if model.startswith("kimi-k2"):
        return 1.0
    return default


def model_extra_body_for_thinking(
    model_name: str,
    thinking_type: str | None,
    base_url: str = "",
) -> dict[str, Any] | None:
    model = str(model_name or "").strip().lower().rsplit("/", 1)[-1]
    if model == "kimi-k2.5":
        return {"thinking": {"type": _normalized_thinking_type(thinking_type)}}
    if model.startswith("deepseek-v4-"):
        mode = _normalized_thinking_type(thinking_type)
        effort = "none" if mode in {"disabled", "none"} else mode if mode in {"low", "high", "max"} else "high"
        if urlsplit(str(base_url or "").strip()).hostname == "api.commandcode.ai":
            # CommandCode routes this model through Chat Completions, not Responses.
            if effort == "none":
                return {"thinking": {"type": "disabled"}}
            return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
        if "developer.amd.com.cn/radeon/api" in str(base_url or "").strip().lower():
            return {"chat_template_kwargs": {"thinking": mode not in {"disabled", "none"}}}
        return {"reasoning": {"effort": effort}}
    return None


def _normalized_thinking_type(value: str | None) -> str:
    return str(value or "disabled").strip().lower() or "disabled"


def _requires_reasoning_content_tool_history(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model == "kimi-k2.5"
