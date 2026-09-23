from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from opentelemetry.trace import SpanContext

@dataclass
class InterAgentMessage:
    message_id: str
    trace_id: str
    session_id: str
    recipient: str
    sender: str
    delegation_token: str
    payload: dict[str, Any]
    caused_by_event_id: Optional[str]

    reply_to: Optional[str]
    delegation_span_ctx: Optional[SpanContext] = None   

    @property
    def task_instruction(self) -> Optional[str]:
        return self.payload.get("task_instruction", '')

    @property
    def result(self) -> dict:
        return self.payload.get("result", {})

    @staticmethod
    def create(
        *,
        trace_id,
        session_id,
        caused_by_event_id,
        sender,
        recipient,
        payload,
        delegation_token,
        reply_to=None,
        delegation_span_ctx=None,

    ) -> "InterAgentMessage":
        return InterAgentMessage(
            message_id=uuid.uuid4().hex,
            trace_id=trace_id,
            session_id=session_id,
            caused_by_event_id=caused_by_event_id,
            sender=sender,
            recipient=recipient,
            payload=payload,
            reply_to=reply_to,
            delegation_token=delegation_token,
            delegation_span_ctx=delegation_span_ctx,

        )


AgentHandler = Callable[[InterAgentMessage], InterAgentMessage]


class MessageBus:
    def __init__(self):
        self._handler: dict[str, AgentHandler] = {}

    def register(self, agent_id: str, handler: AgentHandler):
        if agent_id in self._handler:
            raise ValueError(f"agent {agent_id} already registered")
        self._handler[agent_id] = handler

    def send(self, message: InterAgentMessage) -> InterAgentMessage:
        handler = self._handler.get(message.recipient)
        if handler is None:
            raise RuntimeError(
                f"not agent registered for {message.recipient}"
            )
        return handler(message)
