"""Contratos de `harness.cursor_openai` — normalize + reasoning remap (sem LLM)."""

from __future__ import annotations

import json

from harness import cursor_openai as co


def test_is_bonsai_model_aliases() -> None:
    assert co.is_bonsai_model("bonsai")
    assert co.is_bonsai_model("BONSAI")
    assert co.is_bonsai_model("openai:bonsai")
    assert co.is_bonsai_model("prism-ml/bonsai-27b")
    assert not co.is_bonsai_model("harness")
    assert not co.is_bonsai_model(None)
    assert not co.is_bonsai_model("")


def test_normalize_responses_body_to_messages() -> None:
    body = co.normalize_chat_body(
        {
            "model": "bonsai",
            "input": [{"role": "user", "content": "ola", "type": "message"}],
            "tools": [
                {
                    "type": "function",
                    "name": "Shell",
                    "description": "run",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "type": "custom",
                    "name": "ApplyPatch",
                    "format": {"type": "grammar", "syntax": "x"},
                },
            ],
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "stream_options": {"include_usage": True},
            "reasoning": {},
        }
    )
    assert "input" not in body
    assert body["messages"][0]["content"] == "ola"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "Shell"
    assert body["tools"][1]["type"] == "custom"
    assert body["tools"][1]["name"] == "ApplyPatch"
    assert "store" not in body
    assert "stream_options" not in body
    assert body["model"] == "default"


def test_normalize_keeps_nested_function_tools() -> None:
    nested = {
        "type": "function",
        "function": {"name": "TodoWrite", "description": "", "parameters": {}},
    }
    body = co.normalize_chat_body({"model": "bonsai", "messages": [], "tools": [nested]})
    assert body["tools"][0] == nested


def test_remap_completion_reasoning_field() -> None:
    data = {
        "id": "x",
        "object": "chat.completion",
        "model": "default",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "oi",
                    "reasoning": "penso logo existo",
                },
                "finish_reason": "stop",
            }
        ],
    }
    out = co.remap_completion_response(data, echo_model="bonsai")
    assert out["model"] == "bonsai"
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "penso logo existo"
    assert "reasoning" not in msg
    assert msg["content"] == "oi"


def test_remap_sse_reasoning_to_reasoning_content() -> None:
    chunk = {
        "id": "c",
        "object": "chat.completion.chunk",
        "model": "default",
        "choices": [{"index": 0, "delta": {"reasoning": "hmm"}, "finish_reason": None}],
    }
    out = co.remap_sse_data_payload(chunk, echo_model="bonsai")
    assert out["model"] == "bonsai"
    assert out["choices"][0]["delta"]["reasoning_content"] == "hmm"
    assert "reasoning" not in out["choices"][0]["delta"]


def test_iter_remapped_sse_lines_done_and_json() -> None:
    lines = [
        b'data: {"choices":[{"delta":{"reasoning":"a"},"index":0}]}',
        b"data: [DONE]",
    ]
    frames = list(co.iter_remapped_sse_lines(lines, echo_model="bonsai"))
    assert frames[-1] == b"data: [DONE]\n\n"
    first = json.loads(frames[0][len(b"data: ") : -2])
    assert first["choices"][0]["delta"]["reasoning_content"] == "a"
    assert first["model"] == "bonsai"
