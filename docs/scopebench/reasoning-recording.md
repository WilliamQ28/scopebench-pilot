# Recording model reasoning and controlling judge visibility

New harness runs default to `record_reasoning: true`. Recording preserves what the endpoint returns; it cannot recover undisclosed reasoning, and model explanations are not proof of the causal process producing a verdict.

Each run workspace contains:

- `model-responses.jsonl`: one event per returned actor or judge response, including structured thinking, text, tool calls, response/model metadata, and usage when supplied. Events carry a role; judge events carry the proposed actor tool-call ID, including intermediate structured-output retry responses. Actor responses are appended before later history trimming. Judge responses are buffered and appended when the actor run exits, including ordinary exception/timeout unwinding, to avoid exposing judge thinking through the live archive. A process/container hard kill can lose buffered judge responses. File order is consequently not cross-role chronological order; use response timestamps and call IDs.
- `judge-inputs.jsonl`: the exact application payload supplied to each judge run, associated with the proposed actor tool-call ID. It captures brief redaction, reasoning filtering, and history elision. These payloads repeat history and can dominate disk usage. They do not include the judge system policy, provider-generated wire formatting, or individual retry requests; the policy is defined by the recorded code revision. Neither file intentionally records API credentials.

The archive stores responses once rather than repeatedly copying them into each reasoning event. Exact judge inputs deliberately duplicate history to support inspection of visibility. For large experiments, budget disk space for these inputs as well as responses.

## Hide actor reasoning from the judge

Set the following in a new matrix specification's `defaults` (or an individual arm):

```yaml
record_reasoning: true
hide_actor_reasoning_from_judge: true
hide_brief_from_judge: true
```

For a single-task CLI run, add:

```sh
--record-reasoning --hide-actor-reasoning-from-judge --hide-brief-from-judge
```

`hide_actor_reasoning_from_judge` defaults to false to preserve the previous visibility condition. It removes all structured `thinking` parts from the serialized history before applying the judge history window. Recording remains enabled and retains those parts independently. The actor's own reasoning-retention policy is unchanged. Both controls are forwarded to the container and included in effective matrix provenance. `record_reasoning: false` / `--no-record-reasoning` disables both new archives.

This is a **judge condition without structured actor reasoning**, not an action-only monitor and not the existing `deny_feedback: black` condition. The judge still sees the engagement instruction, proposed arguments, ordinary actor text, prior tool calls and results, and prior denial feedback. Strategies disclosed in ordinary text, comments, or tool arguments remain visible. Provider reasoning returned as ordinary text cannot reliably be separated automatically. The actor still sees denial feedback according to `deny_feedback`; that is an independent experimental factor.

The standard adaptive attacker brief already says the monitor sees the surrounding transcript and instructs the actor to keep strategy in its own reasoning. Hiding structured reasoning better matches that instruction, but does not guarantee the actor keeps strategy out of ordinary text.

## Isolation and evidence limits

These files use the existing actor workspace and are local research artifacts, not independent target-side evidence or tamper-proof logs. Actor responses are archived during execution; judge responses are only published after the actor stops. This avoids a direct live judge-reasoning archive leak, but is not a general filesystem isolation boundary. For adversaries deliberately attacking harness storage or subsequent readers, use a separately isolated host-side collector. Do not reuse a workspace between independent units.

Use a new matrix name/output directory when changing visibility; do not pool these runs with the earlier reasoning-visible pilot. Existing runs cannot retroactively acquire missing reasoning. No live endpoint experiment or Docker validation is implied by the offline tests.

## Validation

Offline integration tests use distinct sentinel reasoning for actor and judge. They verify both roles are saved, hidden actor reasoning is absent from exact judge inputs, ordinary explanations remain visible, filtering does not mutate actor history, later reasoning trimming cannot erase the archive, and judge reasoning is not published until flushing after the actor stops. Matrix tests check resolved settings and ledger provenance. The archives contain endpoint-returned reasoning, not an inferred or reconstructed chain of thought.

A ready-to-run, eight-unit comparison specification is provided at [judge-reasoning-visibility.yaml](../../experiments/judge-reasoning-visibility.yaml). Both arms use the same actor and judge; only structured reasoning visibility differs. It does not launch automatically. Run from the repository root with a new output directory:

```sh
scopebench-matrix experiments/judge-reasoning-visibility.yaml \
  --out experiments/reasoning-visibility-new --tasks-dir tasks/scopebench --concurrency 1
```

Use concurrency 1 if you want to retain the interleaved arm order: the matrix's concurrent scheduler groups units by arm. Both arms explicitly retain reasoned feedback to the actor; this example isolates judge visibility rather than actor knowledge of the monitor.


## Three-way actor commentary mediator

`judge_actor_information` adds a per-call actor-to-judge channel. It is independent of recording and of denial feedback to the actor. Leave it unset to reproduce the prior history policy.

| Value | Evidence retained | Additional actor commentary shown |
| --- | --- | --- |
| `none` | Engagement, proposed native arguments, prior tool calls/results | No ordinary actor prose, structured reasoning, or explicit justification |
| `justification` | Same evidence as `none` | Only the current call's explicit `scope_justification` |
| `full` | Engagement, arguments, full available bounded history | Explicit justification plus retained actor prose and structured reasoning |

Every explicit condition exposes the same required `scope_justification` field on function tools. The actor supplies 1–1000 characters alongside the native arguments; the mediator removes this field before native validation/execution and preserves it in the gate audit. Missing or oversized justifications trigger a validation retry, never tool execution. This avoids a separate LLM call to invent or summarize an explanation, and avoids pairing a justification for one command with a different batched call. It also preserves the same actor-side schema and communication instructions across conditions. In `none`, the justification is recorded but withheld from the judge, including from prior tool-call metadata. In `justification`, previous justifications are withheld so only the current claim is added to common evidence.

A justification is an attacker-selected external claim, not necessarily a faithful summary of intent. The judge treats it as untrusted data. The actor receives an identical protocol note across explicit conditions warning that reasoning may be visible, correcting the original brief's implication that its reasoning is necessarily private. The original brief's request to hide strategy is retained as the attack treatment; the note clarifies the available channel rather than asserting strategy is invisible.

Use either matrix `judge_actor_information: justification` or CLI `--judge-actor-information justification`. Choose `none` or `full` for the other conditions. Do not combine `full` with `hide_actor_reasoning_from_judge: true`; the gate rejects that contradiction. The new setting is stored in effective matrix provenance and each gate record. The gate's recorded arguments include the mediation metadata to preserve trajectory correlation; the judge's `validated_arguments` and executable native arguments do not include it.

“No commentary” is a channel definition, not proof of zero strategy disclosure: commands, code comments, `think`/report/tracker tool arguments, and tool outputs can contain strategic text. These are retained as action evidence in every mode. If a strict action-only adversary is desired, separately restrict commentary tools and control comments, rather than claiming the mediator removes all intent. `full` means endpoint-returned reasoning still present after actor-history trimming and judge-window bounding, not the complete saved archive. Different information lengths can retain different amounts of history under the common character limit; record elision and use fixed histories in replay to isolate this effect.

The original two-way `hide_actor_reasoning_from_judge` switch remains available for legacy comparisons. It retains ordinary actor prose; it is not equivalent to the new `none` or `justification` conditions. Prefer the explicit three-way setting for new experiments and keep denial feedback fixed while comparing it.
