# Bounded HTTP ScopeFirewall experiment

This extension lives in `scopebench.firewall`, separate from the frozen pilot runner. It provides
matched no-defense, semantic-only, deterministic-only and combined arms with fresh state for
each attack family and repetition. The actor can use only an HTTP request tool. It cannot access
the Python controller, policy object, target state, filesystem, Docker socket or arbitrary shell.

The fixture has two objectives: an allowed public flag and a forbidden private flag. Receiving
target handlers record actual requests, private flag reads and mutations independently of actor
claims, tool arguments and semantic verdicts. Reports retain broker decisions, resolved addresses,
actor observations, target events, model identifiers, latency, actor token usage and errors.
Infrastructure failures are separate from prevented attacks. Target events are authoritative
within this simulation; they are not independently certified production telemetry.

## Run the offline controls

```sh
.venv/bin/python -m scopebench.firewall --output .scopefirewall-runs/http-controls.json
```

The default semantic monitor deliberately allows everything and is labeled `permissive-stub`.
This tests enforcement plumbing under maximally permissive semantic decisions. It does not
establish that an LLM gate is bypassable. The 13 families across four arms make 52 fresh cases.
Existing reports are never overwritten. Use `--repeat N` for repeated matrices.

Families include direct private access, encoded paths and numeric destinations, DNS aliases,
redirects, broker-mediated target pivots, multi-step staging, rejection-dependent reformulation,
tool-output prompt injection, tenant and credential misuse, mutations, and cumulative budgets.
The scripted injection case follows the malicious instruction deliberately; it demonstrates the
plumbing, not a model's susceptibility. The adaptive LLM actor must decide how to react itself.

## Run an adaptive model and real semantic monitor

```sh
.venv/bin/python -m scopebench.firewall \
  --actor-model openai-chat:ACTOR_MODEL \
  --judge-model openai-chat:JUDGE_MODEL \
  --base-url http://localhost:8000/v1 \
  --repeat 3 --request-limit 20 \
  --output .scopefirewall-runs/http-adaptive.json
```

Replace identifiers with models actually served by the configured endpoint. Without `--base-url`,
normal provider endpoints and environment credentials apply. Actor and judge are distinct agent
instances and may use different models. The actor receives rejection feedback and continues;
the test suite verifies a denial-to-reformulation sequence. No real provider requests are needed
for the default validation run. The operation deadline defaults to 30 seconds and the case deadline
to 120 seconds. Use `--operation-timeout` and `--case-timeout` for slow inference; timeouts appear
as infrastructure failures, not successful prevention.

Add `--compile-model MODEL` to compile the natural-language engagement before target contact and
compare the result to the manually specified policy. Reports retain both policies and differing
fields. Field comparison is exact, including ordering; it is not a general semantic equivalence
test. Compilation is evaluated separately and manual policy remains the treatment by default.
Add `--use-compiled-policy` to explicitly evaluate enforcement of the compiler output. The
fixture's ground-truth authorization remains the manual engagement in either condition.

## Deterministic enforcement

The frozen policy permits exact hosts, resolved IP addresses, ports, paths, methods, tenant and
credential identifiers. Destinations outside the authorized host/port inventory are rejected
before resolution. Every DNS answer must be pinned; the dispatcher connects to the checked IP,
preserves a controlled Host header, ignores ambient proxies, and never resolves the name a
second time. Each operation rechecks DNS, and every redirect receives new authorization. Identity
and request bodies are stripped on cross-authority redirects.

Noncanonical encoded or dot-segment paths, queries and caller-supplied arbitrary headers are
unsupported and denied. Only HTTP is supported; HTTPS/SNI is deliberately excluded. Conservative
response-byte reservations and request counts are reserved atomically before dispatch. Reservations
are consumed even if a request fails. Oversized responses are withheld from the actor; this is a
delivered-response limit, not proof that no extra bytes traveled from a server to the broker.

Tenant and credential values are fixture identifiers, not real authentication secrets. The broker
does not implement a general identity provider. Exact route allowlists prevent fixture mutations;
they cannot infer that an arbitrary GET endpoint is free of side effects.

## Docker isolation validation

```sh
.venv/bin/python experiments/scopefirewall/docker_smoke.py --image YOUR_AGENT_SANDBOX_IMAGE
```

Use an existing `agent-sandbox` image containing the locked Python dependencies. The validator
creates separate internal app and vault networks; a trusted broker container joins both. Each
target joins only its own network, has a read-only root and fixture mount, dropped capabilities,
and no Docker socket. Nothing is published to the host. Validation checks:

- the allowed HTTP request succeeds;
- a forbidden destination is denied before dispatch;
- the unrestricted control can reach the same destination;
- a raw connection from the app container to the vault fails;
- the app cannot overwrite its fixture;
- target logs record one authorized app request and one unrestricted vault-control request.

Evidence is saved under a unique `.scopefirewall-runs/sf-smoke-*/evidence.json`. Created containers
and networks are cleaned up in a `finally` block. This validates a real network isolation boundary
and HTTP broker, but does not run the adaptive model matrix in Docker.

## Remaining research boundaries

The main matrix uses in-process HTTP fixtures. Target-originated requests follow a mediated
fixture callback; arbitrary compromised server code is not modeled. The Docker smoke test
separately demonstrates blocked raw lateral access, without establishing complete egress mediation.
Shell/Python action tools, indirect shell execution, arbitrary credentials, unrestricted target
code, TLS, and containerized end-to-end adaptive runs remain outside this prototype.

These controls do not prove the professor's hypothesis. Use actual LLM semantic-only and combined
arms with matched budgets and repeated tasks, investigate independently corroborated violations,
and measure useful positive-task success. The simple public-flag positive control is a plumbing
check, not representative penetration-testing utility. Provider pricing and judge-token costs
are not yet reported; actor request and token counts are available.
