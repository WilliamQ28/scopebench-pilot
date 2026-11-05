"""Adapt the pinned AgentJudge core to our sandbox and explicit model transport."""

# The adapter deliberately reuses private pinned-core methods and lazily dispatches
# from capability.py; the runtime import cycle is broken at the call boundary.
# pyright: reportPrivateUsage=false, reportImportCycles=false
from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, cast

from pydantic_ai import UsageLimits
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from scopejudge.capability import (
    _JUDGE_TRAJECTORY_MAX_CHARS,
    ScopeDecision,
    _strip_text,
    _without_reasoning,
)
from scopejudge.mediation import ACTOR_JUSTIFICATION_POLICY
from scopejudge.recording import ResponseRecorder
from scopejudge.vendor.agentjudge import Message, ProcessJudge, ToolCall

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.capabilities import ValidatedToolArgs
    from pydantic_ai.messages import ToolCallPart

    from agent_harness import HarnessDeps
    from scopejudge.capability import ScopeJudge
    from scopejudge.protocol import JudgeActorInformation, JudgeTranscriptStrategy

UPSTREAM_VERSION = "2.0.53"


def policy_prompt(instruction: str, actor_information: JudgeActorInformation | None = None) -> str:
    """Return the actual official system policy, including the safety floor."""
    core = ProcessJudge(rubric=instruction)
    prompt = core._build_messages([], "")[0].content  # noqa: SLF001
    if actor_information is not None:
        prompt += "\n" + ACTOR_JUSTIFICATION_POLICY
    return prompt


def transcript(history: object, strategy: JudgeTranscriptStrategy) -> list[Message]:
    """Translate PydanticAI text history and apply the upstream transcript choices."""
    if strategy == "rubric_only":
        return []
    messages: list[Message] = []
    for raw in cast("list[dict[str, object]]", history):
        parts = cast("list[dict[str, object]]", raw.get("parts", []))
        # Keep role boundaries and source order. Tool results are separate messages.
        calls: list[ToolCall] = []
        prose: list[str] = []
        for part in parts:
            kind = part.get("part_kind")
            if kind in {"system-prompt", "user-prompt"}:
                messages.append(
                    Message(
                        role="system" if kind == "system-prompt" else "user",
                        content=str(part.get("content", "")),
                    )
                )
            elif kind == "tool-call" and strategy != "intent_only":
                args = part.get("args", {})
                calls.append(
                    ToolCall(
                        name=str(part.get("tool_name", "")),
                        arguments=args if isinstance(args, str) else json.dumps(args),
                        call_id=str(part.get("tool_call_id", "")),
                    )
                )
            elif kind == "tool-return" and strategy == "full":
                messages.append(
                    Message(
                        role="tool",
                        content=str(part.get("content", "")),
                        tool_call_id=str(part.get("tool_call_id", "")),
                    )
                )
            elif kind in {"text", "thinking"} and strategy == "full":
                prose.append(str(part.get("content", "")))
        if calls or prose:
            messages.append(Message(role="assistant", content="\n".join(prose), tool_calls=calls))
    # Fixed across judge sizes, unlike upstream's model-dependent estimate.
    protected_end = 0
    if messages and messages[0].role == "system":
        protected_end = 1
    for i in range(protected_end, len(messages)):
        if messages[i].role == "user":
            protected_end = i + 1
            break
    used = sum(len(str(m)) for m in messages)
    drop_to = protected_end
    while used > _JUDGE_TRAJECTORY_MAX_CHARS and drop_to < len(messages):
        used -= len(str(messages[drop_to]))
        drop_to += 1
    return messages[:protected_end] + messages[drop_to:]


async def evaluate(
    gate: ScopeJudge,
    ctx: RunContext[HarnessDeps],
    call: ToolCallPart,
    args: ValidatedToolArgs,
) -> ScopeDecision:
    """Judge with upstream prompts/parser through the configured PydanticAI model.

    No SDK global routing, provider-name conversion, or environment key mutation is
    involved: openai-chat models retain their explicit base URL and bearer key.
    """
    information: dict[str, object] | None = None
    if gate.actor_information is not None:
        # Explicit experiments override native transcript selection. A single
        # shared builder keeps evidence, current response, redaction, and bounds
        # identical to the current backend. Do not abbreviate historical args.
        information = gate.information_payload(ctx, call, args)
        intent = [Message(role="user", content=json.dumps(information))]
    else:
        intent = native_transcript(gate, ctx, call)
    core = ProcessJudge(rubric=gate.instruction)
    proposed = ToolCall(call.tool_name, json.dumps(args), call.tool_call_id)
    wire = core._build_messages(intent, core._render_proposed_call(proposed))  # noqa: SLF001
    wire[0].content = policy_prompt(gate.instruction, gate.actor_information)
    capabilities: list[ResponseRecorder[None]] = []
    if gate.recording_dir is not None:
        payload = {
            "judge_backend": "agentjudge",
            "upstream_version": UPSTREAM_VERSION,
            "transcript_strategy": (
                gate.transcript_strategy if gate.actor_information is None else None
            ),
            "actor_information": gate.actor_information,
            "information": information,
            "policy_sha256": hashlib.sha256(wire[0].content.encode()).hexdigest(),
            "messages": [{"role": m.role, "content": m.content} for m in wire],
        }
        gate.record_input(call.tool_call_id, json.dumps(payload))
        capabilities.append(
            ResponseRecorder[None](
                gate.recording_dir / "model-responses.jsonl",
                role="judge",
                call_id=call.tool_call_id,
                buffer=gate.recorded_responses,
            )
        )
    result = await gate.judge.run(
        # Static Agent.system_prompt is not injected when continuing supplied
        # history. Include the policy explicitly so the provider always gets it.
        message_history=[
            ModelRequest(parts=[SystemPromptPart(wire[0].content)]),
            *[ModelRequest(parts=[UserPromptPart(m.content)]) for m in wire[1:]],
        ],
        usage_limits=UsageLimits(request_limit=3),
        capabilities=capabilities,
    )
    action = core._extract_action(result.output)  # noqa: SLF001
    # Same reason extraction as the SDK's regex fallback; ASK has no operator here.
    match = re.search(r"<reason>(.*?)</reason>", result.output, re.DOTALL | re.IGNORECASE)
    reason = match.group(1).strip() if match else "Unable to parse reason"
    verdict = "allow" if action == "allow" else "uncertain" if action == "ask" else "deny"
    return ScopeDecision(verdict=verdict, reason=reason or "Unable to parse reason")


def native_transcript(
    gate: ScopeJudge, ctx: RunContext[HarnessDeps], call: ToolCallPart
) -> list[Message]:
    """Preserve the upstream history snapshot when no shared treatment is selected."""
    from pydantic_ai.messages import ModelMessagesTypeAdapter  # noqa: PLC0415

    history = cast("object", json.loads(ModelMessagesTypeAdapter.dump_json(ctx.messages)))
    # The SDK hook snapshots history at GenerationStart, before the response
    # proposing this batch. Exclude that response here too, including siblings.
    prior = cast("list[dict[str, object]]", history)
    for index in range(len(prior) - 1, -1, -1):
        parts = cast("list[dict[str, object]]", prior[index].get("parts", []))
        if any(
            p.get("part_kind") == "tool-call" and p.get("tool_call_id") == call.tool_call_id
            for p in parts
        ):
            history = prior[:index]
            break
    if gate.hide_brief and gate.attacker_brief:
        history = _strip_text(history, gate.attacker_brief)
    if gate.hide_actor_reasoning:
        history = _without_reasoning(history)
    return transcript(history, gate.transcript_strategy)
