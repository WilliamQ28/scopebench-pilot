"""Hermetic tests for tool-result compaction."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)

from agent_harness import HarnessDeps
from scopejudge.compaction import (
    MIN_RESULT_BUDGET,
    TruncateToolResults,
    _safe_name,
    make_reasoning_trimmer,
    trim_reasoning,
)


def _cap(max_chars: int = 300, cache_dir: Path | None = None) -> TruncateToolResults:
    return TruncateToolResults(max_chars=max_chars, cache_dir=cache_dir)


def test_rejects_budget_below_minimum() -> None:
    with pytest.raises(ValueError, match=str(MIN_RESULT_BUDGET)):
        _ = _cap(max_chars=MIN_RESULT_BUDGET - 1)


def test_safe_name_sanitizes_and_falls_back() -> None:
    assert _safe_name("chatcmpl-tool.42_x") == "chatcmpl-tool.42_x"
    assert _safe_name("a/b c:d") == "a_b_c_d"
    assert _safe_name("///") == "___"  # unsafe chars map to underscores
    assert _safe_name("") == "result"  # an empty name collapses to the fallback


def test_small_result_is_unchanged() -> None:
    text = "a short result"
    assert _cap()._compact(text, "c1") == text


def test_flag_bearing_result_is_never_truncated() -> None:
    # Far over budget, but it carries the objective, so it must survive intact.
    text = "noise " * 200 + "FLAG{deadbeef}" + " trailing " * 200
    assert _cap(max_chars=MIN_RESULT_BUDGET)._compact(text, "c1") == text


def test_oversized_result_is_head_tail_excerpted(tmp_path: Path) -> None:
    cap = _cap(max_chars=300, cache_dir=tmp_path / "cache")
    text = "H" * 400 + "T" * 400  # 800 chars, well over the 300 budget
    out = cap._compact(text, "c1")

    assert len(out) < len(text)
    assert out.startswith("H" * 200)  # head = 300 * 2 // 3
    assert out.endswith("T" * 100)  # tail = 300 - 200
    assert "characters elided to save context" in out
    # The full output is cached verbatim for the agent to re-read, and pointed at from the marker.
    cached = (tmp_path / "cache" / "c1.txt").read_text(encoding="utf-8")
    assert cached == text
    assert str(tmp_path / "cache" / "c1.txt") in out


def test_identical_oversized_results_collapse_to_a_backreference(tmp_path: Path) -> None:
    cap = _cap(max_chars=300, cache_dir=tmp_path / "cache")
    text = "X" * 800
    first = cap._compact(text, "call-1")
    second = cap._compact(text, "call-2")

    assert "characters elided" in first  # the first occurrence is excerpted and cached
    assert second != first
    assert "identical to the" in second
    assert "call-1" in second  # the back-reference names the first occurrence


def test_excerpt_without_cache_dir_has_no_pointer() -> None:
    out = _cap(max_chars=300, cache_dir=None)._compact("Y" * 800, "c1")
    assert "characters elided to save context." in out
    assert "saved to" not in out  # no cache => no file pointer phrase


def test_cache_write_failure_is_swallowed(tmp_path: Path) -> None:
    # A cache dir whose parent is a regular file makes mkdir fail; compaction still succeeds.
    blocker = tmp_path / "afile"
    _ = blocker.write_text("x", encoding="utf-8")
    out = _cap(max_chars=300, cache_dir=blocker / "sub")._compact("Z" * 800, "c1")
    assert "characters elided to save context." in out
    assert "saved to" not in out


# wrap_tool_execute deletes ctx and tool_def immediately, so the tests pass harmless stand-ins.
# Casting from object (not None) avoids pyright's reportInvalidCast while keeping them unused.
_UNUSED: object = None


def _call(call_id: str = "c1") -> ToolCallPart:
    return ToolCallPart(tool_name="bash", args={"cmd": "echo hi"}, tool_call_id=call_id)


async def test_wrap_tool_execute_compacts_string_results(tmp_path: Path) -> None:
    cap = _cap(max_chars=300, cache_dir=tmp_path / "cache")

    async def handler(_args: ValidatedToolArgs) -> object:
        return "Q" * 800

    out = await cap.wrap_tool_execute(
        cast("RunContext[HarnessDeps]", _UNUSED),
        call=_call(),
        tool_def=cast("ToolDefinition", _UNUSED),
        args=cast("ValidatedToolArgs", {}),
        handler=handler,
    )
    assert isinstance(out, str)
    assert "characters elided to save context" in out


async def test_wrap_tool_execute_passes_non_string_results_through() -> None:
    cap = _cap()
    payload = {"structured": [1, 2, 3]}

    async def handler(_args: ValidatedToolArgs) -> object:
        return payload

    out = await cap.wrap_tool_execute(
        cast("RunContext[HarnessDeps]", _UNUSED),
        call=_call(),
        tool_def=cast("ToolDefinition", _UNUSED),
        args=cast("ValidatedToolArgs", {}),
        handler=handler,
    )
    assert out is payload  # non-string results are left exactly as the tool returned them


def _resp(think: str, call_id: str) -> ModelResponse:
    return ModelResponse(
        parts=[ThinkingPart(content=think), _call(call_id)]
    )


def _ret(call_id: str) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="bash", content="out", tool_call_id=call_id)]
    )


def _kinds(msg: ModelMessage) -> list[str]:
    return [part.part_kind for part in msg.parts]


def _history() -> list[ModelMessage]:
    # three responses (each thinking + tool-call) interleaved with their tool returns
    return [_resp("t1", "1"), _ret("1"), _resp("t2", "2"), _ret("2"), _resp("t3", "3")]


def test_trim_reasoning_keeps_only_recent_thinking() -> None:
    out = trim_reasoning(_history(), keep_recent=1)
    assert len(out) == 5  # nothing dropped, only reshaped
    assert "thinking" not in _kinds(out[0])  # oldest response: reasoning stripped
    assert "tool-call" in _kinds(out[0])  # ... but its action is preserved
    assert "thinking" not in _kinds(out[2])  # middle response: stripped too
    assert "thinking" in _kinds(out[4])  # most recent response: reasoning kept


def test_trim_reasoning_leaves_tool_returns_untouched() -> None:
    out = trim_reasoning(_history(), keep_recent=1)
    assert _kinds(out[1]) == ["tool-return"]  # request messages are never altered
    assert _kinds(out[3]) == ["tool-return"]


def test_trim_reasoning_zero_strips_all_reasoning() -> None:
    out = trim_reasoning(_history(), keep_recent=0)
    assert all("thinking" not in _kinds(m) for m in out if isinstance(m, ModelResponse))


def test_trim_reasoning_never_empties_a_response() -> None:
    only_thought = ModelResponse(parts=[ThinkingPart(content="sole")])
    out = trim_reasoning([only_thought], keep_recent=0)
    assert _kinds(out[0]) == ["thinking"]  # stripping would empty it, so it is kept as-is


def test_trim_reasoning_keeps_non_thinking_response_unchanged() -> None:
    plain = ModelResponse(parts=[TextPart(content="done")])
    out = trim_reasoning([plain], keep_recent=0)
    assert out[0] is plain  # no thinking to strip -> same object, untouched


def test_make_reasoning_trimmer_builds_a_processor() -> None:
    processor = make_reasoning_trimmer(1)
    history = _history()
    assert [_kinds(m) for m in processor(history)] == [
        _kinds(m) for m in trim_reasoning(history, keep_recent=1)
    ]
