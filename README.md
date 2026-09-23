# Multi-Agent Causal Tracing — Trial

A minimal four-agent delegation workflow instrumented with **OpenTelemetry**, an **append-only causal event store**, and a **causal graph projection**, built to demonstrate that causation across autonomous agents can be distinguished from mere temporal correlation.

The trial asks: *can we determine not only that multiple events happened near each other in time, but that one agent action actually caused or delegated the next?* This implementation answers yes, and shows how.

---

## What it does

A user task flows through four agents in a delegation chain:

```
User Task
   │
   ▼
workflow.root (orchestrator)
   │  delegates
   ▼
agent_a ──llm──> delegated ──▶ agent_b ──llm──> tool ──> object ──▶ agent_c
                                                                     │
                                                                     ▼
                                                                  agent_d
                                                                     │
                                                                     ▼
                                                                  returns
```

Every agent:
- runs an LLM call (`llm.requested` → `llm.responded`)
- invokes a tool (`tool.requested` → `tool.responded`)
- creates an artifact with declared provenance (`object.created`)
- delegates to the next agent with a signed JWT (except the leaf, which replies upward)

Three independent views of the same run are produced:

| View | What it shows | Where it lives |
|---|---|---|
| **OpenTelemetry spans** | Physical timing, one trace per agent, cross-agent edges as `Link` | stdout (ConsoleSpanExporter) |
| **Causal event store** | Immutable business events with explicit `caused_by` | SQLite (`:memory:` in the demo) |
| **Causal graph** | Projection of events into a directed graph | Mermaid / DOT / ASCII |

---

## Requirements

- Python 3.10+
- `opentelemetry-sdk`, `opentelemetry-api`
- `networkx`
- `pyjwt`, `cryptography`
- Optional: `matplotlib` (for PNG export), Graphviz (for DOT rendering)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install opentelemetry-sdk opentelemetry-api networkx pyjwt cryptography
```

## Run

```bash
python main.py
```

This produces:
1. A JSON span dump (one object per span, printed when each span ends)
2. The human-readable causal chain of the final object
3. A cost report aggregated by `session_id`
4. `causal_graph.mmd` (Mermaid) and `causal_graph.dot` (Graphviz)

### Rendering the graph

- **Mermaid:** paste `causal_graph.mmd` at <https://mermaid.live>, or open it in VS Code with a Mermaid preview extension
- **Graphviz:** `dot -Tsvg -o causal_graph.svg causal_graph.dot`
- **PNG:** uncomment `builder.save_png("causal_graph.png")` in `main()`

---

## Repository layout

| File | Purpose |
|---|---|
| `message_bus.py` | message bus |
| `agents.py` | Agents|
| `models.py` | `CausalEvent`, `CostReport` |
| `event_store.py` | Append-only event store (SQLite) |
| `helpers.py` | ID/hash/JWT/DAG helpers |
| `tracing.py` | OpenTelemetry provider + exporter |
| `graph.py` | `CausalGraphBuilder`, Mermaid/DOT/ASCII exporters |
| `audit.py` | `causal_chain`, `cost_report` |
| `main.py` |  workflow driver |

---

## Sample output

```
trace_id   = 628d74029d19e764d15e1eb73df7129d
session_id = b25de8e3c4ad

obj-e16efd0d85d0 (claim)
  <- agent_d (09bccce8) agent_d.llm.requested  model=gpt-4o-mini
     (2c2dc1f0) agent_d.llm.responded cost=$0.0021
  <- agent_d (cf98f8e0) agent_d.tool.requested  tool=finalize_data
     (e16efd0d) agent_d.tool.responded cost=$0.0002
  <- agent_d (22ca8753) agent_d.object.created
    <- agent_d (b90aeed1) agent_d.activated
      <- agent_c (78d005e9) agent_c.delegated
        <- agent_c (9ef1f819) agent_c.llm.responded
          <- agent_c (0a716557) agent_c.llm.requested
            <- agent_c (ad90948f) agent_c.activated
              <- agent_b (75c93043) agent_b.delegated
                <- agent_b (bdbd1db9) agent_b.llm.responded
                  <- agent_b (9677857f) agent_b.llm.requested
                    <- agent_b (e902180a) agent_b.activated
                      <- agent_a (fedf64e5) agent_a.delegated
                        <- agent_a (daab7a3a) agent_a.llm.responded
                          <- agent_a (c6a9c741) agent_a.llm.requested
                            <- agent_a (f3b4db21) agent_a.activated
                              <- orchestrator (0e7b59dd) task.started
