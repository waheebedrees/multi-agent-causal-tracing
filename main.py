
from __future__ import annotations

import uuid
from typing import Optional

from opentelemetry.trace import Link, SpanContext, SpanKind


from helpers import causes_only, enforce
from models import CausalEvent
from event_store import EventStore
from tracing import get_tracer, span_ids
from graph import CausalGraphBuilder
from audit import causal_chain, cost_report
from message_bus import InterAgentMessage, MessageBus
from agents import AgentA, AgentB, AgentC, AgentD

def run_workflow(
    store: EventStore,
    task: str,
    session_id: str,
    link_to: Optional[SpanContext] = None
) -> tuple[str, str, SpanContext]:

    tracer = get_tracer(False)
    bus = MessageBus()

    links = []
    if link_to is not None:
        links = [Link(link_to, attributes={"rel": "follows"})]

    with tracer.start_as_current_span(
        "workflow.root",
        kind=SpanKind.SERVER,
        links=links,
    ) as root:

        trace_id, root_span_id, _ = span_ids(root)
        root.set_attribute("workflow.session_id", session_id)

        root.set_attribute("workflow.root_trace_id", trace_id)
        root.set_attribute("graph.node.id", "root_task")

        root_ctx = root.get_span_context()

        AgentA(store, bus=bus, agent_id="agent_a",
               session_id=session_id, trace_id=trace_id)
        AgentB(store, bus=bus, agent_id="agent_b",
               session_id=session_id, trace_id=trace_id)
        AgentC(store, bus=bus, agent_id="agent_c",
               session_id=session_id, trace_id=trace_id)
        AgentD(store, bus=bus, agent_id="agent_d",
               session_id=session_id, trace_id=trace_id)

        root_evt = store.append(CausalEvent.create(
            trace_id=trace_id, 
            actor="user",
            span_id=root_span_id,
            parent_span_id=None, 
            caused_by=None,
            session_id=session_id, 
            event_type="task.started", payload={"task": task},
        ))

        initial = InterAgentMessage.create(
            trace_id=trace_id,
            session_id=session_id,            
            recipient="agent_a",
            sender="user",
            payload={"task_instruction": task},
            caused_by_event_id=root_evt.event_id,
            delegation_token="",
            delegation_span_ctx=root_ctx,      # NEW — root.get_span_context()

        )

        try:
            reply = bus.send(initial)
        except Exception:
            # The failure event is whatever the last event in this trace
            # was (e.g. tool.failed). task.failed is caused by it, not by
            # the root — otherwise it becomes a sibling of the whole run.
            last = [e for e in store.by_id().values()
                    if e.session_id == session_id][-1]
            store.append(CausalEvent.create(
                trace_id=trace_id, span_id=root_span_id,
                parent_span_id=None,
                caused_by=last.event_id,
                session_id=session_id,
                actor="user",
                event_type="task.failed",
                payload={"reason": "downstream failure"},
            ))
            raise

        result_id = reply.payload.get("result")
        store.append(CausalEvent.create(
            trace_id=trace_id, span_id=root_span_id,
            parent_span_id=None,
            caused_by=reply.caused_by_event_id,
            actor="user",
            session_id=session_id,
            event_type="task.completed",
            payload={"result_object_id": result_id},
        ))

    return trace_id, result_id, root_ctx




def main() -> None:
    store = EventStore(":memory:")
    path = "./docs"

    session1 = uuid.uuid4().hex[:12]
    trace1, obj1, ctx1 = run_workflow(
        store, "Analyze security logs for anomalies", session_id=session1)

    print(f"trace_id   = {trace1}")
    print(f"session_id = {session1}\n")

    print(causal_chain(store, obj1))
    cost_report(store, session1).print()

    events = store.by_session_id(session1)
    builder = CausalGraphBuilder()
    builder.build(events)

    print(builder.to_ascii())

    builder.save_mermaid(f"{path}/causal_graph.mmd")
    builder.save_dot(f"{path}/causal_graph.dot")
    builder.to_markdown(f"{path}/causal_graph.md")

    print(f"\nwrote {path}/causal_graph.{{mmd,dot,md}}")
    print(
        f"render:  dot -Tsvg -o {path}/causal_graph.svg {path}/causal_graph.dot")
    

