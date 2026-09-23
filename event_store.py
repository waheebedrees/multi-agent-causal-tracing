
"""Event model and append-only store.

CONTRACT v1 #2: events are immutable. Once written, an event is never
updated or deleted. sequence_num is a monotonic counter assigned by
the store, independent of wall-clock time.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from typing import Iterator, Optional

from helpers import hash_payload, ContractViolation
from models import  CausalEvent


_COLUMNS = (
    "event_id", "trace_id", "span_id", "parent_span_id", "caused_by",
    "session_id", "actor", "event_type", "timestamp", "sequence_num",
    "payload", "payload_hash",
)

class EventStore:
    """Append-only SQLite-backed store. Use db_path=':memory:' for tests."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                caused_by TEXT,
                session_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                sequence_num INTEGER NOT NULL,
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trace ON events(trace_id)"
        )
        self.conn.commit()
        self._seq = 0

    def append(self, event: CausalEvent) -> CausalEvent:
        self._seq += 1
        stored = replace(event, sequence_num=self._seq)
        self.conn.execute(
            f"INSERT INTO events ({','.join(_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_COLUMNS))})",
            (
                stored.event_id, stored.trace_id, stored.span_id,
                stored.parent_span_id, stored.caused_by, stored.session_id,
                stored.actor, stored.event_type, stored.timestamp,
                stored.sequence_num, json.dumps(stored.payload),
                stored.payload_hash,
            ),
        )
        self.conn.commit()
        return stored

    def by_trace(self, trace_id: str) -> list[CausalEvent]:
        cur = self.conn.execute(
            "SELECT * FROM events WHERE trace_id=? ORDER BY sequence_num",
            (trace_id,),
        )
        return [self._row(r) for r in cur.fetchall()]
    
    def by_session_id(self, session_id: str) -> list[CausalEvent]:
        cur = self.conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY sequence_num",
            (session_id,),
        )
        return [self._row(r) for r in cur.fetchall()]

    def all_events(self) -> Iterator[CausalEvent]:
        cur = self.conn.execute("SELECT * FROM events ORDER BY sequence_num")
        for row in cur.fetchall():
            yield self._row(row)


    def by_id(self) -> dict[str, CausalEvent]:
        return {e.event_id: e for e in self.all_events()}

    @staticmethod
    def _row(row) -> CausalEvent:
        payload = json.loads(row[10])
        stored_hash = row[11]
        recomputed = hash_payload(payload)
        if recomputed != stored_hash:
            raise ContractViolation(
                2, f"payload_hash mismatch for event {row[0]}"
            )
        return CausalEvent(
            event_id=row[0], trace_id=row[1], span_id=row[2],
            parent_span_id=row[3], caused_by=row[4], session_id=row[5],
            actor=row[6], event_type=row[7], timestamp=row[8],
            sequence_num=row[9], payload=payload,
            payload_hash=stored_hash,
        )

    def next_sequence_num(self) -> int:
        return self._seq + 1
