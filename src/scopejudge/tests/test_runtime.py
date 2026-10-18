"""Hermetic tests for the in-container ScopeJudge application."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel

from agent_harness import HarnessDeps, Trajectory
from agent_harness.toolsets import ALL_TOOL_NAMES
from scopejudge import __main__ as container_entrypoint
from scopejudge import runtime
from scopejudge.capability import ScopeDecision, ScopeJudge
from scopejudge.compaction import TruncateToolResults
from scopejudge.protocol import AgentConfig, AgentResult


@pytest.fixture(autouse=True)
def offline_scope_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(
        _self: ScopeJudge,
        _ctx: RunContext[HarnessDeps],
        _call: ToolCallPart,
        _args: ValidatedToolArgs,
    ) -> ScopeDecision:
        return ScopeDecision(verdict="allow", reason="Offline fixture authorizes this call.")

    monkeypatch.setattr(ScopeJudge, "_judge", allow)


def _text_model(text: str = "ok") -> FunctionModel:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(respond)


def _cfg(
    model: str | None = None,
    *,
    atif: bool = False,
    request_limit: int = 5,
    scopejudge_enabled: bool = False,
    base_url: str | None = None,
) -> AgentConfig:
    return AgentConfig(
        instruction="hi",
        model=model,
        api_key="k",
        base_url=base_url,
        request_limit=request_limit,
        timeout=60.0,
        ask_user_reply="no human is available",
        atif=atif,
        session_id="sess-1",
        scopejudge_enabled=scopejudge_enabled,
    )


def test_build_model_strips_anthropic_prefix() -> None:
    assert runtime._build_model(_cfg(model="anthropic:claude-x")).model_name == "claude-x"


def test_build_model_defaults_when_none() -> None:
    assert "claude" in runtime._build_model(_cfg(model=None)).model_name


def test_build_model_openai_uses_responses_api() -> None:
    model = runtime._build_model(_cfg(model="openai:gpt-4o"))
    assert isinstance(model, OpenAIResponsesModel)
    assert model.model_name == "gpt-4o"


def test_build_judge_model_falls_back_to_actor_when_unset() -> None:
    # No judge override: the monitor runs on the actor's model (prior single-model behaviour).
    assert runtime._build_judge_model(_cfg(model="anthropic:claude-x")).model_name == "claude-x"


def test_build_judge_model_uses_independent_override() -> None:
    cfg = _cfg(model="anthropic:claude-x")
    cfg["judge_model"] = "openai:gpt-4o"
    cfg["judge_api_key"] = "jk"
    assert runtime._build_judge_model(cfg).model_name == "gpt-4o"  # judge uses its own model
    assert runtime._build_model(cfg).model_name == "claude-x"  # actor model unchanged


def test_build_model_openrouter_uses_native_provider() -> None:
    model = runtime._build_model(_cfg(model="openrouter:anthropic/claude-sonnet-4.5"))
    assert isinstance(model, OpenRouterModel)
    assert model.model_name == "anthropic/claude-sonnet-4.5"
    assert str(model.base_url) == "https://openrouter.ai/api/v1/"


def test_build_model_custom_base_url_targets_self_hosted_endpoint() -> None:
    # Self-hosted vLLM (Compute Canada) speaks the Chat Completions API: openai-chat + base_url.
    model = runtime._build_model(
        _cfg(model="openai-chat:Qwen2.5-72B-Instruct", base_url="http://cc-node:8000/v1")
    )
    assert model.model_name == "Qwen2.5-72B-Instruct"
    assert str(model.base_url) == "http://cc-node:8000/v1/"


def test_build_model_google_key_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stdin config is the only key source: with no provider env vars at all, the
    # provider must still build from cfg["api_key"].
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert runtime._build_model(_cfg(model="google:gemini-2.0-flash")).model_name == (
        "gemini-2.0-flash"
    )


def _empty_result() -> AgentResult:
    return AgentResult(output="", tool_calls=[], submitted=None, error=None)


def test_capture_flag_from_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src, out = tmp_path / "tmp.txt", tmp_path / "work.txt"
    _ = src.write_text("  FLAG{y}\n", encoding="utf-8")  # whitespace is stripped for `submitted`
    monkeypatch.setattr(runtime, "_FLAG_SRC", src)
    monkeypatch.setattr(runtime, "_FLAG_OUT", out)
    result = _empty_result()
    runtime._capture_flag(result)
    assert result.submitted == "FLAG{y}"
    assert out.read_text(encoding="utf-8") == "  FLAG{y}\n"  # copied verbatim to the workspace


def test_capture_flag_falls_back_to_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "work.txt"
    _ = out.write_text("FLAG{z}", encoding="utf-8")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "absent")
    monkeypatch.setattr(runtime, "_FLAG_OUT", out)
    result = _empty_result()
    runtime._capture_flag(result)
    assert result.submitted == "FLAG{z}"


def test_capture_flag_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "a")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "b")
    result = _empty_result()
    runtime._capture_flag(result)
    assert result.submitted is None


def test_capture_capability_bridges_to_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src, out = tmp_path / "cap_src.txt", tmp_path / "cap_out.txt"
    _ = src.write_text("FLAG{cap}\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_CAPABILITY_SRC", src)
    monkeypatch.setattr(runtime, "_CAPABILITY_OUT", out)
    runtime._capture_capability()
    assert out.read_text(encoding="utf-8") == "FLAG{cap}\n"  # copied verbatim for the verifier


def test_capture_capability_no_file_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "cap_out.txt"
    monkeypatch.setattr(runtime, "_CAPABILITY_SRC", tmp_path / "absent")
    monkeypatch.setattr(runtime, "_CAPABILITY_OUT", out)
    runtime._capture_capability()
    assert not out.exists()


def _deferred_then_text() -> FunctionModel:
    state = {"n": 0}

    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        state["n"] += 1
        if state["n"] == 1:
            # A confirm (approval) and an ask_user (call) together exercise both resolver loops.
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="confirm", args={"action": "x"}, tool_call_id="a1"),
                    ToolCallPart(tool_name="ask_user", args={"question": "?"}, tool_call_id="q1"),
                ],
            )
        return ModelResponse(parts=[TextPart("resolved")])

    return FunctionModel(fn)


async def test_run_agent_resolves_deferred_requests(tmp_path: Path) -> None:
    out = await runtime.run_agent(
        _cfg(), model=_deferred_then_text(), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "resolved"
    assert {call.name for call in out["tool_calls"]} >= {"confirm", "ask_user"}


async def test_run_agent_completes_without_running_tools(tmp_path: Path) -> None:
    out = await runtime.run_agent(_cfg(), model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path))
    assert out["output"] == "done"
    assert out["tool_calls"] == []


def test_tool_calls_extract_complete_arguments() -> None:
    marker = "admin.vesta-market.example"
    call = ToolCallPart(
        tool_name="bash",
        args={"cmd": "x" * 400 + marker},
        tool_call_id="c1",
    )
    calls = runtime._tool_calls([ModelResponse(parts=[call])])
    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert marker in calls[0].args


def test_scopejudge_capability_is_explicitly_toggled(tmp_path: Path) -> None:
    assert container_entrypoint.main is runtime.main
    assert runtime._capabilities(_cfg(scopejudge_enabled=False), tmp_path) == ()
    capabilities = runtime._capabilities(_cfg(scopejudge_enabled=True), tmp_path)
    assert len(capabilities) == 1
    assert isinstance(capabilities[0], ScopeJudge)


def test_compaction_capability_added_only_when_configured(tmp_path: Path) -> None:
    # Absent/0 budget leaves baseline runs untouched; a positive budget adds the compactor.
    assert runtime._capabilities(_cfg(), tmp_path) == ()
    off = _cfg()
    off["max_tool_result_chars"] = 0
    assert runtime._capabilities(off, tmp_path) == ()

    compact_only = _cfg()
    compact_only["max_tool_result_chars"] = 2000
    caps = runtime._capabilities(compact_only, tmp_path)
    assert len(caps) == 1
    assert isinstance(caps[0], TruncateToolResults)

    both = _cfg(scopejudge_enabled=True)
    both["max_tool_result_chars"] = 2000
    caps_both = runtime._capabilities(both, tmp_path)
    # The judge gates first; the compactor shapes only results the judge has allowed.
    assert [type(cap) for cap in caps_both] == [ScopeJudge, TruncateToolResults]


async def test_run_agent_compaction_only_writes_no_decisions(tmp_path: Path) -> None:
    # A compaction-only run carries no ScopeJudge, so run_agent must skip it in the decisions
    # loop and emit no decisions file (exercises the non-ScopeJudge capability path).
    cfg = _cfg()
    cfg["max_tool_result_chars"] = 2000
    out = await runtime.run_agent(cfg, model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path))
    assert out["output"] == "done"
    assert not (tmp_path / "scopejudge-decisions.json").exists()


def _todo_then_text() -> FunctionModel:
    state = {"called": False}

    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if not state["called"]:
            state["called"] = True
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="todo",
                        args={
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "exercise hook",
                                    "status": "pending",
                                    "priority": "high",
                                }
                            ]
                        },
                        tool_call_id="c1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


@pytest.mark.parametrize(("enabled", "expected_calls"), [(False, 0), (True, 1)])
async def test_scopejudge_runs_after_each_completed_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enabled: bool,
    expected_calls: int,
) -> None:
    observed: list[object] = []
    original = ScopeJudge.after_tool_execute

    async def observe(
        self: ScopeJudge,
        ctx: RunContext[HarnessDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        result: object,
    ) -> object:
        observed.append(call)
        return cast(
            "object",
            await original(
                self,
                ctx,
                call=call,
                tool_def=tool_def,
                args=args,
                result=result,
            ),
        )

    monkeypatch.setattr(ScopeJudge, "after_tool_execute", observe)
    out = await runtime.run_agent(
        _cfg(scopejudge_enabled=enabled),
        model=_todo_then_text(),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert out["output"] == "done"
    assert len(observed) == expected_calls


def test_main_reads_stdin_and_writes_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_cfg())))
    monkeypatch.setattr(runtime, "_WORK", tmp_path)
    monkeypatch.setattr(runtime, "_RESULT_OUT", tmp_path / "agent-result.json")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "flag_src")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "flag_out")

    def build(_cfg: AgentConfig) -> Model:
        return _text_model()

    monkeypatch.setattr(runtime, "_build_model", build)
    assert runtime.main() == 0
    data = _read_result(tmp_path)
    assert data.output == "ok"
    assert data.error is None


def _main_with_failing_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: Exception
) -> AgentResult:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_cfg())))
    monkeypatch.setattr(runtime, "_WORK", tmp_path)
    monkeypatch.setattr(runtime, "_RESULT_OUT", tmp_path / "agent-result.json")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "flag_src")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "flag_out")

    def _raise(_cfg: AgentConfig) -> object:
        raise exc

    monkeypatch.setattr(runtime, "_build_model", _raise)
    assert runtime.main() == 0
    return _read_result(tmp_path)


def test_main_records_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _main_with_failing_build(monkeypatch, tmp_path, TimeoutError())
    assert data.error is not None
    assert "time budget" in data.error


def test_main_records_generic_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _main_with_failing_build(monkeypatch, tmp_path, RuntimeError("kaboom"))
    assert data.error is not None
    assert "kaboom" in data.error


def test_main_records_invalid_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # api_key has the wrong type: validation fails, but the offending value must not leak into
    # the host-visible result file (the real config carries the actual API key).
    monkeypatch.setattr("sys.stdin", io.StringIO('{"instruction": "hi", "api_key": 123456}'))
    monkeypatch.setattr(runtime, "_WORK", tmp_path)
    monkeypatch.setattr(runtime, "_RESULT_OUT", tmp_path / "agent-result.json")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "flag_src")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "flag_out")
    assert runtime.main() == 0
    error = _read_result(tmp_path).error
    assert error is not None
    assert error.startswith("invalid agent config")
    assert "123456" not in error


def test_main_records_broken_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _BrokenStdin:
        def read(self) -> str:
            raise OSError("stdin exploded")

    monkeypatch.setattr("sys.stdin", _BrokenStdin())
    monkeypatch.setattr(runtime, "_WORK", tmp_path)
    monkeypatch.setattr(runtime, "_RESULT_OUT", tmp_path / "agent-result.json")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "flag_src")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "flag_out")
    assert runtime.main() == 0
    error = _read_result(tmp_path).error
    assert error is not None
    assert error.startswith("invalid agent config")
    assert "OSError" in error


_RESULT_ADAPTER: TypeAdapter[AgentResult] = TypeAdapter(AgentResult)


def _read_result(tmp_path: Path) -> AgentResult:
    # Validating (rather than json.loads-ing) means every main() test also proves the file
    # conforms to the Result wire shape.
    text = (tmp_path / "agent-result.json").read_text(encoding="utf-8")
    return _RESULT_ADAPTER.validate_json(text)


# --- ATIF trajectory output ------------------------------------------------


def _load_trajectory(tmp_path: Path) -> Trajectory:
    return Trajectory.model_validate_json(
        (tmp_path / "trajectory.json").read_text(encoding="utf-8")
    )


def _always_todo() -> FunctionModel:
    """A model that endlessly calls a safe in-memory tool, to exhaust the request budget."""

    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="todo",
                    args={
                        "todos": [
                            {"id": "1", "content": "w", "status": "pending", "priority": "high"}
                        ]
                    },
                    tool_call_id="c1",
                )
            ]
        )

    return FunctionModel(fn)


async def test_run_agent_writes_valid_trajectory_when_atif_enabled(tmp_path: Path) -> None:
    out = await runtime.run_agent(
        _cfg(atif=True), model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "done"
    traj = _load_trajectory(tmp_path)  # model_validate_json proves it is conformant ATIF
    assert any(s.source == "user" for s in traj.steps)
    assert any(s.source == "agent" and s.message == "done" for s in traj.steps)
    assert traj.agent.name == "agent-harness"
    assert traj.session_id == "sess-1"
    assert traj.trajectory_id is not None
    _ = uuid.UUID(traj.trajectory_id)  # the runtime mints a fresh UUID per trajectory
    assert traj.agent.tool_definitions is not None
    names = {cast("dict[str, object]", d["function"])["name"] for d in traj.agent.tool_definitions}
    assert names == set(ALL_TOOL_NAMES)


async def test_scopejudge_run_has_distinct_trajectory_identity(tmp_path: Path) -> None:
    _ = await runtime.run_agent(
        _cfg(atif=True, scopejudge_enabled=True),
        model=_text_model("done"),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert _load_trajectory(tmp_path).agent.name == "scopejudge"


async def test_run_agent_skips_trajectory_when_atif_disabled(tmp_path: Path) -> None:
    _ = await runtime.run_agent(
        _cfg(atif=False), model=_text_model(), deps=HarnessDeps(cwd=tmp_path)
    )
    assert not (tmp_path / "trajectory.json").exists()


async def test_run_agent_reports_model_request_count(tmp_path: Path) -> None:
    out = await runtime.run_agent(
        _cfg(), model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path)
    )
    # Model requests are tracked separately from tool calls (the unit request_limit caps).
    assert out["model_requests"] >= 1


async def test_run_agent_builds_restricted_agent_when_toolsets_selected(tmp_path: Path) -> None:
    # Exercises the per-run restricted-agent path; behavioural restriction is proven in test_agent.
    cfg = _cfg()
    cfg["toolsets"] = ["workflow"]
    out = await runtime.run_agent(
        cfg, model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "done"


async def test_trajectory_written_when_run_hits_request_limit(tmp_path: Path) -> None:
    # Budget exhaustion is a gradeable agent outcome: preserve its calls and partial trajectory
    # so the mechanical verifier and trajectory judge can still evaluate it.
    outcome = await runtime.run_agent(
        _cfg(atif=True, request_limit=2), model=_always_todo(), deps=HarnessDeps(cwd=tmp_path)
    )
    assert outcome["output"] == "Agent request budget exhausted before a final response."
    assert outcome["tool_calls"]
    traj = _load_trajectory(tmp_path)
    assert any(s.source == "agent" and s.tool_calls for s in traj.steps)


async def test_deferred_loop_trajectory_has_single_prompt(tmp_path: Path) -> None:
    # Each resumed run replays the full history; the captured messages must yield the "hi" user
    # prompt exactly once (no duplication across the deferred-resolution boundary).
    _ = await runtime.run_agent(
        _cfg(atif=True), model=_deferred_then_text(), deps=HarnessDeps(cwd=tmp_path)
    )
    traj = _load_trajectory(tmp_path)
    assert sum(s.source == "user" for s in traj.steps) == 1
    assert any(s.message == "resolved" for s in traj.steps)


def _counting_text_model(calls: list[int], seen_nudge: list[bool]) -> FunctionModel:
    """A model that always wants to stop (emits text, no tool calls), recording each invocation
    and whether it was shown the persistence nudge."""

    def fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls.append(1)
        saw_nudge = any(
            "continue working toward the objective" in str(getattr(part, "content", ""))
            for message in messages
            for part in message.parts
        )
        if saw_nudge and not seen_nudge:
            seen_nudge.append(True)
        return ModelResponse(parts=[TextPart("stopping")])

    return FunctionModel(fn)


async def test_persist_reprompts_a_voluntarily_stopping_agent_until_budget_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    seen_nudge: list[bool] = []
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "absent.txt")  # objective never reached
    cfg = _cfg(request_limit=5)
    cfg["persist"] = True
    out = await runtime.run_agent(
        cfg, model=_counting_text_model(calls, seen_nudge), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "stopping"
    # The agent stops after one request; persistence nudges it back to work until the shared
    # request budget (5) is spent — each nudge is itself one request.
    assert len(calls) == 5
    assert seen_nudge  # the content-neutral nudge actually reached the model


async def test_without_persist_the_agent_stops_when_it_chooses(tmp_path: Path) -> None:
    calls: list[int] = []
    seen_nudge: list[bool] = []
    out = await runtime.run_agent(
        _cfg(request_limit=5),
        model=_counting_text_model(calls, seen_nudge),
        deps=HarnessDeps(cwd=tmp_path),
    )
    assert out["output"] == "stopping"
    assert len(calls) == 1  # no re-prompt: the early final answer is accepted
    assert not seen_nudge


async def test_persist_stops_once_objective_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    seen_nudge: list[bool] = []

    # The objective becomes "reached" after the first model response: one nudge, then stop.
    def _reached_after_first(_hash: str | None) -> bool:
        return len(calls) >= 2

    monkeypatch.setattr(runtime, "_objective_reached", _reached_after_first)
    cfg = _cfg(request_limit=50)
    cfg["persist"] = True
    out = await runtime.run_agent(
        cfg, model=_counting_text_model(calls, seen_nudge), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "stopping"
    # Persistence stops on success, not just on budget: initial call + one nudge = 2, far below 50.
    assert len(calls) == 2


async def test_objective_reached_uses_trusted_hash_not_arbitrary_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = tmp_path / "result.txt"
    monkeypatch.setattr(runtime, "_FLAG_SRC", flag)
    digest = hashlib.sha256(b"FLAG{reached}").hexdigest()
    assert runtime._objective_reached(digest) is False  # absent flag
    assert runtime._objective_reached(None) is False  # no trusted target -> never stop early
    _ = flag.write_text("FLAG{reached}\n", encoding="utf-8")
    assert runtime._objective_reached(digest) is True  # correct flag matches the grading digest
    assert runtime._objective_reached("00" * 32) is False  # wrong target digest
    _ = flag.write_text("FLAG{wrong-or-junk}", encoding="utf-8")
    assert runtime._objective_reached(digest) is False  # nonempty text that is NOT the objective
    # End to end: with a trusted digest set but never produced, persistence runs to the budget
    # rather than stopping on whatever text the actor-writable file holds.
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "absent.txt")
    calls: list[int] = []
    seen_nudge: list[bool] = []
    cfg = _cfg(request_limit=3)
    cfg["persist"] = True
    cfg["objective_hash"] = digest
    _ = await runtime.run_agent(
        cfg, model=_counting_text_model(calls, seen_nudge), deps=HarnessDeps(cwd=tmp_path)
    )
    assert len(calls) == 3


async def test_persist_respects_max_nudges_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    seen_nudge: list[bool] = []
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "absent.txt")  # objective never reached
    cfg = _cfg(request_limit=10)
    cfg["persist"] = True
    cfg["max_nudges"] = 2
    out = await runtime.run_agent(
        cfg, model=_counting_text_model(calls, seen_nudge), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "stopping"
    # Budget (10) is ample, so max_nudges binds first: initial request + 2 nudges = 3 calls.
    assert len(calls) == 3


async def test_trajectory_written_on_timeout_cancellation(tmp_path: Path) -> None:
    async def _slow(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("late")])

    with pytest.raises(TimeoutError):
        _ = await asyncio.wait_for(
            runtime.run_agent(
                _cfg(atif=True), model=FunctionModel(_slow), deps=HarnessDeps(cwd=tmp_path)
            ),
            timeout=0.1,
        )
    # The initial prompt was captured before the model stalled, so a partial trajectory survives.
    assert any(s.source == "user" for s in _load_trajectory(tmp_path).steps)


async def test_trajectory_write_errors_are_suppressed(tmp_path: Path) -> None:
    # A trajectory.json that is a directory makes the write fail; the run must still succeed.
    (tmp_path / "trajectory.json").mkdir()
    out = await runtime.run_agent(
        _cfg(atif=True), model=_text_model("done"), deps=HarnessDeps(cwd=tmp_path)
    )
    assert out["output"] == "done"
    assert (tmp_path / "trajectory.json").is_dir()  # untouched


def test_write_trajectory_skips_empty_history(tmp_path: Path) -> None:
    runtime._write_trajectory(
        [],
        model=_text_model(),
        cwd=tmp_path,
        session_id=None,
        scopejudge_enabled=False,
    )
    assert not (tmp_path / "trajectory.json").exists()


def test_agent_version_returns_installed_version() -> None:
    assert runtime._agent_version() != "unknown"  # agent-harness has dist metadata on the host


def test_agent_version_falls_back_when_unpackaged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(runtime, "version", _raise)
    assert runtime._agent_version() == "unknown"


def test_main_writes_trajectory_when_atif_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_cfg(atif=True))))
    monkeypatch.setattr(runtime, "_WORK", tmp_path)
    monkeypatch.setattr(runtime, "_RESULT_OUT", tmp_path / "agent-result.json")
    monkeypatch.setattr(runtime, "_FLAG_SRC", tmp_path / "flag_src")
    monkeypatch.setattr(runtime, "_FLAG_OUT", tmp_path / "flag_out")

    def build(_cfg: AgentConfig) -> Model:
        return _text_model("done")

    monkeypatch.setattr(runtime, "_build_model", build)
    assert runtime.main() == 0
    assert _read_result(tmp_path).output == "done"
    traj = _load_trajectory(tmp_path)
    assert any(s.message == "done" for s in traj.steps)
    assert traj.session_id == "sess-1"  # threaded from the stdin config to the trajectory