def main2() -> None:
    store = EventStore(":memory:")
    path = "./docs"

    #  workflow 1: happy path 
    session1 = uuid.uuid4().hex[:12]
    trace1, obj1, ctx1 = run_workflow(
        store, "Analyze security logs for anomalies", session_id=session1)
    print(f"[W1] trace_id={trace1}  session_id={session1}")

    #  workflow 2: span-linked to W1 (temporal, not causal)
    session2 = uuid.uuid4().hex[:12]
    trace2, obj2, ctx2 = run_workflow(
        store, "Summarize a news article",
        session_id=session2, link_to=ctx1)
    print(f"[W2] trace_id={trace2}  session_id={session2}")

    #  workflow 3: error path
    session3 = uuid.uuid4().hex[:12]
    trace3 = None
    try:
        trace3, obj3, ctx3 = run_workflow(store, "fail", session_id=session3)
    except RuntimeError as exc:
        print(f"[W3] raised as expected: {exc}")
        failed = [e for e in store.by_id().values()
                  if e.session_id == session3
                  and e.event_type == "task.failed"]
        if failed:
            trace3 = failed[-1].trace_id
            print("\nchain to failure:")
            by_id = store.by_id()
            cursor = failed[-1]
            indent = "  "
            while cursor:
                print(f"{indent}<- {cursor.actor} "
                      f"({cursor.event_id[:12]}) {cursor.event_type}")
                if not cursor.caused_by:
                    break
                cursor = by_id[cursor.caused_by]
                indent += "  "

    sessions = [session1, session2, session3]
    for i, sid in enumerate(sessions, start=1):
        events = store.by_session_id(sid)
        if not events:
            continue
        print(f"\n=== WORKFLOW {i} — session {sid} — "
              f"{len(events)} events ===")
        cost_report(store, sid).print()

    #  DAG validation: three disjoint workflows 
    print("\n" + "=" * 72)
    print("DAG VALIDATION")
    print("=" * 72)
    all_evts = list(store.all_events())
    g = CausalGraphBuilder().build(all_evts)
    print(f"nodes={g.number_of_nodes()}  edges={g.number_of_edges()}")

    # 1) session ids unique
    enforce(len(set(sessions)) == len(sessions), 6, "session ids collided")

    # 2) exactly one root per workflow
    roots = [n for n, d in g.in_degree() if d == 0]
    enforce(len(roots) == len(sessions), 6,
            f"expected {len(sessions)} roots, got {len(roots)}")

    # 3) no causes-edge crosses a session boundary
    by_id = store.by_id()
    crossed = 0
    for u, v, k in g.edges(keys=True):
        if k != "causes":
            continue
        if by_id[u].session_id != by_id[v].session_id:
            crossed += 1
    enforce(crossed == 0, 6,
            f"{crossed} causes edges cross workflow boundaries")

    print(f"OK - {len(sessions)} disjoint causal DAGs, "
          f"no shared causes-edges")

    causes_g = causes_only(g)
    print(f"causes-only subgraph: {causes_g.number_of_nodes()} nodes, "
          f"{causes_g.number_of_edges()} edges")

    print("\n" + "=" * 72)
    print(f"MERMAID - workflow 1 (session {session1})")
    print("=" * 72)
    events1 = store.by_session_id(session1)
    builder = CausalGraphBuilder()
    builder.build(events1)
    print(builder.to_ascii())
    builder.save_mermaid(f"{path}/causal_graph_{session1}.mmd")
    builder.save_dot(f"{path}/causal_graph_{session1}.dot")
    builder.to_markdown(f"{path}/causal_graph_{session1}.md")
    print(f"\nwrote {path}/causal_graph_{session1}.{{mmd,dot,md}}")

if __name__ == "__main__":
    main2()
