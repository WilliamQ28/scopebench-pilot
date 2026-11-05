# AgentJudge source provenance

Dreadnode SDK 2.0.53, Apache-2.0. Copyright Dreadnode contributors.
Source: https://pypi.org/project/dreadnode/2.0.53/
Source archive SHA-256: 4c128ba5fe02a644410649a5950430981cf90776b4a6f24d3bd83fe44e5021b9

Vendored subset: ProcessJudge._build_messages, _extract_action, and
_render_proposed_call from dreadnode/agents/process_judge.py, plus the exact
system prompt and default rubric from policies/rubrics/process/default.yaml.
The unused SDK Message import is removed; annotations use local text-only
containers. Message and ToolCall text rendering follows the SDK text path,
including shortening prior-call arguments to 50 characters. The proposed
call always contains full validated arguments. The upstream regex reason
fallback is used by our adapter.

This is an adapted AgentJudge judging backend, not the complete Dreadnode
runtime or an upstream-certified integration. The transport uses our existing
PydanticAI provider configuration. Our wrapper keeps timeouts, fail-closed
behavior, call correlation, and buffered response recording. ASK becomes
uncertain/blocked in unattended runs. No auth-error gate disabling, human
approval bridge, or tool-name bypass lists are imported. Anthropic-specific
cache markers are disabled for Compute Canada's OpenAI-compatible endpoint.
The supported transcript strategies are rubric_only, intent_only,
intent_plus_calls (default), and full; output summarization is not included.
History uses our fixed character budget for comparable judge inputs.

Explicit judge_actor_information experiments use the same bounded JSON payload
used by the removed legacy backend, overriding native transcript filtering. This includes
current-response reasoning in full mode and a separately labeled, untrusted
per-call justification in justification/full modes. Native history abbreviation
is not applied to these shared experimental payloads. The added actor-claim
trust instruction is an integration policy, not an upstream default.
