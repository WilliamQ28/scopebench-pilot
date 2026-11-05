"""Pre-execution scope enforcement for harness function tools."""

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Literal, cast, override

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.capabilities.abstract import RawToolArgs
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from agent_harness import HarnessDeps
from scopejudge.mediation import (
    JUSTIFICATION_KEY,
    evidence_history,
    mediation_tools,
    split_justification,
)
from scopejudge.protocol import (
    DenyFeedback,
    JudgeActorInformation,
    JudgeBackend,
    JudgeHistoryFormat,
    JudgeTranscriptStrategy,
)

# A verdict is a short JSON object, but a reasoning ("thinking") judge model emits reasoning
# tokens before it. Without an explicit cap an OpenAI-compatible (vLLM) endpoint reserves half the
# context window for output (e.g. 32768 of 65536), halving the trajectory the judge can read; too
# small a cap instead truncates the model mid-reason ("token limit exceeded before any response").
# This value leaves ample room for reasoning plus the verdict while preserving most of the context
# window for the input trajectory.
_JUDGE_MAX_OUTPUT_TOKENS = 4096

# The judge sees the whole actor trajectory, which grows every step; a smaller-context judge would
# otherwise overflow and fail closed (over-block) on long runs, confounding a judge-SIZE comparison
# with context capacity. Bounding the trajectory to a fixed recent window -- identical for every
# judge, and sized to fit well inside the smallest judge's context -- makes "what the judge sees"
# constant across models. The budget is in characters (~4 per token) as a tokenizer-free proxy:
# ~120k chars is roughly 30k tokens, comfortably under a 64k-context judge even with the output
# reservation and the instruction. A trajectory already under budget is sent UNCHANGED, so runs
# that never approach the limit (and frozen baselines) are byte-for-byte unaffected. This bounds
# proximate context only: a multi-step attack whose intent is established far earlier may fall
# outside the window -- an accepted limitation of comparing judges at equal, bounded information.
_JUDGE_TRAJECTORY_MAX_CHARS = 120_000


def _bound_trajectory(trajectory: object, max_chars: int) -> object:
    """Keep only the most recent trajectory entries that fit in ``max_chars``, flagging elision.

    Returns the trajectory unchanged when it already fits or is not a list, so small runs are
    unaffected. When it overflows, keeps the newest entries within budget (at least one), preserving
    order, and wraps them with a count of the elided earlier steps so the judge knows its view of
    the history is partial.
    """
    if not isinstance(trajectory, list):
        return trajectory
    items = cast("list[object]", trajectory)
    kept: list[object] = []
    used = 0
    for item in reversed(items):
        size = len(json.dumps(item)) + 1  # +1 approximates the separator between entries
        if kept and used + size > max_chars:
            break
        used += size
        kept.append(item)
    if len(kept) == len(items):
        return items  # nothing elided -> return the original list unchanged (identity preserved)
    kept.reverse()
    return {"elided_earlier_steps": len(items) - len(kept), "recent_trajectory": kept}


# Compact judge history: provider/run bookkeeping that no actor saw and that carries no evidence
# about the proposed action. Everything else is kept, so an unknown or future field is never
# silently lost. The raw dump spends about two-thirds of its characters on these fields, crowding
# real history out of the bounded window and inflating judge input tokens.
_MESSAGE_BOOKKEEPING = frozenset(
    {
        "usage",
        "model_name",
        "timestamp",
        "provider_name",
        "provider_url",
        "provider_details",
        "provider_response_id",
        "run_id",
        "conversation_id",
        "metadata",
    }
)
_PART_BOOKKEEPING = frozenset(
    {"part_kind", "timestamp", "id", "provider_name", "provider_details", "signature", "tool_kind"}
)
_PART_LABELS = {
    "system-prompt": "system",
    "user-prompt": "user",
    "text": "text",
    "thinking": "reasoning",
    "tool-call": "call",
    "tool-return": "result",
    "retry-prompt": "retry",
}
# Routine values that only restate the default; unusual ones (a truncated response, a failed tool)
# are evidence and are kept.
_ROUTINE_FINISH = frozenset({"stop", "tool_call"})
_ROUTINE_OUTCOME = "success"


def _compact_part(part: dict[str, object]) -> dict[str, object]:
    """One part as its label plus every non-bookkeeping, non-null field (args decoded)."""
    kind = str(part.get("part_kind"))
    compact: dict[str, object] = {"type": _PART_LABELS.get(kind, kind)}
    for key, value in part.items():
        if key in _PART_BOOKKEEPING or value is None:
            continue
        if key == "outcome" and value == _ROUTINE_OUTCOME:
            continue
        compact[key] = value
        if key == "args" and isinstance(value, str):
            # Decoding removes a layer of escaped quotes; undecodable args are kept verbatim.
            with contextlib.suppress(ValueError):
                compact[key] = cast("object", json.loads(value))
    return compact


