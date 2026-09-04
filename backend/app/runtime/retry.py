from __future__ import annotations

import re


_TRANSIENT_MARKERS = (
    "rate limit",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary unavailable",
    "connection reset",
    "connection aborted",
    "service unavailable",
    "stream ended before terminal chunk",
)

_NON_RETRY_MARKERS = (
    "modelbehaviorerror",
    "typeadapter",
    "validation error",
    "validationerror",
    "policy",
    "guard",
    "schema",
    "forbid",
)


def is_transient_llm_error(exc: BaseException) -> bool:
    return _is_transient(exc, include_types=("timeout", "connection", "rate"))


def is_transient_tool_error(exc: BaseException) -> bool:
    return _is_transient(exc, include_types=("timeout", "temporary", "ioerror", "oserror", "subprocess"))


def _is_transient(exc: BaseException, *, include_types: tuple[str, ...]) -> bool:
    name = type(exc).__name__.lower()
    text = f"{name}: {exc}".lower()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code in {429, 500, 502, 503, 504}:
        return True
    if any(marker in text for marker in _NON_RETRY_MARKERS):
        return False
    if any(marker in name for marker in include_types):
        return True
    return any(marker in text for marker in _TRANSIENT_MARKERS) or bool(
        re.search(r"\b(?:http(?: status)?|status code)\s*[:=]?\s*(?:429|500|502|503|504)\b", text)
    )
