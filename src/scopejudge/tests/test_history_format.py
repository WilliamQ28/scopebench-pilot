"""The compact judge-history format keeps every content field and drops only bookkeeping."""

import contextlib
import json
from types import SimpleNamespace
from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agent_harness import HarnessDeps
from scopejudge.capability import ScopeJudge, _compact_history
from scopejudge.protocol import JudgeHistoryFormat

_INSTRUCTIONS = "Use `bash` for shell commands."


def _history() -> list[ModelMessage]:
    usage = RequestUsage(input_tokens=16_732, output_tokens=322)
    return [
        ModelRequest(
            parts=[SystemPromptPart("SYSTEM_TEXT"), UserPromptPart("ENGAGEMENT_TEXT")],
            instructions=_INSTRUCTIONS,
        ),
        ModelResponse(
            parts=[
                ThinkingPart("REASONING_TEXT", id="reasoning_content", provider_name="openai"),
                TextPart("PROSE_TEXT"),
                ToolCallPart("bash", '{"command": "curl \\"http://x\\""}', tool_call_id="c1"),
            ],
            usage=usage,
            model_name="qwen35-122b",
            provider_name="openai",
            provider_url="https://endpoint.test/v1/",
            provider_response_id="chatcmpl-123",
            finish_reason="tool_call",
        ),
        ModelRequest(
            parts=[ToolReturnPart("bash", "RESULT_TEXT", tool_call_id="c1")],
            instructions=_INSTRUCTIONS,
        ),
        ModelResponse(
            parts=[ToolCallPart("read", '{"file_path": "/tmp/a"', tool_call_id="c2")],
            usage=usage,
            finish_reason="length",
        ),
        ModelRequest(
            parts=[RetryPromptPart("RETRY_TEXT", tool_name="read", tool_call_id="c2")],
            instructions="CHANGED_INSTRUCTIONS",
        ),
    ]


def _raw(messages: list[ModelMessage]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", json.loads(ModelMessagesTypeAdapter.dump_json(messages)))


def _content_sequence(history: list[dict[str, object]], *, decoded: bool) -> list[object]:
    """Every actor-visible content value, in order (args decoded when the format decodes them)."""
    values: list[object] = []
    for message in history:
        for part in cast("list[dict[str, object]]", message["parts"]):
            for key in ("content", "tool_name", "tool_call_id", "args"):
                value = part.get(key)
                if key == "args" and decoded and isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        value = cast("object", json.loads(value))
                if value is not None:
                    values.append(value)
    return values


def test_compact_history_preserves_every_content_field_in_order() -> None:
    raw = _raw(_history())
    compact = cast("list[dict[str, object]]", _compact_history(raw))
    assert len(compact) == len(raw)
    assert [m["kind"] for m in compact] == [m["kind"] for m in raw]
    assert _content_sequence(compact, decoded=False) == _content_sequence(raw, decoded=True)


def test_compact_history_drops_bookkeeping_and_shrinks_the_payload() -> None:
    raw = _raw(_history())
    compact = _compact_history(raw)
    text = json.dumps(compact)
    for leaked in ("16732", "qwen35-122b", "endpoint.test", "chatcmpl-123", "timestamp", "run_id"):
        assert leaked not in text
    assert len(text) < len(json.dumps(raw)) // 2


def test_compact_history_labels_parts_and_decodes_args() -> None:
    compact = cast("list[dict[str, object]]", _compact_history(_raw(_history())))
    response = cast("list[dict[str, object]]", compact[1]["parts"])
    assert [p["type"] for p in response] == ["reasoning", "text", "call"]
    assert response[2]["args"] == {"command": 'curl "http://x"'}
    # Undecodable (truncated) args are kept verbatim rather than dropped.
    truncated = cast("list[dict[str, object]]", compact[3]["parts"])[0]
    assert truncated["args"] == '{"file_path": "/tmp/a"'


def test_compact_history_keeps_unusual_signals_and_dedupes_instructions() -> None:
    compact = cast("list[dict[str, object]]", _compact_history(_raw(_history())))
    assert "finish_reason" not in compact[1]  # routine tool_call finish restates the default
    assert compact[3]["finish_reason"] == "length"  # a truncated response is evidence
    instructions = [m.get("instructions") for m in compact]
    assert instructions == [_INSTRUCTIONS, None, None, None, "CHANGED_INSTRUCTIONS"]
    failed = {"part_kind": "tool-return", "content": "x", "outcome": "failed", "timestamp": "t"}
    assert _compact_history([{"kind": "request", "parts": [failed]}]) == [
        {"kind": "request", "parts": [{"type": "result", "content": "x", "outcome": "failed"}]}
    ]


def test_compact_history_keeps_unknown_fields_and_non_lists() -> None:
    part = {"part_kind": "file", "content": {"url": "u"}, "novel_field": 1}
    expected_part = {"type": "file", "content": {"url": "u"}, "novel_field": 1}
    assert _compact_history([{"kind": "request", "parts": [part], "novel": "n"}]) == [
        {"kind": "request", "novel": "n", "parts": [expected_part]}
    ]
    assert _compact_history("not-a-list") == "not-a-list"
    # Shapes the serializer does not recognize pass through untouched rather than being dropped.
    assert _compact_history(["opaque", {"kind": "request", "parts": ["opaque-part"]}]) == [
        "opaque",
        {"kind": "request", "parts": ["opaque-part"]},
    ]


def _payload(history: list[ModelMessage], history_format: JudgeHistoryFormat) -> dict[str, object]:
    gate = ScopeJudge(
        instruction="ENGAGEMENT_TEXT", model=TestModel(), history_format=history_format
    )
    ctx = cast("RunContext[HarnessDeps]", cast("object", SimpleNamespace(messages=history)))
    call = ToolCallPart("bash", "{}", tool_call_id="c3")
    return gate.information_payload(ctx, call, cast("ValidatedToolArgs", {}))


def test_raw_default_is_the_unmodified_message_dump() -> None:
    # The default must stay byte-identical to what frozen baselines sent the judge.
    history = _history()
    assert _payload(history, "raw")["trajectory"] == _raw(history)


def test_compact_format_reaches_the_judge_payload() -> None:
    history = _history()
    payload = _payload(history, "compact")
    assert payload["trajectory"] == _compact_history(_raw(history))
    assert payload["engagement_instruction"] == "ENGAGEMENT_TEXT"


def test_explicit_payload_filters_hidden_reasoning() -> None:
    gate = ScopeJudge(
        instruction="ENGAGEMENT_TEXT", model=TestModel(), hide_actor_reasoning=True
    )
    ctx = cast("RunContext[HarnessDeps]", cast("object", SimpleNamespace(messages=_history())))
    payload = gate.information_payload(
        ctx, ToolCallPart("bash", "{}", tool_call_id="c3"), cast("ValidatedToolArgs", {})
    )
    raw = json.dumps(payload["trajectory"])
    assert '"part_kind": "thinking"' not in raw
    assert '"part_kind": "text"' in raw