def _compact_history(trajectory: object) -> object:
    """Re-serialize a PydanticAI message dump with the same content, minus bookkeeping.

    Message order, part order, every prompt, reasoning, text, tool call, tool result and retry are
    preserved. Harness ``instructions`` are emitted on the first request and again only when they
    change, since the raw dump repeats the identical text on every request.
    """
    if not isinstance(trajectory, list):
        return trajectory
    output: list[object] = []
    last_instructions: object = None
    for raw in cast("list[object]", trajectory):
        if not isinstance(raw, dict):
            output.append(raw)
            continue
        message = cast("dict[str, object]", raw)
        compact: dict[str, object] = {}
        for key, value in message.items():
            if key == "parts" or key in _MESSAGE_BOOKKEEPING or value is None:
                continue
            if key == "instructions":
                if value == last_instructions:
                    continue
                last_instructions = value
            if key == "finish_reason" and value in _ROUTINE_FINISH:
                continue
            compact[key] = value
        compact["parts"] = [
            _compact_part(cast("dict[str, object]", part)) if isinstance(part, dict) else part
            for part in cast("list[object]", message.get("parts", []))
        ]
        output.append(compact)
    return output


def _without_reasoning(obj: object) -> object:
    """Remove structured thinking parts, preserving text, tool calls, and results."""
    if isinstance(obj, list):
        return [
            _without_reasoning(cast("object", item))
            for item in cast("list[object]", obj)
            if not (
                isinstance(item, dict)
                and cast("dict[str, object]", item).get("part_kind") == "thinking"
            )
        ]
    if isinstance(obj, dict):
        return {
            key: _without_reasoning(value) for key, value in cast("dict[str, object]", obj).items()
        }
    return obj


def _strip_text(obj: object, needle: str) -> object:
    """Recursively remove every occurrence of ``needle`` from strings in a parsed JSON value."""
    if isinstance(obj, str):
        return obj.replace(needle, "")
    if isinstance(obj, list):
        return [_strip_text(item, needle) for item in cast("list[object]", obj)]
    if isinstance(obj, dict):
        items = cast("dict[str, object]", obj).items()
        return {key: _strip_text(value, needle) for key, value in items}
    return obj


class ScopeDecision(BaseModel):
    """A structured authorization decision, independent of tool execution."""

    verdict: Literal["allow", "deny", "uncertain"]
    reason: str = Field(min_length=1)


class GateRecord(BaseModel):
    """Audit evidence for an attempted call and its authorization."""

    call_id: str
    tool: str
    arguments: str
    decision: ScopeDecision
    execution_allowed: bool
    actor_justification: str | None = None
    actor_information: JudgeActorInformation | None = None
    judge_backend: JudgeBackend = "agentjudge"
    judge_transcript_strategy: JudgeTranscriptStrategy | None = None


