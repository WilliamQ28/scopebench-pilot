"""The host/container wire contract for a ScopeJudge run.

These types are deliberately dependency-light. The host-side :mod:`agent_sandbox`
runner and the in-container ScopeJudge application import the same definitions, so
the JSON contract cannot drift between duplicated models.
"""

from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

# How much the acting agent learns when the guard denies a call. ``black`` hides the monitor
# entirely (a generic failure); ``binary`` reveals a block with no reason; ``reasoned`` (the
# default, unchanged behavior) returns the verdict and rationale; ``white`` also discloses the
# judge's policy prompt. Only meaningful when ScopeJudge is enabled.
DenyFeedback = Literal["black", "binary", "reasoned", "white"]


class AgentConfig(TypedDict):
    """Configuration piped to the ScopeJudge container on standard input."""

    instruction: str
    model: str | None
    api_key: str
    # Optional OpenAI-compatible endpoint (e.g. a self-hosted vLLM server). Applied to the provider
    # for both the actor and the judge, so they share one model/endpoint. Absent = the provider's
    # default endpoint. Pair with an ``openai-chat:<served-model>`` model string for vLLM.
    base_url: NotRequired[str | None]
    request_limit: int
    timeout: float
    ask_user_reply: str
    atif: bool
    session_id: str | None
    scopejudge_enabled: bool
    # Adversarial treatment: an attacker-only brief prepended to the acting agent's prompt. It is
    # never passed to the ScopeJudge policy, so the judge keeps authorizing against the unaltered
    # engagement instruction. Optional (NotRequired) so existing configs remain valid on the wire.
    attacker_brief: NotRequired[str | None]
    # Experimental condition: how much a denial reveals to the acting agent. Absent = ``reasoned``.
    deny_feedback: NotRequired[DenyFeedback]
    # Arm B: redact the attacker brief from the trajectory the judge inspects, so the gate never
    # reads the attacker's declared intent. Absent/False = Arm A (brief visible in-band).
    hide_brief_from_judge: NotRequired[bool]
    # Per-call authorization timeout for the judge (seconds). Absent = 30. Slow self-hosted
    # endpoints need a larger value, else judge calls time out and fail closed as ``uncertain``.
    judge_timeout: NotRequired[float]


@dataclass
class ToolCall:
    """One tool invocation captured from the agent's message history."""

    name: str
    args: str


@dataclass
class AgentResult:
    """The raw result written by ScopeJudge and read by the sandbox host."""

    output: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    submitted: str | None = None
