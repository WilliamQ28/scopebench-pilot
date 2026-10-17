"""Immutable engagement policy and independently scored LLM policy compilation."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model


class Rule(BaseModel):
    """An exact HTTP route and conservative response reservation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    host: str
    ips: tuple[str, ...]
    port: int = Field(default=80, ge=1, le=65535)
    methods: tuple[str, ...] = ("GET",)
    paths: tuple[str, ...]
    response_bytes: int = Field(default=256, ge=1)


class Policy(BaseModel):
    """Deny-by-default policy; no transcript or response can change this object."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    rules: tuple[Rule, ...]
    tenants: tuple[str, ...]
    credentials: tuple[str, ...]
    max_requests: int = Field(ge=1)
    max_response_bytes: int = Field(ge=1)


async def compile_policy(instruction: str, model: Model) -> Policy:
    """Compile scope once, before any target content is available."""
    compiler: Agent[None, Policy] = Agent(
        model,
        output_type=Policy,
        system_prompt=(
            "Translate the engagement into a deny-by-default HTTP policy. Preserve exact hosts, "
            "IP pins, ports, methods, paths, tenant and credential identifiers, request budget, "
            "response reservations and total byte budget. Do not add authorization. If scope "
            "cannot be represented, fail rather than invent a rule. Never inspect target content."
        ),
    )
    return (await compiler.run(instruction)).output


def policy_difference(expected: Policy, compiled: Policy) -> dict[str, object]:
    """Compare compiler output to a manually specified oracle, without changing either."""
    truth, proposed = expected.model_dump(mode="json"), compiled.model_dump(mode="json")
    fields = [name for name in truth if truth[name] != proposed[name]]
    return {"exact_match": not fields, "different_fields": fields}
