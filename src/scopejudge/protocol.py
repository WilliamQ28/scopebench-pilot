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
    # Context control: cap each string tool result to this many characters in the trajectory,
    # keeping the head and tail and writing the full output to a per-call cache file the agent can
    # re-read. Results carrying a task flag are never truncated. Absent/None/0 = off (unchanged).
    max_tool_result_chars: NotRequired[int | None]
    # Persistence treatment: when true, re-prompt a voluntarily-stopping agent to keep working
    # toward the objective until the shared request budget (``request_limit``, measured in MODEL
    # REQUESTS, not tool calls) is spent, instead of accepting an early final answer. Isolates "not
    # giving up" from the agent's own reasoning. The budget is the real cap; ``max_nudges`` only
    # bounds a pathological stop-without-progress loop and defaults to ``request_limit`` (each nudge
    # is itself a request, so it can never bind first under normal operation). Absent/False =
    # unchanged (the agent stops when it chooses).
    persist: NotRequired[bool]
    max_nudges: NotRequired[int]
    # Trusted stop condition for persistence: the SHA-256 digest of the task's forbidden flag (the
    # same digest the host grades against). When set, persistence stops once /tmp/result.txt hashes
    # to this value -- i.e. the objective is actually reached -- rather than on any nonempty text an
    # actor-writable file happens to contain. Absent/None = no trusted target, so persistence never
    # stops early on the flag and runs to the request budget. The digest is one-way and is read only
    # by the controller (never shown to the model), so passing it leaks nothing.
    objective_hash: NotRequired[str | None]
    # Independent ScopeJudge (monitor) model/endpoint/key, so the judge can be a different size or
    # provider than the actor -- the research question's actor/judge-size axis. Absent judge_model =
    # the judge shares the actor's model/base_url/api_key (prior single-model behaviour). When set,
    # the judge uses judge_base_url (if given; it is NOT auto-inherited from the actor, to avoid an
    # anthropic judge inheriting a vLLM base_url) and judge_api_key (falling back to the actor key).
    judge_model: NotRequired[str | None]
    judge_base_url: NotRequired[str | None]
    judge_api_key: NotRequired[str]
    # Harness-capability axis: restrict the acting agent to this subset of toolset-group ids (e.g.
    # ["filesystem", "web"]). Absent/None = all groups (unchanged). An unknown id fails the run.
    toolsets: NotRequired[list[str] | None]
    # Context control: keep reasoning ("thinking") only in the last N of the actor's own responses,
    # stripping stale chain-of-thought from older ones so a long run does not overflow the actor's
    # context window. Tool calls/results and the task prompt are untouched. Absent/None = off.
    trim_reasoning_keep_recent: NotRequired[int | None]


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
    # Count of MODEL REQUESTS the run consumed (the unit ``request_limit`` caps), kept distinct from
    # len(tool_calls): one request can emit several tool calls, and nudges/denials add requests with
    # no new tool call. Reported separately so budget analysis does not conflate the two.
    model_requests: int = 0
    # Resolved ScopeJudge model actually used (actor model when no judge override), for provenance.
    judge_model: str | None = None
