"""Reported zero, missing usage, and partial totals are distinct observations."""
from types import SimpleNamespace

import pytest
from agents.usage import Usage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from app.runtime.context_partition import usage_from_result


def result(*usages):
    return SimpleNamespace(raw_responses=[SimpleNamespace(usage=usage) for usage in usages])


def test_explicit_sdk_zero_is_retained():
    usage = Usage(
        requests=1, input_tokens=0, output_tokens=0, total_tokens=0,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    assert usage_from_result(result(usage)) == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0, "reasoning_tokens": 0,
    }


@pytest.mark.parametrize("usage", [None, {}, {
    "input_tokens": None, "output_tokens": None, "total_tokens": None,
    "input_tokens_details": {"cached_tokens": None},
    "output_tokens_details": {"reasoning_tokens": None},
}])
def test_missing_usage_does_not_create_zero(usage):
    assert usage_from_result(result(usage)) == {}
    assert usage_from_result(result()) == {}
    assert usage_from_result(result({"input_tokens": 12})) == {"prompt_tokens": 12}


@pytest.mark.parametrize("second", [{
    "prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6,
    "prompt_tokens_details": {"cached_tokens": 2},
}, None])
def test_multiple_responses_sum_known_values_and_mark_partial_metrics(second):
    first = {
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    observed = usage_from_result(result(first, second))
    if second is None:
        assert observed == {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
            "cached_tokens": 0, "reasoning_tokens": 0,
            "partial_metrics": ["cached_tokens", "completion_tokens", "prompt_tokens", "reasoning_tokens", "total_tokens"],
        }
    else:
        assert observed == {
            "prompt_tokens": 15, "completion_tokens": 3, "total_tokens": 18,
            "cached_tokens": 2, "reasoning_tokens": 0,
            "partial_metrics": ["reasoning_tokens"],
        }
