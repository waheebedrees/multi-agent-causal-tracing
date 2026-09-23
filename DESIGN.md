
---
# Design

## The core question

Two actions happening close in time is **correlation**. One agent authorizing
and causing another's action is **causation**. This codebase captures the
second as a first-class, queryable relation and never infers it from
timestamps.

## Three layers, three responsibilities

| Layer | What it stores | Key | Question it answers |
|---|---|---|---|
| **OTel spans** | Physical timing, span tree | `trace_id`, `span_id` | Where did the work happen? |
| **Event store** | Immutable business events | `event_id`, `caused_by` | What caused what? |
| **Causal graph** | Projection of the store | — | Show the whole run as a DAG |

The layers are independent:

- Every agent is its **own OTel trace** (a fresh root, `parent_id: null`).
- The event store spans agents via `session_id`, not `trace_id`.
- The graph is a pure projection of the event store.

## How causality is preserved

**1. Every event carries an explicit cause.**
`CausalEvent.caused_by` points at another event's `event_id`. Nothing is
inferred from `parent_span_id` — that only says "ran inside the same span,"
which is *structural*, not causal.

The two relations are kept separate in the graph:

- `causes` edges come from `caused_by` — the semantic relation.
- `contains` edges come from `parent_span_id` — the structural relation.

The Mermaid export draws them differently (thick vs. dotted) so a reader can
tell at a glance which is which.

**2. `emit` requires an explicit parent at branch points.**
The helper defaults to the last emitted event (`parent_event_id`), which is
correct for a linear sequence but wrong at any fork. Every branch — LLM vs.
tool vs. object — passes its own trigger explicitly. This eliminated a phantom
`llm.responded → tool.requested` edge during development; the fallback is
retained only for strictly linear spans.

**3. Cross-agent edges are stated, not inferred.**
Each agent is its own OTel trace. The receiving agent's root span carries a
`Link` to the sender's `PRODUCER` span with `rel="follows"` and
`messaging.from=<sender>`. `parent_id` is `null` on every agent root — no agent
is a descendant of another. This is the OTel-blessed model for async message
boundaries (`PRODUCER` / `CONSUMER` + `Link`).

**4. Correlation uses `session_id`, not `trace_id`.**
`trace_id` is per-agent (physical). The business-level correlation key is
`workflow.session_id`, stamped on every event and every agent span. Cost
aggregation and workflow-wide queries filter on `session_id`; the causal walk
uses neither — it follows `caused_by` across trace boundaries.

**5. Authorization is bound to the same key.**
Every delegation carries a signed JWT whose `sid` (session) and `task_hash`
match the parent context, whose `aud` is the recipient agent, and whose `iss`
is the sender. This turns "authorized" from an assumption into a verifiable
claim. Verification failures surface as `CONTRACT v1 #7` violations on the
receiving agent's span.

**6. Storage is append-only with a monotonic `sequence_num`.**
Reconstruction is deterministic and cannot be rewritten after the fact.
Wall-clock time is present on every event but never used for ordering.

## What the four-agent run demonstrates

Final run: **33 events, 32 `causes` edges, 13 `contains` edges, 5 traces,
total cost $0.0093.**

- The graph is a single connected DAG across five OTel traces.
- `causal_chain` walks the full four-hop delegation plus the reply chain
  without a single `trace_id` — it uses `caused_by` throughout.
- `cost_report` aggregates across all five traces via `session_id`, breaking
  down by agent and by tool.
- `contains` edges stay within a single actor (span hierarchy never crosses
  traces) — visible as dotted arrows that never leave a color group.

## Limitations

1. **In-process bus.** `MessageBus` dispatches synchronously. Cross-process
   transport would need the span context serialized (`traceparent` header via
   `propagate.inject` / `extract`) instead of passing the `SpanContext` object.

2. **Ephemeral signing key.** `_load_or_generate_signing_key` creates a fresh
   Ed25519 key per process unless `SIGNING_KEY_PEM` is set. Fine for a trial; a
   real deployment needs a KMS or JWKS endpoint.

3. **`emit` fallback.** The implicit `parent_event_id` default is a convenience
   that can produce false causal edges if a caller forgets to pass `caused_by`
   at a branch point. Every current caller is explicit. A stricter API would
   require the argument and remove the fallback.

4. **Single-process OTel exporter.** Spans go to stdout via
   `ConsoleSpanExporter`. A real deployment exports to a collector (OTLP),
   giving retention and cross-service correlation.

5. **Workflow-level attributes are per-span.** `workflow.session_id` and
   `workflow.root_trace_id` are set on every span explicitly. The cleaner
   approach is a per-workflow OTel resource; the current code favors
   explicitness over DRY.

6. **Graph size.** The ASCII exporter is O(N) and fine for hundreds of events;
   DOT / Mermaid output scales to a few thousand. Beyond that, push to Neo4j
   or an analytical store.

7. **Failure path is wired but not exercised in the sample run.** `call_tool`
   emits `tool.failed` and re-raises; `run_workflow` records `task.failed`
   caused by the actual last event, not the root. Running
   `run_workflow(store, "fail")` exercises it.

## Scaling to a larger multi-agent environment

**What stays:**

- The three-layer split (physical spans / logical events / semantic graph).
- `caused_by` as the load-bearing field on every event.
- `session_id` (business) ≠ `trace_id` (physical).
- `PRODUCER` / `CONSUMER` + `Link` for cross-agent edges.

**What changes first:**

1. **Context propagation over the wire.** Replace the in-process bus with a
   real transport (HTTP, gRPC, Kafka) and use W3C `traceparent` propagation.
   The `Link` becomes the only cross-service artifact, which is what OTel
   expects.

2. **Central signing key + audience registry.** The token model is already
   correct; it needs a key server and a per-agent public-key registry instead
   of a shared in-process signing key.

3. **Durable event store.** SQLite → Postgres (or similar) with `session_id`
   and `caused_by` indexed. The schema is unchanged; the queries are already
   written.

4. **Graph service.** Move the causal graph out of memory into a graph
   database or a materialized view, so cross-session and cross-tenant queries
   are possible. Edge semantics don't change.

5. **Policy layer.** Add `authorized_by` and `scope` fields to each event so
   "was this action authorized, by whom, to what extent" is answerable without
   re-verifying tokens — needed once tokens are short-lived.

6. **Backpressure and out-of-order events.** With a real bus, events may
   arrive late. `sequence_num` is currently per-store; a distributed version
   needs a per-session logical clock (Lamport or vector) so causality survives
   reordering.

## The five-agent question

> If five agents each perform authorized actions, what information would you
> need to capture to determine whether those actions were part of one
> coordinated workflow rather than five unrelated events?

You need a **directed causal edge on every action**, not just shared context.
Concretely:

- A stable `agent_id` per actor (logical, not process-scoped).
- A run-scoped `session_id` propagated to every agent.
- A `caused_by` field on every event, pointing at the specific upstream event.
- A signed delegation token bound to the same `session_id` and to the specific
  subject / audience / task, so authorization is verifiable, not asserted.
- A monotonic ordering (`sequence_num`) so reconstruction is deterministic.
- Immutable, append-only storage.

**The test:** the transitive closure of `caused_by` from every action must
terminate at a single root event, and every cross-agent edge must be
corroborated by a delegation token whose session matches. Five agents are
coordinated iff both hold. Shared `session_id` alone is correlation; only the
causal pointer and the token make it coordination.

That is exactly what this codebase implements, at four agents plus an
orchestrator, on one process.