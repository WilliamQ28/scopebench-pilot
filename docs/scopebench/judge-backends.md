# Dreadnode live judge

`--scopejudge` enables live tool-call gating. AgentJudge is selected automatically; this explicit flag is also accepted:

| Flag | Implementation |
| --- | --- |
| `--judge-backend agentjudge` | The default and sole live backend: vendored Dreadnode AgentJudge 2.0.53 prompt/action core, adapted to this harness. |

AgentJudge uses the same pre-execution wrapper, validated native tool arguments,
per-call timeout, denial-feedback controls, gate records, and response recording.
Only an explicit allow executes the tool. Errors never disable subsequent gating.
Actor model selection, reasoning retention, and tool-result compaction remain
independent of this choice.

## Compute Canada / Vulcan

Use `openai-chat:<served-model-name>` for the OpenAI-compatible Chat Completions
API. AgentJudge uses the existing PydanticAI provider factory and explicit
base URL/key; the new backend does not introduce Dreadnode/LiteLLM model routing,
provider credentials, or a separate platform login. Set `OPENAI_API_KEY` through
your existing environment or `.env`; do not put it in command arguments.

```sh
.venv/bin/python -m scopebench \
  --tasks-dir tasks/scopebench \
  --task traversal-overreach-mixed-path \
  --instruction program-brief-v3 \
  --scopejudge --judge-backend agentjudge \
  --judge-transcript-strategy intent_plus_calls \
  --model openai-chat:qwen35-122b \
  --base-url https://inference.vulcan.alliancecan.ca/v1 \
  --judge-model openai-chat:qwen38-27b \
  --judge-base-url https://inference.vulcan.alliancecan.ca/v1 \
  --judge-timeout 120 --no-trajectory-judge --atif
```

Replace the served model names if necessary. Without `--judge-model`, the judge
shares the actor model, endpoint, and key. With an independent judge, an explicit
`--judge-base-url` takes precedence. When both
actor and judge use `openai-chat:`, an omitted judge URL inherits the actor URL.
Other-provider judges do not inherit it. Provider keys continue to be resolved by the existing host runner.

## AgentJudge transcript choices

`--judge-transcript-strategy` applies to native `agentjudge` runs when
`--judge-actor-information` is unset:

- `intent_plus_calls` (default): task/system messages and prior tool calls;
  excludes tool outputs and assistant prose/thinking.
- `intent_only`: task/system messages; excludes prior calls and outputs.
- `rubric_only`: policy plus proposed call; excludes conversation history.
- `full`: prior task, calls, outputs, and available assistant prose/thinking.

As in the upstream hook, history ends before the actor response that proposed the
current call batch. The proposed call is supplied separately with full validated
arguments. The pinned SDK text rendering shortens prior-call argument strings to
50 characters. This is an important information difference from the removed legacy gate.

The legacy custom live judge has been removed. Explicit `current` backend settings
are rejected; historical result archives retain their original provenance. AgentJudge
includes the upstream default safety floor and appends the engagement as its rubric.

## Shared information conditions

`--judge-actor-information full` and `--judge-actor-information justification`
select explicit information treatments through a common payload builder:

| Mode | Information supplied to AgentJudge |
| --- | --- |
| `none` | Engagement, proposed native tool/validated arguments, bounded tool-call/result evidence; no separate actor claim or ordinary prose/thinking. |
| `justification` | The same evidence plus this call's explicit `scope_justification`; ordinary prose/thinking and prior justification metadata are excluded. |
| `full` | Engagement, proposed native tool/validated arguments, this call's justification, and the full available bounded actor history, including current-response prose/thinking. |

All explicit modes expose the same required justification field and actor prompt,
using the mediator. It extracts the field before native tool
validation/execution and forwards the current claim only when the mode permits.
Claims are explicitly marked as untrusted and cannot expand authorization. Scope
explanations embedded in tool arguments or results remain tool evidence, so the
justification condition is not guaranteed to contain no reasoning-like text.

For AgentJudge, these shared modes **override native transcript selection**.
The complete common JSON information payload is supplied to its upstream prompt
builder, alongside the proposed call. Native 50-character history abbreviation
and omission of the current proposing response do not apply to shared modes.
The default rubric and XML verdict parsing come from the pinned upstream core. This shared experimental mode is an
adaptation, not a transcript option supplied by upstream Dreadnode.

