import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def setup_function():
    server.session_store = server.SessionStore()


def _assistant_tool_call(call_id="call_1"):
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": "I need to inspect the project before answering.",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "shell",
                "arguments": "{\"cmd\":\"rg foo\"}",
            },
        }],
    }


def _base_body(input_items, previous_response_id=None):
    body = {
        "model": "gpt-5.5",
        "reasoning": {"effort": "high"},
        "input": input_items,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def test_previous_response_preserves_assistant_tool_reasoning():
    response_id = "resp_prev"
    assistant = _assistant_tool_call()
    server.session_store.set(response_id, [
        {"role": "user", "content": "check this"},
        assistant,
    ])

    chat_body, _, _ = server.translate_response_create_to_chat(_base_body([
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        }
    ], previous_response_id=response_id))

    assistant_messages = [
        msg for msg in chat_body["messages"]
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert assistant_messages == [assistant]
    assert assistant_messages[0]["reasoning_content"] == assistant["reasoning_content"]
    assert chat_body["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "result",
    }
    assert chat_body["thinking"] == {"type": "enabled"}


def test_duplicate_function_call_is_not_rebuilt_without_reasoning():
    response_id = "resp_prev"
    assistant = _assistant_tool_call()
    server.session_store.set(response_id, [
        {"role": "user", "content": "check this"},
        assistant,
    ])

    chat_body, _, _ = server.translate_response_create_to_chat(_base_body([
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "shell",
            "arguments": "{\"cmd\":\"rg foo\"}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        },
    ], previous_response_id=response_id))

    assistant_messages = [
        msg for msg in chat_body["messages"]
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert assistant_messages == [assistant]
    assert all(msg.get("reasoning_content") for msg in assistant_messages)


def test_tool_call_index_recovers_assistant_without_previous_response_id():
    assistant = _assistant_tool_call("call_indexed")
    server.session_store.set("resp_prev", [
        {"role": "user", "content": "check this"},
        assistant,
    ])

    chat_body, _, _ = server.translate_response_create_to_chat(_base_body([
        {
            "type": "function_call_output",
            "call_id": "call_indexed",
            "output": "result",
        }
    ]))

    assert chat_body["messages"][0] == assistant
    assert chat_body["messages"][1]["role"] == "tool"
    assert chat_body["messages"][1]["tool_call_id"] == "call_indexed"


def test_unknown_tool_call_fails_instead_of_disabling_thinking():
    with pytest.raises(server.ReasoningContextError) as excinfo:
        server.translate_response_create_to_chat(_base_body([
            {
                "type": "function_call",
                "call_id": "call_missing",
                "name": "shell",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_missing",
                "output": "result",
            },
        ]))

    assert excinfo.value.missing_call_ids == ["call_missing"]


def test_stream_state_assistant_keeps_reasoning_and_tool_calls():
    state = {}
    meta = {
        "response_id": "resp_stream",
        "created_at": 1,
        "original_model": "gpt-5.5",
    }

    server.translate_sse_chunk_to_ws_events({
        "choices": [{
            "delta": {
                "role": "assistant",
                "reasoning_content": "Need a file search.",
            },
            "finish_reason": None,
        }]
    }, state, meta)
    server.translate_sse_chunk_to_ws_events({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_stream",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{\"cmd\""},
                }]
            },
            "finish_reason": None,
        }]
    }, state, meta)
    server.translate_sse_chunk_to_ws_events({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "function": {"arguments": ":\"rg foo\"}"},
                }]
            },
            "finish_reason": "tool_calls",
        }]
    }, state, meta)

    assistant = server.build_assistant_from_state(state)
    assert assistant["reasoning_content"] == "Need a file search."
    assert assistant["tool_calls"] == [{
        "id": "call_stream",
        "type": "function",
        "function": {
            "name": "shell",
            "arguments": "{\"cmd\":\"rg foo\"}",
        },
    }]
