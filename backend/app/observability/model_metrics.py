from __future__ import annotations

import time
from typing import Any


MODEL_METRIC_KEYS = (
    "latency_ms",
    "ttft_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_hit_ratio",
    "prompt_cache_key",
)


def build_model_metrics(
    *,
    usage: dict[str, Any] | None = None,
    prompt_partition: dict[str, Any] | None = None,
    latency_ms: float | int | None = None,
    ttft_ms: float | int | None = None,
) -> dict[str, Any]:
    usage = usage or {}
    partition = prompt_partition or {}
    prompt_tokens = _int_or_none(partition.get("prompt_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _int_or_none(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = _int_or_none(usage.get("completion_tokens") or usage.get("output_tokens"))
    total_tokens = _int_or_none(usage.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    cached_tokens = _int_or_none(partition.get("cached_tokens"))
    if cached_tokens is None:
        cached_tokens = _int_or_none(usage.get("cached_tokens"))
    cache_hit_ratio = _float_or_none(partition.get("cache_hit_ratio"))
    if cache_hit_ratio is None and prompt_tokens and cached_tokens is not None:
        cache_hit_ratio = round(cached_tokens / prompt_tokens, 4)
    return {
        "latency_ms": _float_or_none(latency_ms),
        "ttft_ms": _float_or_none(ttft_ms),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_ratio": cache_hit_ratio,
        "prompt_cache_key": _str_or_none(partition.get("prompt_cache_key")),
    }


def summarize_model_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [_metric_row(call) for call in calls]
    rows = [row for row in rows if row]
    prompt_tokens = sum(int(row.get("prompt_tokens") or 0) for row in rows)
    cached_tokens = sum(int(row.get("cached_tokens") or 0) for row in rows)
    return {
        "call_count": len(rows),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in rows),
        "cached_tokens": cached_tokens,
        "cache_hit_ratio": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else None,
        "calls": rows,
    }


def trace_duration_ms(payload: dict[str, Any], raw: dict[str, Any]) -> int | None:
    return _int_or_none(payload.get("latency_ms") or payload.get("duration_ms") or raw.get("duration_ms"))


def trace_token_count(payload: dict[str, Any], raw: dict[str, Any]) -> int | None:
    value = payload.get("total_tokens") or payload.get("tokens") or raw.get("token_count")
    if value is not None:
        return _int_or_none(value)
    prompt_tokens = _int_or_none(payload.get("prompt_tokens"))
    completion_tokens = _int_or_none(payload.get("completion_tokens"))
    if prompt_tokens is not None and completion_tokens is not None:
        return prompt_tokens + completion_tokens
    return None


def mark_model_started(observability: dict[str, Any], role: str, *, now: float | None = None) -> None:
    role = str(role or "model")
    timings = observability.setdefault("_model_timing", {})
    if not isinstance(timings, dict):
        timings = {}
        observability["_model_timing"] = timings
    timings[role] = {"started_perf": now if now is not None else time.perf_counter()}


def mark_first_model_output(observability: dict[str, Any], role: str, *, now: float | None = None) -> None:
    role = str(role or "model")
    timings = observability.get("_model_timing")
    if not isinstance(timings, dict):
        return
    timing = timings.get(role)
    if not isinstance(timing, dict) or timing.get("ttft_ms") is not None:
        return
    started = _float_or_none(timing.get("started_perf"))
    if started is None:
        return
    current = now if now is not None else time.perf_counter()
    timing["first_output_perf"] = current
    timing["ttft_ms"] = round(max(0.0, current - started) * 1000, 2)


def model_ttft_ms(observability: dict[str, Any], role: str) -> float | None:
    timings = observability.get("_model_timing")
    if not isinstance(timings, dict):
        return None
    timing = timings.get(str(role or "model"))
    if not isinstance(timing, dict):
        return None
    return _float_or_none(timing.get("ttft_ms"))


def _metric_row(call: dict[str, Any]) -> dict[str, Any]:
    row = {
        "role": call.get("role"),
        "model": call.get("model"),
        "prompt_version": call.get("prompt_version"),
    }
    for key in MODEL_METRIC_KEYS:
        value = call.get(key)
        if value is not None:
            row[key] = value
    return {key: value for key, value in row.items() if value not in (None, "")}


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
