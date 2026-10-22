"""Research recording and actor-reasoning visibility are independent controls."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from agent_harness import HarnessDeps
from scopejudge.capability import ScopeJudge, _without_reasoning
from scopejudge.compaction import make_reasoning_trimmer
from scopejudge.protocol import AgentConfig
from scopejudge.recording import ResponseRecorder
from scopejudge.runtime import _capabilities
from scopejudge.tests.test_runtime import _cfg


@pytest.mark.parametrize("hidden", [False, True])
async def test_record_both_roles_without_leaking_hidden_thinking(
    tmp_path: Path, hidden: bool
) -> None:
    calls = 0

    def actor(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ThinkingPart("PRIVATE-ACTOR-STRATEGY"),
                    TextPart("Ordinary explanation"),
                    ToolCallPart("safe", {}, tool_call_id="proposal-1"),
                ]
            )
        return ModelResponse(parts=[ThinkingPart("ACTOR-FINAL-THOUGHT"), TextPart("done")])

    def judge(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.output_tools
        return ModelResponse(
            parts=[
                ThinkingPart("JUDGE-REASONING"),
                ToolCallPart(info.output_tools[0].name, {"verdict": "allow", "reason": "in scope"}),
            ]
        )

    async def safe() -> str:
        return "safe result"

    gate = ScopeJudge(
        instruction="Only safe is authorized",
        model=FunctionModel(judge),
        hide_actor_reasoning=hidden,
        recording_dir=tmp_path,
    )
    recorder = ResponseRecorder[HarnessDeps](tmp_path / "model-responses.jsonl", role="actor")
    agent = Agent(
        FunctionModel(actor), deps_type=HarnessDeps, tools=[safe], capabilities=[gate, recorder]
    )
    result = await agent.run("Do the task", deps=HarnessDeps(cwd=tmp_path))
    assert result.output == "done"
    inputs = [
        json.loads(line) for line in (tmp_path / "judge-inputs.jsonl").read_text().splitlines()
    ]
    payload = cast("str", inputs[0]["payload"])
    assert ("PRIVATE-ACTOR-STRATEGY" in payload) is not hidden
    assert "Ordinary explanation" in payload
    assert "Only safe is authorized" in payload
    assert inputs[0]["call_id"] == "proposal-1"
    assert "JUDGE-REASONING" not in (tmp_path / "model-responses.jsonl").read_text()
    gate.flush_recording()
    events = [
        json.loads(line) for line in (tmp_path / "model-responses.jsonl").read_text().splitlines()
    ]
    assert [e["role"] for e in cast("list[dict[str, object]]", events)] == [
        "actor",
        "actor",
        "judge",
    ]
    assert events[2]["call_id"] == "proposal-1"
    serialized = json.dumps(events)
    assert "PRIVATE-ACTOR-STRATEGY" in serialized
    assert "JUDGE-REASONING" in serialized
    assert "ACTOR-FINAL-THOUGHT" in serialized
    # Later actor-history trimming cannot erase the append-only response archive.
    trimmed = make_reasoning_trimmer(0)(result.all_messages())
    assert "PRIVATE-ACTOR-STRATEGY" not in ModelMessagesTypeAdapter.dump_json(trimmed).decode()
    assert "PRIVATE-ACTOR-STRATEGY" in (tmp_path / "model-responses.jsonl").read_text()
    assert (
        "PRIVATE-ACTOR-STRATEGY"
        in ModelMessagesTypeAdapter.dump_json(result.all_messages()).decode()
    )


def test_reasoning_filter_preserves_other_fields() -> None:
    assert _without_reasoning(
        [
            {
                "parts": [
                    {"part_kind": "thinking", "content": "secret"},
                    {"part_kind": "text", "content": "keep"},
                ]
            }
        ]
    ) == [{"parts": [{"part_kind": "text", "content": "keep"}]}]


def test_recording_default_and_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(scopejudge_enabled=True)
    cfg.pop("record_reasoning")
    cfg["hide_actor_reasoning_from_judge"] = True

    def model(_cfg: AgentConfig) -> TestModel:
        return TestModel()

    monkeypatch.setattr("scopejudge.runtime._build_judge_model", model)
    caps = _capabilities(cfg, tmp_path)
    assert isinstance(caps[0], ScopeJudge)
    assert caps[0].recording_dir == tmp_path
    assert caps[0].hide_actor_reasoning is True
    assert isinstance(caps[-1], ResponseRecorder)
    cfg["record_reasoning"] = False
    caps = _capabilities(cfg, tmp_path)
    assert len(caps) == 1
    assert isinstance(caps[0], ScopeJudge)
    assert caps[0].recording_dir is None