class ScopeJudge(AbstractCapability[HarnessDeps]):
    """Execute a tool only after an independent judge explicitly allows it."""

    def __init__(
        self,
        *,
        instruction: str,
        model: Model,
        timeout: float = 30.0,
        feedback: DenyFeedback = "reasoned",
        attacker_brief: str | None = None,
        hide_brief: bool = False,
        hide_actor_reasoning: bool = False,
        recording_dir: Path | None = None,
        actor_information: JudgeActorInformation | None = None,
        backend: str = "agentjudge",
        transcript_strategy: JudgeTranscriptStrategy = "intent_plus_calls",
        history_format: JudgeHistoryFormat = "raw",
    ) -> None:
        """Create a tool-free judge with a bounded authorization timeout."""
        if actor_information == "full" and hide_actor_reasoning:
            raise ValueError("full actor information conflicts with hidden actor reasoning")
        if backend != "agentjudge":
            raise ValueError("Only the Dreadnode agentjudge backend is supported")
        self.backend: JudgeBackend = backend
        self.transcript_strategy: JudgeTranscriptStrategy = transcript_strategy
        self.history_format: JudgeHistoryFormat = history_format
        self.actor_information: JudgeActorInformation | None = actor_information
        self.justifications: dict[str, str] = {}
        self.instruction: str = instruction
        self.timeout: float = timeout
        self.feedback: DenyFeedback = feedback
        # Arm B: the brief text to redact from the judge's trajectory view, when hiding is on.
        self.attacker_brief: str | None = attacker_brief
        self.hide_brief: bool = hide_brief
        self.hide_actor_reasoning: bool = hide_actor_reasoning
        self.recording_dir: Path | None = recording_dir
        # Both buffers stay in memory until flush: recording_dir is the actor's workspace, so a
        # live file would let the actor read the judge's policy and inputs mid-run.
        self.recorded_inputs: list[str] = []
        self.recorded_responses: list[str] = []
        self.records: list[GateRecord] = []
        self.judge: Agent[None, str] = Agent(
            model,
            output_type=str,
            model_settings=ModelSettings(max_tokens=_JUDGE_MAX_OUTPUT_TOKENS),
        )

    @override
    async def prepare_tools(
        self, ctx: RunContext[HarnessDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        del ctx
        return mediation_tools(tool_defs) if self.actor_information is not None else tool_defs

    @override
    async def before_tool_validate(
        self,
        ctx: RunContext[HarnessDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
    ) -> RawToolArgs:
        del ctx, tool_def
        if self.actor_information is None:
            return args
        cleaned, justification = split_justification(cast("str | dict[str, object]", args))
        self.justifications[call.tool_call_id] = justification
        return cleaned

    def record_input(self, call_id: str, payload: str) -> None:
        """Buffer one judge input; published only by ``flush_recording``."""
        self.recorded_inputs.append(json.dumps({"call_id": call_id, "payload": payload}) + "\n")

    def flush_recording(self) -> None:
        """Publish judge inputs and responses after the actor stops, so neither is live."""
        if self.recording_dir is not None:
            with (self.recording_dir / "judge-inputs.jsonl").open("a", encoding="utf-8") as stream:
                stream.writelines(self.recorded_inputs)
            with (self.recording_dir / "model-responses.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.writelines(self.recorded_responses)
            self.recorded_inputs.clear()
            self.recorded_responses.clear()

    def _deny_message(self, decision: ScopeDecision) -> str:
        """Render the denial the acting agent sees, at the configured feedback level."""
        if self.feedback == "black":
            return "Tool execution failed."
        if self.feedback == "binary":
            return "ScopeJudge blocked this call."
        reasoned = f"ScopeJudge blocked this call ({decision.verdict}): {decision.reason}"
        if self.feedback == "white":
            from scopejudge.agentjudge import policy_prompt  # noqa: PLC0415

            policy = policy_prompt(self.instruction, self.actor_information)
            return f"{reasoned}\n\n[Judge policy]\n{policy}"
        return reasoned

    def information_payload(
        self, ctx: RunContext[HarnessDeps], call: ToolCallPart, args: ValidatedToolArgs
    ) -> dict[str, object]:
        """Build the same bounded information treatment for either judge backend."""
        trajectory = cast("object", json.loads(ModelMessagesTypeAdapter.dump_json(ctx.messages)))
        if self.hide_brief and self.attacker_brief:
            trajectory = _strip_text(trajectory, self.attacker_brief)
        if self.hide_actor_reasoning:
            trajectory = _without_reasoning(trajectory)
        if self.actor_information in {"none", "justification"}:
            trajectory = evidence_history(trajectory)
        if self.history_format == "compact":
            # Applied before the bound, so the fixed window holds more real history, not less.
            trajectory = _compact_history(trajectory)
        trajectory = _bound_trajectory(trajectory, _JUDGE_TRAJECTORY_MAX_CHARS)
        information: dict[str, object] = {}
        if self.actor_information is not None:
            information["actor_information"] = self.actor_information
            if self.actor_information != "none":
                information["actor_justification"] = self.justifications.get(call.tool_call_id)
        return {
            **information,
            "engagement_instruction": self.instruction,
            "proposed_tool": call.tool_name,
            "validated_arguments": args,
            "trajectory": trajectory,
        }

    async def _judge(
        self, ctx: RunContext[HarnessDeps], call: ToolCallPart, args: ValidatedToolArgs
    ) -> ScopeDecision:
        from scopejudge.agentjudge import evaluate  # noqa: PLC0415

        return await evaluate(self, ctx, call, args)

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
        """Fail closed before invoking the handler; denials are normal tool results."""
        del tool_def
        try:
            decision = await asyncio.wait_for(self._judge(ctx, call, args), timeout=self.timeout)
        except Exception:  # noqa: BLE001 - authorization failures must never execute the tool
            decision = ScopeDecision(verdict="uncertain", reason="Scope judge failed or timed out.")
        allowed = decision.verdict == "allow"
        self.records.append(
            GateRecord(
                call_id=call.tool_call_id,
                tool=call.tool_name,
                arguments=json.dumps(
                    {**args, JUSTIFICATION_KEY: self.justifications[call.tool_call_id]}
                    if call.tool_call_id in self.justifications
                    else args
                ),
                actor_justification=self.justifications.get(call.tool_call_id),
                actor_information=self.actor_information,
                judge_backend=self.backend,
                judge_transcript_strategy=(
                    self.transcript_strategy
                    if self.backend == "agentjudge" and self.actor_information is None
                    else None
                ),
                decision=decision,
                execution_allowed=allowed,
            )
        )
        if not allowed:
            return self._deny_message(decision)
        return cast("object", await handler(args))
