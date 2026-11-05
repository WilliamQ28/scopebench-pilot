"""Actor completion controls must not change judge settings or disappear on resume."""

import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.models.test import TestModel
from pytest_httpx import HTTPXMock

from agent_harness import HarnessDeps
from scopejudge import runtime
from scopejudge.tests.test_agentjudge import config


@pytest.mark.parametrize("budget", [None, 8192])
async def test_actor_token_budget_on_initial_resume_and_nudge_requests(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: int | None
) -> None:
    actor_requests: list[dict[str, object]] = []
    judge_requests: list[dict[str, object]] = []
    effects: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, object]", json.loads(request.content))
        if body["model"] == "qwen35-122b":
            actor_requests.append(body)
            assert request.headers["authorization"] == "Bearer actor-test-key"
            if len(actor_requests) == 1:
                message: dict[str, object] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "safe-1",
                            "type": "function",
                            "function": {"name": "safe", "arguments": "{}"},
                        }
                    ],
                }
            else:
                message = {"role": "assistant", "content": "done"}
        else:
            assert body["model"] == "qwen38-27b"
            assert request.headers["authorization"] == "Bearer judge-test-key"
            judge_requests.append(body)
            message = {
                "role": "assistant",
                "content": "<passing>true</passing><reason>safe</reason>",
            }
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )

    httpx_mock.add_callback(
        respond, url="https://compute.test/v1/chat/completions", is_reusable=True
    )

    async def safe() -> str:
        effects.append("executed")
        return "ok"

    acting_agent = Agent(
        deps_type=HarnessDeps,
        output_type=[str, DeferredToolRequests],
        tools=[Tool(safe, requires_approval=True)],
    )
    monkeypatch.setattr(runtime, "agent", acting_agent)
    cfg = config()
    cfg["actor_max_output_tokens"] = budget
    cfg["persist"] = True
    cfg["max_nudges"] = 1
    outcome = await runtime.run_agent(
        cfg, model=runtime._build_model(cfg), deps=HarnessDeps(cwd=tmp_path)
    )
    assert outcome["output"] == "done"
    assert effects == ["executed"]
    assert len(actor_requests) == 3  # initial, deferred approval resume, persistence nudge
    for body in actor_requests:
        assert body.get("max_completion_tokens", body.get("max_tokens")) == budget
    assert len(judge_requests) == 1
    assert (
        judge_requests[0].get("max_completion_tokens", judge_requests[0].get("max_tokens")) == 4096
    )


async def test_invalid_actor_budget_rejected_before_execution(tmp_path: Path) -> None:
    cfg = config()
    cfg["actor_max_output_tokens"] = 0
    with pytest.raises(ValueError, match="must be positive"):
        _ = await runtime.run_agent(cfg, model=TestModel(), deps=HarnessDeps(cwd=tmp_path))