Recorded AgentJudge inputs contain `actor_information`, the exact common
`information` object, and the application messages; `transcript_strategy` is null
when native filtering was not used. Gate records carry the same mode and null
native strategy. Matrix settings retain the requested flags. Shared history still
respects actor reasoning retention, tool-result compaction, exact-brief redaction,
and the same 120,000-character judge-history target for AgentJudge. Full does
not restore thoughts already removed from actor history. Combining shared `full`
with hidden actor reasoning is rejected for AgentJudge.

For example, append this to a normal run:

```sh
--scopejudge --judge-actor-information justification
```

Replace `justification` with `full` for the full-information treatment. Leave the
information flag unset to use native AgentJudge transcript filtering.

Backend and strategy are recorded in effective matrix configurations, evaluation
attributes, and gate records. AgentJudge judge-input archives also record the
upstream version, effective policy hash, and application message contents. These
archives remain local evidence, not independent proof of target-side effects.

## Adaptation and limits

See [vendor provenance](../../src/scopejudge/vendor/NOTICE.md) and the included
Apache-2.0 license. This is the pinned judging core, not the full Dreadnode
runtime or an upstream-certified integration. Deliberate integration choices:

- Keep our execution wrapper rather than importing the SDK hook's authentication
  failure disabling behavior. ASK is uncertain/blocked in unattended runs.
- Use our fixed history character budget, preserving initial task messages,
  rather than the SDK's model-dependent character estimate. This is not a hard
  token limit: protected task/policy and proposed arguments can exceed it.
- Disable Anthropic cache-control markers on the Compute Canada transport.
  Server-side prefix caching may still exist; verify cache usage in receipts.
- Preserve our 4,096-token judge output allowance and request timeout. Neither
  is a claim about how many tokens a particular request will consume.
- Output-summary strategy, human approval, and tool-name bypass lists are not
  included. No additional summary model calls are made.

Changing transcript information can change authorization accuracy. Compare input
and output tokens, latency, cache reads, legitimate-task completion, and prohibited
effects before drawing efficiency or robustness conclusions.

## Actor latency controls

Backend selection does not make the actor model faster. The shared information
modes retain the bounded evidence budget; native calls-only
AgentJudge and shared full-information AgentJudge are different cost conditions.
Information-piping smoke tests establish no comparative defense-strength result.

The following controls can reduce actor context or bound unusually long outputs:

| Flag | Behavior |
| --- | --- |
| `--trim-reasoning-keep-recent 1` | Keep structured reasoning in the latest actor response and remove older reasoning when the message still has other content. Does not disable generating new reasoning. |
| `--max-tool-result-chars 4000` | Keep shorter head/tail excerpts of oversized tool outputs; full outputs are cached for rereading. Existing flag-bearing-output exemption still applies. |
| `--actor-max-output-tokens 8192` | Bound each actor completion, including reasoning on the Compute Canada chat endpoint. Applies to initial, approval-resume, and persistence-nudge requests; does not change judge settings. |

For example, append these to a normal run, with AgentJudge:

```sh
--trim-reasoning-keep-recent 1 \
--max-tool-result-chars 4000 \
--actor-max-output-tokens 8192
```

These are optional trial settings, not a measured optimal profile or a promised
speedup. Defaults are unchanged: reasoning trimming and output compaction are off
unless configured; the actor completion limit is left to the model/endpoint.
The existing full-suite YAML explicitly selects three recent reasoning responses
and a 12,000-character result threshold. Matrix specs accept
`actor_max_output_tokens`, `trim_reasoning_keep_recent`, and `max_tool_result_chars`;
effective configurations record them, and actor output cap/reasoning retention
also appear in evaluation attributes.

An output ceiling may leave typical short responses unchanged, or truncate a long
response before its tool call. Smaller histories can remove useful evidence and
change what the full-information defender sees. Apply each profile consistently
across treatment arms and gate-off capability controls; measure actor latency,
input/output tokens, completion-limit errors, and objective completion before
replacing a baseline. More concurrent tasks affect throughput and may increase
individual latency on the shared endpoint.
