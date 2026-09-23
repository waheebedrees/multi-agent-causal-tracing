"""Causal graph projection + visualization.

Reads a list of CausalEvents and produces a directed graph where:
  * 'causes'   edges come from event.caused_by       — semantic causality
  * 'contains' edges come from event.parent_span_id  — structural nesting

Both are kept so the visualization can show *why* two events are related:
"ran inside the same span" (contains) is not the same as "caused" (causes).
The Mermaid/DOT exporters distinguish them visually.

MultiDiGraph is used so both edge types can coexist between the same pair
of nodes without overwriting each other.

Exports:
    to_mermaid()   — paste into README / GitHub / https://mermaid.live
    to_dot()       — `dot -Tsvg -o graph.svg < graph.dot`
    to_ascii()     — terminal fallback, no dependencies
    to_markdown()  — write a fenced ```mermaid block to a .md file
    save_mermaid / save_dot / save_png
"""
from __future__ import annotations

from typing import Iterable, Optional

import networkx as nx

from helpers import assert_dag
from models import CausalEvent


_ACTOR_COLORS = {
    "user":    "#6b7280",   # gray   — operator / boundary
    "agent_a": "#2563eb",   # blue   — orchestrator
    "agent_b": "#dc2626",   # red    — worker
    "agent_c": "#059669",   # green  — worker
    "agent_d": "#d97706",   # amber  — worker
}
_DEFAULT_COLOR = "#9333ea"

_KNOWN_AGENTS = {"user", "agent_a", "agent_b", "agent_c", "agent_d"}


def _actor_color(actor: str) -> str:
    return _ACTOR_COLORS.get(actor, _DEFAULT_COLOR)


def _short_type(event_type: str) -> str:
    """'agent_a.llm.requested' -> 'llm.requested'."""
    head, _, tail = event_type.partition(".")
    return tail if head in _KNOWN_AGENTS else event_type


def _node_label(e: CausalEvent) -> str:
    label = _short_type(e.event_type)
    cost = e.payload.get("cost_usd")
    if cost is not None:
        try:
            label += f" ${float(cost):.4f}"
        except (TypeError, ValueError):
            pass
    if e.payload.get("error"):
        label += " (error)"
    return label


def _safe_id(s: str) -> str:
    return "g_" + "".join(c if c.isalnum() else "_" for c in s)[:32]


def _mermaid_shape(short_type: str, label: str) -> str:
    """Mermaid node shape by event type — makes the graph readable at a glance."""
    if short_type in ("task.started", "task.completed"):
        return f"(({label}))"           # circle: workflow boundary
    if short_type == "task.failed":
        return f"{{{{{label}}}}}"       # hexagon: failure
    if short_type == "object.created":
        return f"([{label}])"           # stadium: artifact
    if short_type == "delegated":
        return f">{label}]"             # flag: handoff
    if short_type == "activated":
        return f"[/{label}/]"           # parallelogram: activation
    return f"[{label}]"


