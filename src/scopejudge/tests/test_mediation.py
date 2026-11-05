"""Verify the three conditions share tools/evidence but separate commentary."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import Agent, ModelRetry, ToolDefinition
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from agent_harness import HarnessDeps
from scopebench.breach import inspect_gate
from scopejudge.capability import ScopeJudge
from scopejudge.mediation import evidence_history, mediation_tools, split_justification
from scopejudge.protocol import JudgeActorInformation, JudgeBackend
from scopejudge.runtime import _actor_prompt, _write_trajectory
from scopejudge.tests.test_gate import _cfg


@pytest.mark.parametrize("backend", ["agentjudge"])
@pytest.mark.parametrize("mode", ["none", "justification", "full"])
async def test_mediator_visibility_and_execution(
    tmp_path: Path, mode: JudgeActorInformation, backend: JudgeBackend
) -> None:
    calls = 0
    hits: list[str] = []

    def actor(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        assert "scope_justification" in str(info.function_tools[0].parameters_json_schema)
        calls += 1
        if calls <= 2:
            return ModelResponse(
                parts=[
                    ThinkingPart("PRIVATE_STRATEGY"),
                    TextPart("ACTOR_PROSE"),
                    ToolCallPart(
                        "safe",
                        {"value": "ACTION_DATA", "scope_justification": "EXTERNAL_CLAIM"},
                        tool_call_id=f"c{calls}",
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    async def safe(value: str) -> str:
        hits.append(value)
        return "OBSERVATION_DATA"

    gate = ScopeJudge(
        instruction="Only safe actions",
        model=(TestModel(custom_output_text="<passing>true</passing><reason>ok</reason>")),
        backend=backend,
        transcript_strategy="rubric_only",  # shared conditions override native filtering
        actor_information=mode,
        recording_dir=tmp_path,
    )
    agent = Agent(FunctionModel(actor), deps_type=HarnessDeps, tools=[safe], capabilities=[gate])
    result = await agent.run("HIDDEN_PROMPT", deps=HarnessDeps(cwd=tmp_path))
    assert result.output == "done"
    assert hits == ["ACTION_DATA", "ACTION_DATA"]  # metadata did not reach the native tool
    gate.flush_recording()
    inputs = [
        cast("dict[str, str]", json.loads(line))
        for line in (tmp_path / "judge-inputs.jsonl").read_text().splitlines()
    ]
    assert len(inputs) == 2
    for i in inputs:
        recorded = cast("dict[str, object]", json.loads(i["payload"]))
        assert recorded["actor_information"] == mode
        assert recorded["transcript_strategy"] is None
        messages = cast("list[dict[str, str]]", recorded["messages"])
        piped = cast(
            "dict[str, object]", json.loads(messages[2]["content"].removeprefix("[user]: "))
        )
        assert piped == recorded["information"]
        assert "untrusted actor claim" in messages[0]["content"]
        payload = json.dumps(recorded["information"])
        assert "ACTION_DATA" in payload
        assert ("EXTERNAL_CLAIM" in payload) is (mode != "none")
        assert ("PRIVATE_STRATEGY" in payload) is (mode == "full")
        assert ("ACTOR_PROSE" in payload) is (mode == "full")
        assert ("HIDDEN_PROMPT" in payload) is (mode == "full")
    assert "OBSERVATION_DATA" in inputs[1]["payload"]
    assert gate.records[0].actor_justification == "EXTERNAL_CLAIM"
    assert gate.records[0].actor_information == mode
    # Metadata remains in original call audit so inspect_gate can correlate it.
    assert "scope_justification" in gate.records[0].arguments
    decisions = tmp_path / "scopejudge-decisions.json"
    decisions.write_text("[" + ",".join(r.model_dump_json() for r in gate.records) + "]")
    _write_trajectory(
        result.all_messages(),
        model=FunctionModel(actor),
        cwd=tmp_path,
        session_id="test",
        scopejudge_enabled=True,
    )
    assert inspect_gate(decisions, tmp_path / "trajectory.json", None).status == "correlated"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"scope_justification": " "},
        {"scope_justification": 3},
        {"scope_justification": "x" * 1001},
        "[]",
    ],
)
def test_invalid_justification_rejected(args: str | dict[str, object]) -> None:
    with pytest.raises(ModelRetry):
        split_justification(args)


def test_json_justification_and_schema_collision() -> None:
    assert split_justification('{"scope_justification":"why","value":1}') == ({"value": 1}, "why")
    with pytest.raises(ValueError, match="reserved"):
        mediation_tools(
            [
                ToolDefinition(
                    name="bad",
                    parameters_json_schema={
                        "type": "object",
                        "properties": {"scope_justification": {}},
                    },
                )
            ]
        )


def test_evidence_history_strips_claims_and_marks_invalid_arguments() -> None:
    raw = [
        {
            "parts": [
                {"part_kind": "text", "content": "secret"},
                {"part_kind": "tool-call", "args": '{"cmd":"ls","scope_justification":"private"}'},
                {"part_kind": "tool-call", "args": '{"scope_justification":"private"'},
                {"part_kind": "tool-call", "args": "[]"},
            ]
        }
    ]
    result = str(evidence_history(raw))
    assert "private" not in result
    assert "secret" not in result
    assert "ls" in result
    assert "invalid_arguments" in result


@pytest.mark.parametrize("backend", ["agentjudge"])
def test_full_conflict_and_uniform_prompt(backend: JudgeBackend) -> None:
    with pytest.raises(ValueError, match="conflicts"):
        ScopeJudge(
            instruction="task",
            model=TestModel(),
            actor_information="full",
            backend=backend,
            hide_actor_reasoning=True,
        )
    prompts: list[str] = []
    for mode in ("none", "justification", "full"):
        cfg = _cfg()
        cfg["judge_actor_information"] = mode
        cfg["judge_backend"] = backend
        prompts.append(_actor_prompt(cfg))
    assert prompts[0] == prompts[1] == prompts[2]
    assert "do not assume reasoning is private" in prompts[0]
