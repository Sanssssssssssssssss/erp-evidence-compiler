from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_REASONING_EXCERPT_CHARS = 8000
REASONING_FIELDS = ("reasoning", "reasoning_content", "thinking")


@dataclass(frozen=True)
class ReasoningCapture:
    text: str
    full_text: str
    chars: int
    chunks: int
    source: str

    def model_payload(
        self,
        *,
        role: str,
        model: str,
        thinking_type: str = "",
        status: str = "completed",
    ) -> dict[str, Any]:
        return {
            "role": role,
            "model": model,
            "reasoning_excerpt": self.text,
            "reasoning_chars": self.chars,
            "reasoning_chunks": self.chunks,
            "thinking_type": thinking_type,
            "reasoning_source": self.source,
            "status": status,
        }


def extract_stream_reasoning_delta(data: Any) -> str:
    event_type = _str_field(data, "type")
    chunks: list[str] = []

    for field in REASONING_FIELDS:
        value = _get(data, field)
        if isinstance(value, str) and value:
            chunks.append(value)

    delta = _get(data, "delta")
    if "reasoning" in event_type or "thinking" in event_type:
        if isinstance(delta, str) and delta:
            chunks.append(delta)
        else:
            chunks.extend(_reasoning_texts_from_value(delta))
    else:
        chunks.extend(_reasoning_texts_from_value(delta))

    choices = _get(data, "choices")
    if isinstance(choices, list):
        for choice in choices:
            chunks.extend(_reasoning_texts_from_value(_get(choice, "delta")))

    return "".join(_unique_nonempty(chunks))


def extract_reasoning_from_stream_item(item: Any) -> ReasoningCapture | None:
    raw_item = _get(item, "raw_item") or item
    item_type = _str_field(raw_item, "type") or _str_field(item, "type")
    if item_type and item_type != "reasoning" and "reasoning" not in item_type:
        return None
    return _capture_from_texts(_reasoning_item_texts(raw_item), source="stream_item")


def extract_reasoning_from_result(
    result: Any,
    *,
    final_output: str = "",
) -> ReasoningCapture | None:
    captures: list[ReasoningCapture] = []

    for item in _as_list(_get(result, "new_items")):
        capture = extract_reasoning_from_stream_item(item)
        if capture:
            captures.append(capture)

    for response in _as_list(_get(result, "raw_responses")):
        captures.extend(_captures_from_response(response))

    if not captures:
        return None

    unique_captures: dict[str, ReasoningCapture] = {}
    for capture in captures:
        prior = unique_captures.get(capture.full_text)
        if capture.full_text and (prior is None or capture.chunks > prior.chunks):
            unique_captures[capture.full_text] = capture
    if not unique_captures:
        return None
    full_text = "\n".join(unique_captures)
    if final_output and _same_json_value(full_text, final_output):
        return None
    return ReasoningCapture(
        text=_clip(full_text),
        full_text=full_text,
        chars=len(full_text),
        chunks=sum(capture.chunks for capture in unique_captures.values()),
        source="result",
    )


def append_reasoning_delta(bucket: dict[str, Any], role: str, delta: str) -> dict[str, Any]:
    current = bucket.get(role)
    if not isinstance(current, dict):
        current = {"text": "", "chunks": 0, "total_chars": 0}
    full_text = f"{current.get('text', '')}{delta}"
    total_chars = int(current.get("total_chars") or len(str(current.get("text") or ""))) + len(delta)
    chunks = int(current.get("chunks") or 0) + 1
    current = {
        "text": _clip(full_text),
        "chunks": chunks,
        "chars": total_chars,
        "total_chars": total_chars,
    }
    bucket[role] = current
    return current


def _captures_from_response(response: Any) -> list[ReasoningCapture]:
    captures: list[ReasoningCapture] = []
    for item in _as_list(_get(response, "output")):
        capture = extract_reasoning_from_stream_item(item)
        if capture:
            captures.append(capture)

    for choice in _as_list(_get(response, "choices")):
        message = _get(choice, "message")
        texts = _reasoning_texts_from_value(message)
        capture = _capture_from_texts(texts, source="raw_response_choice")
        if capture:
            captures.append(capture)
    return captures


def _reasoning_item_texts(item: Any) -> list[str]:
    texts: list[str] = []
    texts.extend(_reasoning_texts_from_value(item))
    for part in _as_list(_get(item, "summary")):
        value = _get(part, "text")
        if isinstance(value, str):
            texts.append(value)
    for part in _as_list(_get(item, "content")):
        value = _get(part, "text")
        if isinstance(value, str):
            texts.append(value)
    return _unique_nonempty(texts)


def _reasoning_texts_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    texts: list[str] = []
    for field in REASONING_FIELDS:
        candidate = _get(value, field)
        if isinstance(candidate, str):
            texts.append(candidate)
    return _unique_nonempty(texts)


def _capture_from_texts(texts: list[str], *, source: str) -> ReasoningCapture | None:
    unique = _unique_nonempty(texts)
    if not unique:
        return None
    full_text = "\n".join(unique)
    return ReasoningCapture(
        text=_clip(full_text),
        full_text=full_text,
        chars=len(full_text),
        chunks=len(unique),
        source=source,
    )


def _get(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _str_field(value: Any, key: str) -> str:
    candidate = _get(value, key)
    return str(candidate or "")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clip(value: str) -> str:
    text = str(value or "")
    if len(text) <= MAX_REASONING_EXCERPT_CHARS:
        return text
    return text[-MAX_REASONING_EXCERPT_CHARS:]


def _same_json_value(left: str, right: str) -> bool:
    try:
        return json.loads(left) == json.loads(right)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
