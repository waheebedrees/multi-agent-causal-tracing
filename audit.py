from __future__ import annotations

from typing import Optional

from event_store import EventStore
from models import CausalEvent, CostReport


def cost_report(store: EventStore, session_id: str) -> CostReport:
    report = CostReport(trace_id=session_id)
    by_id = store.by_id()
    for e in by_id.values():
        if e.session_id != session_id:         
            continue
        if e.event_type.endswith(".llm.responded"):
            cost = float(e.payload.get("cost_usd", 0))
            tokens = int(e.payload.get("tokens", 0))
            cached = bool(e.payload.get("cache_hit"))
            req = by_id.get(e.caused_by) if e.caused_by else None
            model = req.payload.get("model", "?") if req else "?"
            agent = req.actor if req else e.actor
            report.llm_calls += 1
            report.total_tokens += tokens
            if cached:
                report.cache_hits += 1
                report.cache_saved_usd += cost
            else:
                report.llm_usd += cost
            report.breakdown.append({
                "type": "llm", "agent": agent, "name": model,
                "cost": cost, "tokens": tokens, "cached": cached,
            })

        elif e.event_type.endswith(".tool.responded"):
            cost = float(e.payload.get("cost_usd", 0))
            cached = bool(e.payload.get("cache_hit"))
            req = by_id.get(e.caused_by) if e.caused_by else None
            # FIX: the request event stores the name under "tool_name", not "tool".
            if req:
                tool = req.payload.get(
                    "tool_name") or req.payload.get("tool") or "?"
            else:
                tool = "?"
            agent = req.actor if req else e.actor
            report.tool_calls += 1
            if not cached:
                report.tool_usd += cost
            report.breakdown.append({
                "type": "tool", "agent": agent, "name": tool,
                "cost": cost, "cached": cached,
            })
    return report


def causal_chain(store: EventStore, object_id: str) -> str:
    if not object_id:
        return "(no such object: <empty id>)"

    by_id = store.by_id()

    # --- locate the object.created event that carries this object 
    # FIX: stored type is "<actor>.object.created"; use suffix match.
    create_evt: Optional[CausalEvent] = None
    for e in by_id.values():
        if not e.event_type.endswith(".object.created"):
            continue
        obj = e.payload.get("object") if isinstance(e.payload, dict) else None
        if isinstance(obj, dict) and obj.get("id") == object_id:
            create_evt = e
            break
    if create_evt is None:
        return f"(no such object: {object_id})"

    obj = create_evt.payload["object"]
    label = (obj.get("data", {}).get("title")
             or obj.get("data", {}).get("text") or "")
    label_s = f' "{label}"' if label else ""

    lines = [f"{obj['id']} ({obj['type']}){label_s}"]
    prov = obj.get("provenance") or {}

    #LLM branch 
    llm_req_id = prov.get("llm_request_event_id")
    if llm_req_id and (llm_req := by_id.get(llm_req_id)) is not None:
        llm_resp = _response_for(by_id, llm_req_id, "llm.responded")
        lines.append(f"  <- {llm_req.actor} ({llm_req.event_id}) "
                     f"{llm_req.event_type}  "
                     f"model={llm_req.payload.get('model', '?')}")
        if llm_resp:
            lines.append(f"    ({llm_resp.event_id}) {llm_resp.event_type}"
                         f"{_tail(llm_resp.payload)}")

    # Tool branch (may be several)
    for tr_id in prov.get("tool_request_event_ids") or []:
        tr = by_id.get(tr_id)
        if tr is None:
            continue
        tr_resp = _response_for(by_id, tr_id, "tool.responded")
        # FIX: request event stores the tool name under "tool_name".
        tool_name = tr.payload.get(
            "tool_name") or tr.payload.get("tool") or "?"
        lines.append(f"  <- {tr.actor} ({tr.event_id}) "
                     f"{tr.event_type}  tool={tool_name}")
        if tr_resp:
            lines.append(f"    ({tr_resp.event_id}) {tr_resp.event_type}"
                         f"{_tail(tr_resp.payload)}")

    #Spine: walk caused_by backwards from the object.created event
    indent = "  "
    seen: set[str] = set()
    cursor: Optional[CausalEvent] = create_evt
    while cursor is not None:
        if cursor.event_id in seen:
            lines.append(f"{indent}<- (cycle at {cursor.event_id})")
            break
        seen.add(cursor.event_id)

        lines.append(f"{indent}<- {cursor.actor} ({cursor.event_id}) "
                     f"{cursor.event_type}")

        if cursor.caused_by is None:
            break
        nxt = by_id.get(cursor.caused_by)
        if nxt is None:
            lines.append(f"{indent}  <- (missing event {cursor.caused_by})")
            break
        cursor = nxt
        indent += "  "

    return "\n".join(lines)



def _response_for(by_id, request_id, response_type):
    """Find the event of `response_type` whose caused_by == request_id.

    `response_type` is the unprefixed suffix, e.g. "llm.responded";
    stored types look like "agent_b.llm.responded".
    """
    for e in by_id.values():
        if e.caused_by != request_id:
            continue
        # suffix match, not exact match.
        if e.event_type == response_type or e.event_type.endswith("." + response_type):
            return e
    return None


def _tail(payload: dict) -> str:
    err = payload.get("error")
    if err:
        return f" error={err}"
    if payload.get("cache_hit"):
        return " (cache_hit)"
    cost = payload.get("cost_usd")
    if cost is not None:
        try:
            return f" cost=${float(cost):.4f}"
        except (TypeError, ValueError):
            return f" cost={cost}"
    return ""