class CausalGraphBuilder:
    """Project an event stream into a causal DAG.

    A single builder instance is reusable: call build() repeatedly.
    """

    def __init__(self) -> None:
        self.g: nx.MultiDiGraph = nx.MultiDiGraph()


    def build(self, events: Iterable[CausalEvent]) -> nx.MultiDiGraph:
        events = list(events)
        g = nx.MultiDiGraph()

        # 1) nodes
        for e in events:
            g.add_node(
                e.event_id,
                event_type=e.event_type,
                short_type=_short_type(e.event_type),
                actor=e.actor,
                trace_id=e.trace_id,
                session_id=e.session_id,
                timestamp=e.timestamp,
                sequence_num=e.sequence_num,
                cost_usd=e.payload.get("cost_usd"),
                label=_node_label(e),
            )

        # 2) causes edges — the semantic relation
        for e in events:
            if e.caused_by and g.has_node(e.caused_by):
                g.add_edge(e.caused_by, e.event_id,
                           key="causes", type="causes")

        # 3) contains edges — structural nesting, from parent_span_id.
        #    Dedup against 'causes': if a causal edge already exists between
        #    the same ordered pair, don't also draw a structural one.
        by_span: dict[str, list[CausalEvent]] = {}
        for e in events:
            by_span.setdefault(e.span_id, []).append(e)
        span_opener = {
            sid: min(evts, key=lambda x: x.sequence_num)
            for sid, evts in by_span.items()
        }

        for e in events:
            if not e.parent_span_id or e.parent_span_id not in span_opener:
                continue
            opener = span_opener[e.parent_span_id]
            if opener.event_id == e.event_id:
                continue
            if g.has_edge(opener.event_id, e.event_id, key="causes"):
                continue
            g.add_edge(opener.event_id, e.event_id,
                       key="contains", type="contains")

        # 4) the whole point of the projection — must be acyclic
        assert_dag(g)
        self.g = g
        return g


    def summary(self) -> dict:
        actors = {d["actor"] for _, d in self.g.nodes(data=True)}
        traces = {d["trace_id"] for _, d in self.g.nodes(data=True)}
        costs = [d["cost_usd"] for _, d in self.g.nodes(data=True)
                 if d["cost_usd"] is not None]
        causes = [(u, v)
                  for u, v, k in self.g.edges(keys=True) if k == "causes"]
        contains = [(u, v)
                    for u, v, k in self.g.edges(keys=True) if k == "contains"]
        roots = [n for n in self.g.nodes() if self.g.in_degree(n) == 0]
        return {
            "events": self.g.number_of_nodes(),
            "causes_edges": len(causes),
            "contains_edges": len(contains),
            "agents": sorted(actors),
            "traces": len(traces),
            "total_cost_usd": round(sum(costs), 4) if costs else 0.0,
            "roots": roots,
        }


    def to_mermaid(self, direction: str = "TD", group_by: str = "trace") -> str:
        """group_by: 'trace' | 'actor' | 'none'."""
        g = self.g

        if group_by == "trace":
            def key(nid): return g.nodes[nid]["trace_id"]
        elif group_by == "actor":
            def key(nid): return g.nodes[nid]["actor"]
        else:
            def key(nid): return "events"

        groups: dict[str, list[str]] = {}
        for nid in g.nodes():
            groups.setdefault(key(nid), []).append(nid)

        # short node ids: n0, n1, ...
        short = {nid: f"n{i}" for i, nid in enumerate(g.nodes())}
        lines = [f"flowchart {direction}"]

        for gname, nids in groups.items():
            sub = _safe_id(gname)
            label = self._group_label(gname, nids, group_by)
            lines.append(f'  subgraph {sub}["{label}"]')
            for nid in nids:
                d = g.nodes[nid]
                shape = _mermaid_shape(d["short_type"], d["label"])
                lines.append(f"    {short[nid]}{shape}")
            lines.append("  end")

        for u, v, k, d in g.edges(keys=True, data=True):
            if d.get("type") == "causes":
                lines.append(f"  {short[u]} ==> {short[v]}")   # thick = caused
            else:
                # dotted = contains
                lines.append(f"  {short[u]} -.-> {short[v]}")

        # one class per actor
        actors = sorted({g.nodes[n]["actor"] for n in g.nodes()})
        for a in actors:
            lines.append(
                f"  classDef {_safe_id(a)} fill:{_actor_color(a)},"
                f"color:#fff,stroke:#111,stroke-width:1px"
            )
        for a in actors:
            members = [short[n] for n in g.nodes() if g.nodes[n]["actor"] == a]
            if members:
                lines.append(f"  class {','.join(members)} {_safe_id(a)}")

        return "\n".join(lines)

    def _group_label(self, gname: str, nids: list[str], group_by: str) -> str:
        g = self.g
        if group_by == "none":
            return "events"
        if group_by == "actor":
            return f"{gname}  ({len(nids)} events)"

        # trace mode — if one actor, just show the actor name.
        actors = sorted({g.nodes[n]["actor"] for n in nids})
        if len(actors) == 1:
            return actors[0]
        actor_part = ", ".join(actors) if actors else "?"
        return f"trace {gname[:8]}  ({actor_part})"

    def to_dot(self) -> str:
        g = self.g
        lines = [
            "digraph causal {",
            '  rankdir="TB";',
            '  graph [fontname="Helvetica"];',
            '  node  [shape=box, style="filled,rounded", '
            'fontname="Helvetica", fontsize=10];',
            '  edge  [fontname="Helvetica", fontsize=9];',
        ]
        for nid, d in g.nodes(data=True):
            color = _actor_color(d["actor"])
            label = f'{d["short_type"]}\\n{d["actor"]}'
            lines.append(
                f'  "{nid}" [label="{label}", fillcolor="{color}", '
                f'fontcolor="white"];'
            )
        # style already distinguishes causes (solid) from contains (dashed);
        # drop the redundant label to keep large graphs readable.
        for u, v, k, d in g.edges(keys=True, data=True):
            style = "solid" if d.get("type") == "causes" else "dashed"
            lines.append(f'  "{u}" -> "{v}" [style={style}];')
        lines.append("}")
        return "\n".join(lines)

    def to_ascii(self, width: int = 100) -> str:
        g = self.g
        s = self.summary()
        out = []
        out.append("=" * width)
        out.append("CAUSAL GRAPH SUMMARY")
        out.append("=" * width)
        out.append(f"  events          : {s['events']}")
        out.append(f"  causes edges    : {s['causes_edges']}")
        out.append(f"  contains edges  : {s['contains_edges']}")
        out.append(f"  agents          : {', '.join(s['agents']) or '(none)'}")
        out.append(f"  traces          : {s['traces']}")
        out.append(f"  total cost      : ${s['total_cost_usd']:.4f}")
        out.append("")
        out.append("EVENTS (in sequence order)")
        out.append("-" * width)
        out.append(f"  {'seq':>4}  {'actor':<14} {'event_type':<28} "
                   f"{'cost':>8}  {'trace':<10}")
        for nid, d in sorted(g.nodes(data=True),
                             key=lambda x: x[1]["sequence_num"]):
            cost = f"${d['cost_usd']:.4f}" if d["cost_usd"] is not None else ""
            out.append(
                f"  {d['sequence_num']:>4}  {d['actor']:<14} "
                f"{d['short_type']:<28} {cost:>8}  {d['trace_id'][:8]}"
            )
        out.append("")
        out.append("CAUSES EDGES  (u caused v)")
        out.append("-" * width)
        for u, v, k, _ in g.edges(keys=True, data=True):
            if k != "causes":
                continue
            ua, ut = g.nodes[u]["actor"], g.nodes[u]["short_type"]
            va, vt = g.nodes[v]["actor"], g.nodes[v]["short_type"]
            out.append(f"  {ua:<12} {ut:<28} ->  {va:<12} {vt}")
        return "\n".join(out)


    def save_mermaid(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_mermaid())

    def to_markdown(self, path: str) -> None:
        """Write a fenced ```mermaid block, ready to embed in a README."""
        with open(path, "w", encoding="utf-8") as f:
            f.write("```mermaid\n")
            f.write(self.to_mermaid())
            f.write("\n```\n")

    def save_dot(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_dot())

    def save_png(self, path: str, seed: int = 42) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        g = nx.DiGraph()
        for n, d in self.g.nodes(data=True):
            g.add_node(n, **d)
        for u, v, k, d in self.g.edges(keys=True, data=True):
            if d.get("type") == "causes":
                g.add_edge(u, v)

        pos = nx.spring_layout(g, seed=seed, k=0.9)
        colors = [_actor_color(g.nodes[n]["actor"]) for n in g.nodes()]

        # label with actor + cost so the PNG is self-describing
        labels = {}
        for n in g.nodes():
            d = g.nodes[n]
            s = f'{d["short_type"]}\n{d["actor"]}'
            if d["cost_usd"] is not None:
                try:
                    s += f'\n${float(d["cost_usd"]):.4f}'
                except (TypeError, ValueError):
                    pass
            labels[n] = s

        plt.figure(figsize=(14, 10))
        nx.draw_networkx_edges(g, pos, arrows=True, arrowsize=14,
                               edge_color="#444", width=1.2)
        nx.draw_networkx_nodes(g, pos, node_color=colors, node_size=1600,
                               edgecolors="#111", linewidths=0.8)
        nx.draw_networkx_labels(g, pos, labels=labels, font_size=7,
                                font_color="white", font_weight="bold")
        plt.axis("off")
        plt.savefig(path, dpi=150, bbox_inches="tight")
        plt.close()
