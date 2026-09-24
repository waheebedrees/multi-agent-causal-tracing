
from __future__ import annotations
import argparse

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
from llm import get_gateway

def run_workflow(
    store: EventStore,
    task: str,
    session_id: str,
    link_to: Optional[SpanContext] = None,
    llm = None
) -> tuple[str, str, SpanContext]:
    
    tracer = get_tracer(False)
    bus = MessageBus()
    llm = llm or get_gateway()

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

        AgentA(store, bus=bus, llm=llm, agent_id="agent_a",
               session_id=session_id, trace_id=trace_id)
        AgentB(store, bus=bus, llm=llm, agent_id="agent_b",
               session_id=session_id, trace_id=trace_id)
        AgentC(store, bus=bus, llm=llm, agent_id="agent_c",
               session_id=session_id, trace_id=trace_id)
        AgentD(store, bus=bus, llm=llm, agent_id="agent_d",
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

def scenario_single(store: EventStore, out_dir: str = "./docs") -> None:
    """One workflow, full chain + graph."""
    session = uuid.uuid4().hex[:12]
    trace, obj, _ctx = run_workflow(store, "Analyze security logs for anomalies",
                                    session_id=session)
    print(f"trace_id   = {trace}")
    print(f"session_id = {session}\n")
    print(causal_chain(store, obj))
    cost_report(store, session).print()
    _write_graph(store, session, out_dir)

    print("\n" + "=" * 72)
    print("LLM RESPONSES")
    print("=" * 72)
    for e in store.by_session_id(session):
        if not e.event_type.endswith("llm.responded"):
            continue
        text = e.payload.get("text", "(no text)")
        preview = text if len(text) <= 300 else text[:300] + "…"
        print(f"\n[{e.actor}]  tokens={e.payload.get('tokens')}  "
              f"cost=${e.payload.get('cost_usd', 0):.6f}")
        print(f"  {preview}")

def scenario_three(store: EventStore, out_dir: str = "./docs") -> None:
    """Happy path, span-linked workflow, error path — in one store."""
    session1 = uuid.uuid4().hex[:12]
    trace1, obj1, ctx1 = run_workflow(
        store, "Analyze security logs for anomalies", session_id=session1)
    print(f"[W1] trace_id={trace1}  session_id={session1}")

    session2 = uuid.uuid4().hex[:12]
    trace2, _obj2, _ctx2 = run_workflow(
        store, "Summarize a news article", session_id=session2, link_to=ctx1)
    print(f"[W2] trace_id={trace2}  session_id={session2}")

    session3 = uuid.uuid4().hex[:12]
    try:
        run_workflow(store, "fail", session_id=session3)
    except RuntimeError as exc:
        print(f"[W3] raised as expected: {exc}")
        _print_failure_chain(store, session3)

    for i, sid in enumerate((session1, session2, session3), start=1):
        events = store.by_session_id(sid)
        if not events:
            continue
        print(f"\n=== WORKFLOW {i} — session {sid} — {len(events)} events ===")
        cost_report(store, sid).print()

    _validate_disjoint(store, [session1, session2, session3])
    _write_graph(store, session1, out_dir)



def _write_graph(store: EventStore, session_id: str, out_dir: str) -> None:
    events = store.by_session_id(session_id)
    builder = CausalGraphBuilder()
    builder.build(events)
    print(builder.to_ascii())
    builder.save_mermaid(f"{out_dir}/causal_graph.mmd")
    builder.save_dot(f"{out_dir}/causal_graph.dot")
    builder.to_markdown(f"{out_dir}/causal_graph.md")
    print(f"\nwrote {out_dir}/causal_graph.{{mmd,dot,md}}")
    print(f"render: dot -Tsvg -o {out_dir}/causal_graph.svg "
          f"{out_dir}/causal_graph.dot")


def _print_failure_chain(store: EventStore, session_id: str) -> None:
    by_id = store.by_id()
    failed = [e for e in by_id.values()
              if e.session_id == session_id and e.event_type == "task.failed"]
    if not failed:
        return
    print("\nchain to failure:")
    cursor = failed[-1]
    indent = "  "
    while cursor:
        print(f"{indent}<- {cursor.actor} "
              f"({cursor.event_id[:12]}) {cursor.event_type}")
        if not cursor.caused_by:
            break
        cursor = by_id[cursor.caused_by]
        indent += "  "


def _validate_disjoint(store: EventStore, sessions: list[str]) -> None:
    print("\n" + "=" * 72)
    print("DAG VALIDATION")
    print("=" * 72)
    g = CausalGraphBuilder().build(list(store.all_events()))
    print(f"nodes={g.number_of_nodes()}  edges={g.number_of_edges()}")

    enforce(len(set(sessions)) == len(sessions), 6, "session ids collided")

    roots = [n for n, d in g.in_degree() if d == 0]
    enforce(len(roots) == len(sessions), 6,
            f"expected {len(sessions)} roots, got {len(roots)}")

    by_id = store.by_id()
    crossed = sum(
        1 for u, v, k in g.edges(keys=True)
        if k == "causes" and by_id[u].session_id != by_id[v].session_id
    )
    enforce(crossed == 0, 6,
            f"{crossed} causes edges cross workflow boundaries")

    print(f"OK - {len(sessions)} disjoint causal DAGs, "
          f"no shared causes-edges")
    causes_g = causes_only(g)
    print(f"causes-only subgraph: "
          f"{causes_g.number_of_nodes()} nodes, "
          f"{causes_g.number_of_edges()} edges")




def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="observer",
        description="Multi-agent causal tracing demo",
    )
    p.add_argument(
        "scenario",
        choices=("single", "three"),
        help="single = one workflow + graph; "
             "three = happy path + span-linked + error path",
    )
    p.add_argument(
        "--out", default="./docs",
        help="output directory for graph artifacts (default: ./docs)",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    store = EventStore(":memory:")

    if args.scenario == "single":
        scenario_single(store, args.out)
    else:
        scenario_three(store, args.out)


if __name__ == "__main__":
    main()
