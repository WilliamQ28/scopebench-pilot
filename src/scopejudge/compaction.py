"""Trajectory context control: cap oversized tool results before they enter history.

Long tool outputs (HTTP bodies, directory listings, process tables, file dumps) dominate an
agent's context window and crowd out the turns it needs for multi-step tradecraft. This
capability wraps tool execution and, for oversized *string* results, keeps the head and tail and
elides the middle, writing the full output to a per-call cache file the agent can re-read. Two
invariants protect experiment validity:

* A result carrying a task flag (``FLAG{...}``) is never truncated, so the capability floor --
  whether the raw agent can reach the objective -- is unaffected by compaction.
* Compaction is a standalone capability, independent of :class:`~scopejudge.capability.ScopeJudge`,
  so it can be enabled uniformly across gate-on and gate-off (capability-control) arms.

Identical oversized results collapse to a single back-reference, so repeated probes returning the
same large body (common during enumeration) cost the window only once.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from pathlib import Path
from typing import cast, override

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart

from agent_harness import HarnessDeps

# A tool result is left untouched when it fits in this many characters. Smaller budgets are
# rejected so a head+tail excerpt always leaves a usable window on both ends.
MIN_RESULT_BUDGET = 256

# Keep roughly two-thirds of the budget as the head (status lines and structure usually open the
# output) and the remainder as the tail (errors, flags and trailers usually close it).
_HEAD_NUMERATOR = 2
_HEAD_DENOMINATOR = 3

# The objective marker: a result carrying a task flag is never elided, at any size.
_FLAG_MARKER = re.compile(r"FLAG\{")

# Characters disallowed in a cache filename derived from a tool-call id.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(call_id: str) -> str:
    """Return a filesystem-safe basename for a tool-call id."""
    return _UNSAFE_NAME.sub("_", call_id) or "result"


class TruncateToolResults(AbstractCapability[HarnessDeps]):
    """Cap oversized string tool results in the trajectory, losslessly for the objective."""

    def __init__(self, *, max_chars: int, cache_dir: Path | None = None) -> None:
        """Create a compactor with a per-result character budget and optional cache directory."""
        if max_chars < MIN_RESULT_BUDGET:
            raise ValueError(f"max_chars must be at least {MIN_RESULT_BUDGET}, got {max_chars}")
        self.max_chars: int = max_chars
        self.cache_dir: Path | None = cache_dir
        # Digest -> (first call id, pointer phrase), so identical oversized results collapse.
        self._seen: dict[str, tuple[str, str]] = {}

    def _write_cache(self, text: str, call_id: str) -> str:
        """Persist the full output and return a pointer phrase, or "" when no cache is available."""
        if self.cache_dir is None:
            return ""
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / f"{_safe_name(call_id)}.txt"
            _ = path.write_text(text, encoding="utf-8")
            return (
                f" Full {len(text)} chars saved to {path}; "
                + "read that file for the complete output."
            )
        return ""

    def _compact(self, text: str, call_id: str) -> str:
        """Return ``text`` unchanged, or a head+tail excerpt with an elision marker."""
        if len(text) <= self.max_chars or _FLAG_MARKER.search(text):
            return text
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        seen = self._seen.get(digest)
        if seen is not None:
            prev_id, pointer = seen
            return f"[identical to the {len(text)}-char result from call {prev_id}.{pointer}]"
        pointer = self._write_cache(text, call_id)
        self._seen[digest] = (call_id, pointer)
        head = self.max_chars * _HEAD_NUMERATOR // _HEAD_DENOMINATOR
        tail = self.max_chars - head
        elided = len(text) - head - tail
        marker = f"…[{elided} characters elided to save context.{pointer}]…"
        return f"{text[:head]}\n\n{marker}\n\n{text[-tail:]}"

    @override
    async def wrap_tool_execute(
        self,
        ctx: RunContext[HarnessDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> object:
        """Execute the tool, then compact an oversized string result before it enters history."""
        del ctx, tool_def
        result = cast("object", await handler(args))
        if isinstance(result, str):
            return self._compact(result, call.tool_call_id)
        return result
