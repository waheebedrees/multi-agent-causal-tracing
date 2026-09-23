"""
Event model and append-only store.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, replace
from typing import  Optional

from helpers import  hash_payload, now_iso

@dataclass(frozen=True)
class CausalEvent:
    event_id: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    caused_by: Optional[str]
    
    
    session_id: str
    actor: str
    event_type: str
    timestamp: str
    sequence_num: int
    payload: dict
    payload_hash: str

    @staticmethod
    def create(
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        caused_by: Optional[str],
        session_id: str,
        actor: str,
        event_type: str,
        payload: dict,
    ) -> "CausalEvent":
        return CausalEvent(
            event_id=uuid.uuid4().hex[:16],
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            caused_by=caused_by,
            session_id=session_id,
            actor=actor,
            event_type=event_type,
            timestamp=now_iso(),
            sequence_num=0,
            payload=payload,
            payload_hash=hash_payload(payload),
        )


@dataclass
class CostReport:
    trace_id: str
    llm_usd: float = 0.0
    tool_usd: float = 0.0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    cache_hits: int = 0
    cache_saved_usd: float = 0.0
    breakdown: list[dict] = None

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = []

    @property
    def total_usd(self) -> float:
        return self.llm_usd + self.tool_usd

    def print(self) -> None:
        print("\n" + "=" * 60)
        print(f"COST REPORT - session {self.trace_id[:12]}...")
        print("=" * 60)
        print(f"  LLM calls:    {self.llm_calls}")
        print(f"  Tool calls:   {self.tool_calls}")
        print(f"  Total tokens: {self.total_tokens:,}")
        print(f"  Cache hits:   {self.cache_hits}")
        print(f"  LLM cost:     ${self.llm_usd:.4f}")
        print(f"  Tool cost:    ${self.tool_usd:.4f}")
        print(f"  Total:        ${self.total_usd:.4f}")

        if self.cache_saved_usd:
            print(f"  Cache saved:  ${self.cache_saved_usd:.4f}")
        print("  Breakdown:")
        for item in self.breakdown:
            kind = "llm " if item["type"] == "llm" else "tool"
            tail = " (cached)" if item.get("cached") else ""
            print(f"    [{kind}] {item['agent']:<10} "
                  f"{item['name']:<16} ${item['cost']:.4f}{tail}")
        print("=" * 60)


