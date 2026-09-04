"""Offline extraction controls; raw provider items must remain unchanged."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseReasoningItem

from app.runtime.reasoning_capture import extract_reasoning_from_result


def test_typed_reasoning_and_final_message_remain_separate():
    reasoning = "Original reasoning.\n逐字保留。"
    final = '{"status":"CONTRADICTED"}'
    items = [
        ResponseReasoningItem(id="reasoning", type="reasoning", summary=[
            {"type": "summary_text", "text": reasoning},
        ]),
        ResponseOutputMessage(id="answer", type="message", role="assistant", status="completed", content=[
            {"type": "output_text", "text": final, "annotations": []},
        ]),
    ]
    before = [item.model_dump() for item in items]
    result = SimpleNamespace(new_items=[], raw_responses=[{"output": items}])
    capture = extract_reasoning_from_result(result, final_output=final)
    assert capture.full_text == reasoning
    assert capture.chars == len(reasoning) and capture.chunks == 1
    assert [item.model_dump() for item in items] == before


def test_typed_final_message_alone_is_not_reasoning():
    result = SimpleNamespace(new_items=[], raw_responses=[{"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Final answer."}]},
    ]}])
    assert extract_reasoning_from_result(result) is None


@pytest.mark.parametrize("response", [
    {"output": [{"reasoning_content": "Legacy reasoning."}]},
    {"output": [{"summary": [{"text": "Legacy reasoning."}]}]},
    {"choices": [{"message": {"reasoning_content": "Legacy reasoning.", "content": "Final answer."}}]},
])
def test_untyped_legacy_reasoning_is_still_supported(response):
    before = deepcopy(response)
    capture = extract_reasoning_from_result(SimpleNamespace(new_items=[], raw_responses=[response]))
    assert capture.full_text == "Legacy reasoning."
    assert response == before
