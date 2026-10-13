"""Hermetic tests for tool-result compaction."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.messages import ToolCallPart

from agent_harness import HarnessDeps
from scopejudge.compaction import MIN_RESULT_BUDGET, TruncateToolResults, _safe_name


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
