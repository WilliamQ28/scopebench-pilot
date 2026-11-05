"""Verify shared information modes on the actual OpenAI-compatible request boundary."""

import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pytest_httpx import HTTPXMock

from agent_harness import HarnessDeps
from scopejudge.capability import ScopeJudge
from scopejudge.protocol import JudgeActorInformation, JudgeBackend
from scopejudge.runtime import _actor_prompt, _capabilities
from scopejudge.tests.test_agentjudge import config


@pytest.mark.parametrize("backend", ["agentjudge"])
@pytest.mark.parametrize("mode", ["none", "justification", "full"])
async def test_information_modes_reach_compute_api_without_cross_call_claim_leaks(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    backend: JudgeBackend,
    mode: JudgeActorInformation,
) -> None:
    wire_payloads: dict[str, dict[str, object]] = {}
    prior_action = "PRIOR_ACTION_" + "x" * 200 + "_UNABBREVIATED_SUFFIX"

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://compute.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer judge-test-key"
        body = cast("dict[str, object]", json.loads(request.content))
        assert body["model"] == "qwen38-27b"
        messages = cast("list[dict[str, str]]", body["messages"])
        assert messages[0]["role"] == "system"
        assert "untrusted actor claim" in messages[0]["content"]
        candidates = [
            m["content"]
            for m in messages
            if m["role"] == "user" and '"actor_information"' in m["content"]
        ]
        assert len(candidates) == 1
        payload = cast("dict[str, object]", json.loads(candidates[0].removeprefix("[user]: ")))
        args = cast("dict[str, object]", payload["validated_arguments"])
        wire_payloads[str(args["action"])] = payload
        assert args["budget"] == 7  # native validation defaults, not actor-supplied metadata
        assert "scope_justification" not in args
        assert payload["engagement_instruction"] == "ENGAGEMENT_RULE: only safe actions."
        assert payload["actor_information"] == mode
        assert "tools" not in body
        message = {
            "role": "assistant",
            "content": "<passing>true</passing><reason>fixture</reason>",
        }
        return httpx.Response(
            200,
            json={
                "id": "judge-response",
                "object": "chat.completion",
                "model": "qwen38-27b",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 12, "total_tokens": 135},
            },
        )

    httpx_mock.add_callback(respond, is_reusable=True)
    cfg = config()
    cfg["judge_backend"] = backend
    cfg["judge_base_url"] = "https://compute.test/v1"
    cfg["instruction"] = "ENGAGEMENT_RULE: only safe actions."
    cfg["judge_actor_information"] = mode
    cfg["judge_transcript_strategy"] = "rubric_only"  # must not discard explicit information
    cfg["record_reasoning"] = True
    cfg["attacker_brief"] = "ATTACKER_BRIEF_SENTINEL"
    cfg["hide_brief_from_judge"] = True
    capabilities = _capabilities(cfg, tmp_path)
    gate = cast("ScopeJudge", capabilities[0])
    count = 0
    executed: list[tuple[str, int]] = []

    def actor(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal count
        schema = info.function_tools[0].parameters_json_schema
        assert "scope_justification" in str(schema)
        count += 1
        if count == 1:
            return ModelResponse(
                parts=[
                    ThinkingPart("PRIOR_PRIVATE_THINKING"),
                    TextPart("PRIOR_ACTOR_PROSE"),
                    ToolCallPart(
                        "safe",
                        {"action": prior_action, "scope_justification": "PRIOR_CLAIM"},
                        tool_call_id="prior",
                    ),
                ]
            )
        if count == 2:
            return ModelResponse(
                parts=[
                    ThinkingPart("CURRENT_PRIVATE_THINKING"),
                    TextPart("CURRENT_ACTOR_PROSE"),
                    ToolCallPart(
                        "safe",
                        {"action": "ACTION_A", "scope_justification": "CLAIM_A"},
                        tool_call_id="a",
                    ),
                    ToolCallPart(
                        "safe",
                        {"action": "ACTION_B", "scope_justification": "CLAIM_B"},
                        tool_call_id="b",
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    async def safe(action: str, budget: int = 7) -> str:
        executed.append((action, budget))
        return "OBSERVATION_DATA"

    agent = Agent(
        FunctionModel(actor), tools=[safe], deps_type=HarnessDeps, capabilities=capabilities
    )
    result = await agent.run(_actor_prompt(cfg), deps=HarnessDeps(cwd=tmp_path))
    assert result.output == "done"
    assert sorted(executed) == sorted([(prior_action, 7), ("ACTION_A", 7), ("ACTION_B", 7)])
    assert len(httpx_mock.get_requests()) == 3
    for action, claim, other in [
        ("ACTION_A", "CLAIM_A", "CLAIM_B"),
        ("ACTION_B", "CLAIM_B", "CLAIM_A"),
    ]:
        payload = wire_payloads[action]
        serialized = json.dumps(payload)
        assert prior_action in serialized  # no SDK 50-character abbreviation in shared modes
        assert "OBSERVATION_DATA" in serialized
        assert "ATTACKER_BRIEF_SENTINEL" not in serialized
        assert ("CURRENT_PRIVATE_THINKING" in serialized) is (mode == "full")
        assert ("PRIOR_PRIVATE_THINKING" in serialized) is (mode == "full")
        assert ("CURRENT_ACTOR_PROSE" in serialized) is (mode == "full")
        assert ("PRIOR_ACTOR_PROSE" in serialized) is (mode == "full")
        if mode == "none":
            assert "actor_justification" not in payload
            assert claim not in serialized
        else:
            assert payload["actor_justification"] == claim
        if mode != "full":
            assert other not in serialized
            assert "PRIOR_CLAIM" not in serialized
    for record in gate.records:
        assert record.actor_information == mode
        assert record.judge_transcript_strategy is None
    gate.flush_recording()
    archived = [
        cast("dict[str, str]", json.loads(line))
        for line in (tmp_path / "judge-inputs.jsonl").read_text().splitlines()
    ]
    assert len(archived) == 3
    for event in archived:
        recorded = cast("dict[str, object]", json.loads(event["payload"]))
        if backend == "agentjudge":
            assert recorded["transcript_strategy"] is None
            assert recorded["actor_information"] == mode
            recorded = cast("dict[str, object]", recorded["information"])
        args = cast("dict[str, object]", recorded["validated_arguments"])
        assert recorded == wire_payloads[str(args["action"])]
