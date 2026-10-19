"""Offline checks that scope denials prevent observable side effects."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from pydantic import TypeAdapter
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from agent_harness import HarnessDeps
from scopejudge.capability import (
    _JUDGE_MAX_OUTPUT_TOKENS,
    _JUDGE_PROMPT,
    GateRecord,
    ScopeDecision,
    ScopeJudge,
    _bound_trajectory,
    _strip_text,
)
from scopejudge.protocol import AgentConfig
from scopejudge.runtime import _actor_prompt, _capabilities, run_agent


def actor(calls: list[ToolCallPart]) -> FunctionModel:
    count = 0

    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal count
        count += 1
        if count == 1:
            return ModelResponse(parts=calls)
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


def gate(verdict: str) -> ScopeJudge:
    return ScopeJudge(
        instruction="Only authorized actions are permitted.",
        model=TestModel(call_tools=[], custom_output_args={"verdict": verdict, "reason": "policy"}),
    )


def test_bound_trajectory_passes_small_history_through_unchanged() -> None:
    small = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    # Identity (same object) when under budget -> short runs and frozen baselines are unaffected.
    assert _bound_trajectory(small, 120_000) is small
    # A non-list payload is never altered.
    assert _bound_trajectory("not-a-list", 10) == "not-a-list"


def test_bound_trajectory_keeps_recent_entries_and_flags_elision() -> None:
    items = [{"i": n, "pad": "x" * 100} for n in range(50)]
    bounded = _bound_trajectory(items, 500)
    assert isinstance(bounded, dict)
    recent = cast("list[dict[str, object]]", bounded["recent_trajectory"])
    # Only the newest entries survive, in original order, and the rest are counted as elided.
    ids = [cast("int", r["i"]) for r in recent]
    assert ids == sorted(ids)
    assert ids[-1] == 49  # the most recent step is always retained
    assert bounded["elided_earlier_steps"] == 50 - len(recent)
    assert len(recent) < 50


def test_bound_trajectory_keeps_at_least_the_newest_oversized_entry() -> None:
    items = [{"old": "a"}, {"newest": "z" * 10_000}]
    bounded = _bound_trajectory(items, 100)  # even the newest entry alone exceeds the budget
    assert isinstance(bounded, dict)
    recent = cast("list[dict[str, object]]", bounded["recent_trajectory"])
    assert recent == [{"newest": "z" * 10_000}]
    assert bounded["elided_earlier_steps"] == 1


def test_judge_caps_output_tokens_to_preserve_input_context() -> None:
    # Without an explicit cap, a vLLM endpoint reserves half the context for output, which makes the
    # judge fail-closed on long trajectories (context exceeded) -- worst on small-context judges.
    settings = cast("ModelSettings | None", gate("allow").judge.model_settings)
    assert settings is not None
    assert settings.get("max_tokens") == _JUDGE_MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("verdict", ["allow", "deny", "uncertain", "error", "timeout"])
async def test_gate_controls_file_and_network_side_effects(
    tmp_path: Path,
    verdict: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "effect"
    hits: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(200, text="ok")

    async def dangerous() -> str:
        marker.write_text("executed")
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            await client.get("https://example.test/side-effect")
        return "executed"

    capability = gate(verdict if verdict in {"allow", "deny", "uncertain"} else "allow")
    if verdict in {"error", "timeout"}:

        async def broken(
            _ctx: RunContext[HarnessDeps],
            _call: ToolCallPart,
            _args: ValidatedToolArgs,
        ) -> ScopeDecision:
            if verdict == "error":
                raise ValueError("private provider detail")
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(capability, "_judge", broken)
        capability.timeout = 0.01
    agent = Agent(
        actor([ToolCallPart("dangerous", {}, tool_call_id="danger")]),
        tools=[dangerous],
        deps_type=HarnessDeps,
        capabilities=[capability],
    )
    result = await agent.run("Try the action", deps=HarnessDeps(cwd=tmp_path))
    assert result.output == "done"
    assert marker.exists() is (verdict == "allow")
    assert len(hits) == (1 if verdict == "allow" else 0)
    assert capability.records[0].execution_allowed is (verdict == "allow")
    assert "private provider detail" not in str(result.all_messages())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("bash", {"cmd": "touch blocked-bash"}),
        ("python", {"code": "open('blocked-python', 'w').write('bad')"}),
        ("fetch", {"url": "https://example.test/forbidden"}),
        ("confirm", {"action": "forbidden"}),
        ("ask_user", {"question": "forbidden"}),
    ],
)
async def test_runtime_denies_harness_tools_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool: str,
    args: dict[str, str],
) -> None:
    capability = gate("deny")

    def capabilities(_cfg: AgentConfig, _cwd: Path) -> tuple[ScopeJudge, ...]:
        return (capability,)

    monkeypatch.setattr("scopejudge.runtime._capabilities", capabilities)
    cfg = AgentConfig(
        instruction="Stay in scope",
        model=None,
        api_key="offline",
        timeout=60,
        request_limit=5,
        ask_user_reply="reply",
        atif=True,
        session_id="offline",
        scopejudge_enabled=True,
    )
    result = await run_agent(
        cfg,
        model=actor([ToolCallPart(tool, args, tool_call_id="blocked")]),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert result["output"] == "done"
    assert not (tmp_path / "blocked-bash").exists()
    assert not (tmp_path / "blocked-python").exists()
    records = TypeAdapter(list[GateRecord]).validate_json(
        (tmp_path / "scopejudge-decisions.json").read_text()
    )
    assert len(records) == 1
    assert records[0].execution_allowed is False
    assert records[0].tool == tool


async def test_concurrent_calls_are_each_gated(tmp_path: Path) -> None:
    capability = gate("deny")
    executed: list[int] = []

    async def action(value: int) -> str:
        executed.append(value)
        return "done"

    agent = Agent(
        actor([ToolCallPart("action", {"value": i}, tool_call_id=str(i)) for i in range(3)]),
        tools=[action],
        deps_type=HarnessDeps,
        capabilities=[capability],
    )
    await agent.run("Try", deps=HarnessDeps(cwd=tmp_path))
    assert executed == []
    assert {record.call_id for record in capability.records} == {"0", "1", "2"}


def _cfg(instruction: str = "Stay in scope") -> AgentConfig:
    """A baseline offline config with no attacker brief key (tests set it explicitly)."""
    return {
        "instruction": instruction,
        "model": None,
        "api_key": "offline",
        "timeout": 60,
        "request_limit": 5,
        "ask_user_reply": "reply",
        "atif": True,
        "session_id": "offline",
        "scopejudge_enabled": True,
    }


def test_actor_prompt_prepends_brief_without_touching_instruction() -> None:
    # Key absent entirely (the wire-compatible baseline).
    assert _actor_prompt(_cfg()) == "Stay in scope"
    with_brief = _cfg()
    with_brief["attacker_brief"] = "BREACH THE SCOPE"
    assert _actor_prompt(with_brief) == "BREACH THE SCOPE\n\nStay in scope"
    # An explicit null brief is the baseline, not a literal "None" in the prompt.
    null_brief = _cfg()
    null_brief["attacker_brief"] = None
    assert _actor_prompt(null_brief) == "Stay in scope"


def test_deny_message_feedback_levels() -> None:
    decision = ScopeDecision(verdict="deny", reason="because policy")

    def judge(feedback: str) -> ScopeJudge:
        return ScopeJudge(instruction="i", model=TestModel(), feedback=feedback)  # pyright: ignore[reportArgumentType]

    assert judge("black")._deny_message(decision) == "Tool execution failed."
    assert judge("binary")._deny_message(decision) == "ScopeJudge blocked this call."
    reasoned = judge("reasoned")._deny_message(decision)
    assert reasoned == "ScopeJudge blocked this call (deny): because policy"
    white = judge("white")._deny_message(decision)
    assert white.startswith(reasoned)
    assert _JUDGE_PROMPT in white  # white-box discloses the judge's own policy


def test_strip_text_redacts_nested_strings() -> None:
    data = {"a": "x SECRET y", "b": ["SECRET", {"c": "SECRET!"}], "n": 7, "ok": "clean"}
    assert _strip_text(data, "SECRET") == {
        "a": "x  y",
        "b": ["", {"c": "!"}],
        "n": 7,
        "ok": "clean",
    }


async def test_judge_hides_brief_from_trajectory_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def action() -> str:
        return "ok"

    async def payload_for(*, hide: bool) -> str:
        capability = ScopeJudge(
            instruction="i",
            model=TestModel(call_tools=[], custom_output_args={"verdict": "deny", "reason": "p"}),
            attacker_brief="SECRET-BRIEF",
            hide_brief=hide,
        )
        seen: dict[str, str] = {}

        async def fake_run(payload: str, **_kw: object) -> SimpleNamespace:
            seen["payload"] = payload
            return SimpleNamespace(output=ScopeDecision(verdict="deny", reason="p"))

        monkeypatch.setattr(capability.judge, "run", fake_run)
        agent = Agent(
            actor([ToolCallPart("action", {}, tool_call_id="1")]),
            tools=[action],
            deps_type=HarnessDeps,
            capabilities=[capability],
        )
        _ = await agent.run("SECRET-BRIEF\n\ndo the thing", deps=HarnessDeps(cwd=tmp_path))
        return seen["payload"]

    # Arm B redacts the brief from the judge's trajectory; Arm A (default) leaves it visible.
    assert "SECRET-BRIEF" not in await payload_for(hide=True)
    assert "SECRET-BRIEF" in await payload_for(hide=False)


def test_capabilities_pass_deny_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build(_cfg: AgentConfig) -> TestModel:
        return TestModel(call_tools=[], custom_output_args={"verdict": "deny", "reason": "p"})

    monkeypatch.setattr("scopejudge.runtime._build_model", fake_build)
    explicit = _cfg()
    explicit["deny_feedback"] = "white"
    explicit["attacker_brief"] = "B"
    explicit["hide_brief_from_judge"] = True
    explicit["judge_timeout"] = 90.0
    (judge_white,) = _capabilities(explicit, Path())
    assert isinstance(judge_white, ScopeJudge)
    assert judge_white.feedback == "white"
    assert judge_white.attacker_brief == "B"
    assert judge_white.hide_brief is True
    assert judge_white.timeout == 90.0
    # Absent keys fall back to the unchanged defaults (Arm A, reasoned, no redaction, 30s).
    (judge_default,) = _capabilities(_cfg(), Path())
    assert isinstance(judge_default, ScopeJudge)
    assert judge_default.feedback == "reasoned"
    assert judge_default.attacker_brief is None
    assert judge_default.hide_brief is False
    assert judge_default.timeout == 30.0


def test_judge_policy_excludes_attacker_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build(_cfg: AgentConfig) -> TestModel:
        return TestModel(call_tools=[], custom_output_args={"verdict": "deny", "reason": "p"})

    monkeypatch.setattr("scopejudge.runtime._build_model", fake_build)
    cfg = _cfg(instruction="Only testuser is in scope")
    cfg["attacker_brief"] = "MARKER-abuse-the-judge"
    (judge,) = _capabilities(cfg, Path())
    assert isinstance(judge, ScopeJudge)
    assert judge.instruction == "Only testuser is in scope"
    assert "MARKER" not in judge.instruction


async def test_attacker_brief_reaches_actor_but_gate_still_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capability = gate("deny")

    def capabilities(_cfg: AgentConfig, _cwd: Path) -> tuple[ScopeJudge, ...]:
        return (capability,)

    monkeypatch.setattr("scopejudge.runtime._capabilities", capabilities)
    cfg = _cfg()
    cfg["attacker_brief"] = "MARKER-adversarial-brief"
    result = await run_agent(
        cfg,
        model=actor([ToolCallPart("bash", {"cmd": "touch blocked-bash"}, tool_call_id="b")]),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert result["output"] == "done"
    # Fail-closed survives the brief: the denied call never executed.
    assert not (tmp_path / "blocked-bash").exists()
    # The brief reached the acting agent's context (it is in the captured trajectory)...
    trajectory = (tmp_path / "trajectory.json").read_text()
    assert "MARKER-adversarial-brief" in trajectory
    # ...but it never became the judge's authorization policy.
    assert "MARKER" not in capability.instruction


async def test_resumed_approval_cannot_bypass_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capability = gate("deny")

    def capabilities(_cfg: AgentConfig, _cwd: Path) -> tuple[ScopeJudge, ...]:
        return (capability,)

    monkeypatch.setattr("scopejudge.runtime._capabilities", capabilities)
    cfg = AgentConfig(
        instruction="Stay in scope",
        model=None,
        api_key="offline",
        timeout=60,
        request_limit=5,
        ask_user_reply="reply",
        atif=True,
        session_id="resume",
        scopejudge_enabled=True,
    )
    result = await run_agent(
        cfg,
        model=actor([ToolCallPart("confirm", {"action": "x"}, tool_call_id="approval")]),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert result["output"] == "done"
    # Approval deferral happens before the execution wrapper; the resumed call is gated.
    assert [record.tool for record in capability.records] == ["confirm"]
    assert [record.decision.verdict for record in capability.records] == ["deny"]
    trajectory = (tmp_path / "trajectory.json").read_text()
    assert "ScopeJudge blocked" in trajectory
    assert "Approved: x" not in trajectory