```

### Cost report

```
============================================================
COST REPORT - trace b25de8e3c4ad...
============================================================
  LLM calls:    4
  Tool calls:   3
  Total tokens: 4,800
  Cache hits:   0
  LLM cost:     $0.0084
  Tool cost:    $0.0009
  Total:        $0.0093
  Breakdown:
    [llm ] agent_a    gpt-4o-mini      $0.0021
    [llm ] agent_b    gpt-4o-mini      $0.0021
    [tool] agent_b    lookup_data      $0.0003
    [llm ] agent_c    gpt-4o-mini      $0.0021
    [tool] agent_c    transform_data   $0.0004
    [llm ] agent_d    gpt-4o-mini      $0.0021
    [tool] agent_d    finalize_data    $0.0002
============================================================
```

### Causal graph summary

```
  events          : 33
  causes edges    : 32
  contains edges  : 13
  agents          : agent_a, agent_b, agent_c, agent_d, orchestrator
  traces          : 5
  total cost      : $0.0093
```

---

## The event model

Every emitted event has:

```python
CausalEvent(
    event_id,        # uuid4 hex, unique
    trace_id,        # the emitting agent's OTel trace
    span_id,         # the span that emitted it
    parent_span_id,  # structural nesting (may be None)
    caused_by,       # ← the causal pointer: the event that caused this one
    session_id,      # business-level workflow key (shared across all agents)
    actor,           # logical agent identity
    event_type,      # e.g. "agent_b.llm.requested"
    timestamp,       # wall-clock, informational only
    sequence_num,    # monotonic, assigned by the store, defines order
    payload,         # event-specific data
    payload_hash,    # sha256 of canonicalized payload
)
```

The load-bearing field is **`caused_by`**. Nothing else in the system infers causality.

---

## How causality is preserved

### 1. Causation is a first-class field, not a timing inference

Every event carries an explicit `caused_by` that points at another event's `event_id`. The graph projection draws edges only from this field. No edge is ever inferred from timestamps, from "happened in the same trace," or from any other proxy.

### 2. Causation ≠ nesting

OpenTelemetry's `parent_span_id` and `caused_by` are kept strictly separate:

- `parent_span_id` → structural nesting ("this ran inside that span")
- `caused_by` → semantic causality ("this made that happen")

The graph emits both as distinct edge types (`contains` and `causes`), deduplicating when both would apply to the same ordered pair. The Mermaid and DOT exporters render them differently (thick vs. dotted), so a reader cannot confuse them.

### 3. Cross-agent edges are stated, not implied

Each agent is its own OTel trace. When agent A delegates to agent B:

- A emits a `PRODUCER` span (`agent_a.send.agent_b`)
- B's `handle_message` span is started with `context=Context()` (a fresh root, not a child) and a `Link` back to A's `PRODUCER` span with `rel="follows"`

B's span has `parent_id=null` — no agent is a descendant of another. The cross-agent relationship is explicit in the `Link`, not a side effect of span nesting.

### 4. Branch points require explicit parent

`emit()` defaults to the last emitted event of the same agent, which is correct for strictly linear sequences but wrong at any fork. Every branch (`llm` vs `tool` vs `object.created`, delegate vs reply) passes its own trigger explicitly:

```python
self.call_llm(task, caused_by=activated.event_id)
self.call_tool(name, input, fn, caused_by=activated.event_id)
self._create_object(..., spine_trigger=activated.event_id)
```

This eliminates false edges like `llm.responded → tool.requested` that the implicit default would otherwise produce.

### 5. Business and physical identity are separate

- `session_id` — business-level workflow key. Shared across every agent, every event, every span. Used for cost aggregation and workflow-wide queries.
- `trace_id` — physical OTel trace. One per agent (five total in a run).
- `workflow.root_trace_id` — the orchestrator's trace, carried as an attribute on every span so a backend can correlate all five traces back to one workflow.

The causal walk (`causal_chain`) uses neither — it follows `caused_by` across trace boundaries, which is the only thing that actually connects events causally.

### 6. Authorization is bound to the same key

Every delegation carries a signed JWT (`Ed25519`) whose:
- `iss` = sender agent
- `sub` = recipient agent
- `aud` = recipient agent
- `sid` = `session_id`
- `task_hash` = hash of the delegated task

The receiver verifies all five before taking any action. A rejected token surfaces as a `ContractViolation` on the receiving agent's span, which makes failed authorizations visible in the trace.

---

## Limitations

1. **In-process message bus.** `MessageBus` dispatches synchronously. A real deployment would use HTTP/gRPC/Kafka with W3C `traceparent` propagation instead of passing the `SpanContext` object directly.

2. **Ephemeral signing key.** `helpers._load_or_generate_signing_key` generates a fresh Ed25519 key per process unless `SIGNING_KEY_PEM` is set. Production needs a KMS or a JWKS endpoint.

3. **`emit()` has an implicit fallback.** When `caused_by` is not passed, it defaults to the agent's last event. Every current caller is explicit at branch points, but the fallback could produce a false edge if a future caller forgets.

4. **Single-process exporter.** Spans go to stdout via `ConsoleSpanExporter`. Production exports to an OTLP collector for retention and cross-service correlation.

5. **`payload_hash` is written but not verified on read.** `CausalEvent` is frozen (immutable at the Python level), and the hash is stored, but the event store does not re-check it on `by_id()` / `by_trace()`. Adding a `_verify()` on read would make tampering detectable.

6. **No failure-path test in the demo.** `call_tool` re-raises on error and `run_workflow` has a `task.failed` branch, but the sample run is the happy path. A second run with `run_workflow(store, "fail")` would exercise the error path.

7. **Graph size.** The ASCII exporter is O(N) and fine for hundreds of events; Mermaid/DOT fine for a few thousand. Beyond that, push to Neo4j or an analytical graph store.

---

## Scaling to a larger multi-agent environment

**What stays:**

- The three-layer split: physical spans / logical events / semantic graph.
- `caused_by` as the load-bearing field on every event.
- `session_id` (business) ≠ `trace_id` (physical).
- `PRODUCER`/`CONSUMER` + `Link` for cross-agent edges.
- Signed delegation tokens bound to session + subject + audience + task.

**What changes first:**

1. **Context propagation over the wire.** Serialize `SpanContext` as W3C `traceparent` using `opentelemetry.propagate.inject` / `extract`. The `Link` remains the only cross-service artifact.

2. **Central signing key + audience registry.** The token model is correct as-is; it needs a key server and a per-agent public-key registry instead of a shared in-process key.

3. **Durable event store.** SQLite → Postgres with `session_id`, `caused_by`, and `sequence_num` indexed. The schema and query patterns stay the same.

4. **Graph service.** Move the causal graph out of process memory into a graph database or materialized view so it can answer cross-session and cross-tenant queries.

5. **Policy layer.** Add `authorized_by` and `scope` fields to each event so "was this action authorized, by whom, to what extent" can be answered without re-verifying tokens.

6. **Out-of-order event handling.** `sequence_num` is currently per-store. A distributed version needs a per-session logical clock (Lamport or vector) so causality survives transport reordering.

---

## The five-agent question

> If five agents each perform authorized actions, what information would you need to capture to determine whether those actions were part of one coordinated workflow rather than five unrelated events?

You need a **directed causal edge on every action**, not shared context. Concretely:

- A stable `agent_id` per actor — logical, not process-scoped.
- A run-scoped `session_id`, propagated to every agent.
- A `caused_by` on every event, pointing at the specific upstream event.
- A signed delegation token bound to `session_id`, subject, audience, and task — so authorization is verifiable, not asserted.
- A monotonic ordering field (`sequence_num`) so reconstruction is deterministic regardless of clock skew.
- Immutable, append-only storage.

**Decision rule:** the five agents are coordinated iff the transitive closure of `caused_by` from every action terminates at a single root event, **and** every cross-agent edge is corroborated by a delegation token whose session matches. Shared `session_id` alone is correlation. Only the causal pointer plus a valid delegation edge upgrades it to coordination.

That is exactly what this codebase implements, at four agents, in one process.

---

## Trial requirements checklist

| Requirement | Where |
|---|---|
| 2–3 agents (implemented with 4 + orchestrator) | `AgentA`–`AgentD` |
| Unique identity per agent | `agent.id` span attribute, `actor` event field |
| Shared task/session id | `workflow.session_id` on every span and event |
| Agent-to-agent delegation | 4 hops, each with signed JWT |
| Tool/resource interaction | 4 tool calls across agents, distinct tools |
| Event logging with timestamps | `CausalEvent.timestamp` + `sequence_num` |
| Parent/child event relationships | `parent_span_id` (structural) + `caused_by` (causal) |
| Trace/correlation IDs | `trace_id` + `workflow.root_trace_id` + `session_id` |
| Graph/structured output | `causal_graph.mmd` / `.dot` / `.ascii` |
| OpenTelemetry | `PRODUCER`/`CONSUMER` kinds + `Link` for cross-agent |
| Working source + setup/run | this repository |
| Example telemetry output | § *Sample output* |
| Simple visualization | `causal_graph.mmd`, renderable at mermaid.live |
| Design writeup | § *How causality is preserved*, § *Limitations*, § *Scaling* |