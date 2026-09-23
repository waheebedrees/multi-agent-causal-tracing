
from __future__ import annotations
from abc import ABC, abstractmethod
import uuid
import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Optional
from opentelemetry.trace import Link, SpanKind, Span, Context

from helpers import (
    enforce,
    make_delegation_token,
    verify_delegation_token,
)

from models import CausalEvent
from event_store import EventStore
from tracing import get_tracer, span_ids
from message_bus import InterAgentMessage, MessageBus



def lookup_data(query: str) -> dict:
    if query.strip().lower() == "fail":
        raise RuntimeError("tool backend unavailable")
    return {
        "tool": "lookup_data",
        "result": f"result-for:{query}",
        "cost_usd": 0.0003,
        "cache_hit": False,
    }


def transform_data(query: str) -> dict:
    if query.strip().lower() == "fail":
        raise RuntimeError("transform backend unavailable")
    return {
        "tool": "transform_data",
        "result": f"transformed:{query}",
        "cost_usd": 0.0004,
        "cache_hit": False,
    }


def finalize_data(query: str) -> dict:
    if query.strip().lower() == "fail":
        raise RuntimeError("finalize backend unavailable")
    return {
        "tool": "finalize_data",
        "result": f"finalized:{query}",
        "cost_usd": 0.0002,
        "cache_hit": False,
    }


@dataclass
class AgentState:
    agent_id: str
    session_id: str
    trace_id: str
    parent_event_id: Optional[str] = None


class BaseAgent(ABC):
    model: str = "gpt-4o-mini"

    def __init__(self, store: EventStore, bus: MessageBus, agent_id, session_id, trace_id):
        self.store = store
        self.bus = bus
        self.tracer = get_tracer()
        self.state = AgentState(agent_id, session_id, trace_id)
        self.bus.register(self.agent_id, self.handle_message)

    @property
    def agent_id(self):
        return self.state.agent_id

    @property
    def trace_id(self):
        return self.state.trace_id

    @property
    def session_id(self):
        return self.state.session_id

    @property
    def parent_event_id(self):
        return self.state.parent_event_id

    def _span_name(self, method) -> str:
        return f"{self.agent_id}.{method.__name__}"

    def make_token(self, subject: str, audience: str, task: str):
        return make_delegation_token(
            issuer=self.agent_id,
            session_id=self.session_id,
            audience=audience,
            subject=subject,
            task=task,
        )

    def validate_token(self, message: InterAgentMessage):
        enforce(verify_delegation_token(
            message.delegation_token,
            expected_subject=self.agent_id,
            expected_session=message.session_id,
            expected_task=message.task_instruction,
            expected_audience=message.recipient,
            expected_issuer=message.sender,
        ), 7, f"{self.agent_id}: delegation token rejected")

    def _incoming_links(self, message: InterAgentMessage) -> list[Link]:
        if message.delegation_span_ctx is None:
            return []
        return [Link(
            message.delegation_span_ctx,
            attributes={"rel": "follows", "messaging.from": message.sender},
        )]

    @abstractmethod
    def handle_message(
        self, message: InterAgentMessage) -> InterAgentMessage: ...

    def _create_object(self, *, span, spine_trigger, response_event,
                       request_event, llm_req_evt, delegation_event_id):
        obj_id = f"obj-{response_event.event_id[:12]}"
        obj = {
            "id": obj_id,
            "type": "claim",
            "data": {"result": response_event.payload.get("result", "")},
            "provenance": {
                "created_by_event": response_event.event_id,
                "tool_request_event_ids": [request_event.event_id],
                "llm_request_event_id": llm_req_evt.event_id,
                "delegated_from_event": delegation_event_id,
            },
        }
        self.emit(
            span,
            f"{self.agent_id}.object.created",
            payload={"object": obj},
            caused_by=spine_trigger,
        )
        return obj_id

    def emit(
        self,
        span: Span,
        event_type: str,
        payload: dict[str, Any],
        caused_by: Optional[str] = None

    ):
        trace_id, span_id, parent_span_id = span_ids(span)
        caused_by = caused_by if caused_by is not None else self.state.parent_event_id
        event = self.store.append(
            CausalEvent.create(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                session_id=self.session_id,
                payload=payload,
                caused_by=caused_by,
                actor=self.agent_id,
                event_type=event_type,
            ))
        self.state.parent_event_id = event.event_id
        return event

    def reply(self, original: InterAgentMessage, result: Any, span: Span) -> InterAgentMessage:

        payload = {
            "result": result, "in_reply_to": original.message_id,
        }
        self.emit(
            span,
            event_type=f"{self.agent_id}.returned",
            payload=payload,

        )
        return InterAgentMessage(
            message_id=uuid.uuid4().hex,
            trace_id=original.trace_id,
            session_id=original.session_id,
            sender=self.agent_id,
            recipient=original.sender,
            payload=payload,
            caused_by_event_id=self.state.parent_event_id,
            delegation_token='',
            reply_to=original.message_id,

        )

    def send_message(self, recipient: str, payload: dict, delegation_token: str, caused_by: Optional[str] = None):
        with self.tracer.start_as_current_span(
            f"{self.agent_id}.send.{recipient}",
            kind=SpanKind.PRODUCER,

        ) as span:
            span.set_attribute('agent.id', self.agent_id)
            span.set_attribute("messaging.destination", recipient)
            span.set_attribute("workflow.session_id", self.session_id)
            span.set_attribute("workflow.root_trace_id", self.trace_id)
            event_type = f"{self.agent_id}.delegated"
            delegate_event = self.emit(
                span,
                event_type=event_type,
                caused_by=caused_by,
                payload={
                    "target": recipient,
                    "delegation_token": delegation_token,
                    **payload
                }
            )
            msg = InterAgentMessage.create(
                trace_id=self.state.trace_id,
                session_id=self.state.session_id,
                sender=self.agent_id,
                recipient=recipient,
                delegation_token=delegation_token,
                caused_by_event_id=delegate_event.event_id,
                payload=payload,
                delegation_span_ctx=span.get_span_context()

            )
            reply = self.bus.send(msg)
            event_type = f"{self.agent_id}.reply.received"

            self.emit(
                span,
                event_type=event_type,
                caused_by=reply.caused_by_event_id,
                payload={
                    "from": recipient,
                    "message_id": reply.message_id,
                    "result": reply.result,
                }
            )

            return reply

    def _llm_gateway(self, prompt: str) -> dict:
        h = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        return {
            "model": self.model,
            "prompt_hash": h,
            "output_hash": hashlib.sha256((prompt + "::out").encode()).hexdigest()[:12],
            "cost_usd": 0.0021,
            "tokens": 1200,
            "cache_hit": False,
        }

    def call_llm(self, prompt: str, caused_by: Optional[str] = None):
        with self.tracer.start_as_current_span(
            self._span_name(self.call_llm),
            kind=SpanKind.CLIENT,
        ) as llm_span:
            llm_span.set_attribute("llm.model", self.model)
            llm_span.set_attribute('agent.id', self.agent_id)
            llm_span.set_attribute("workflow.session_id", self.session_id)
            llm_span.set_attribute("workflow.root_trace_id", self.trace_id)
            request_event = self.emit(
                llm_span,
                f'{self.agent_id}.llm.requested',
                payload={"model": self.model, 'prompt': prompt},
                caused_by=caused_by,
            )
            try:
                response = self._llm_gateway(prompt)
                llm_span.set_attribute(
                    "llm.cost_usd", response.get('cost_usd', 0))
                response_event = self.emit(
                    llm_span,
                    event_type=f"{self.agent_id}.llm.responded",
                    payload=response,
                    caused_by=request_event.event_id
                )
                return request_event, response_event

            except Exception as exc:
                llm_span.record_exception(exc)
                self.emit(llm_span, f'{self.agent_id}.llm.failed', {
                          "error": str(exc)})
                raise

    def call_tool(
        self,
        tool_name: str,
        input_payload: dict,
        tool_fn: Callable,
        caused_by: Optional[str] = None,
    ):

        with self.tracer.start_as_current_span(
            self._span_name(self.call_tool),
            kind=SpanKind.CLIENT,

        ) as tool_span:
            tool_span.set_attribute('tool.name', tool_name)
            tool_span.set_attribute('agent.id', self.agent_id)
            tool_span.set_attribute("workflow.session_id", self.session_id)
            tool_span.set_attribute("workflow.root_trace_id", self.trace_id)
            request_event = self.emit(
                tool_span,
                f"{self.agent_id}.tool.requested",
                payload={"tool_name": tool_name, 'input': input_payload},
                caused_by=caused_by,
            )
            try:
                result = tool_fn(**input_payload)
                response_event = self.emit(
                    tool_span,
                    event_type=f'{self.agent_id}.tool.responded',
                    payload=result,
                    caused_by=request_event.event_id
                )
                return request_event, response_event
            except Exception as exc:
                tool_span.record_exception(exc)
                self.emit(
                    tool_span,
                    event_type=f'{self.agent_id}.tool.failed',
                    caused_by=request_event.event_id,
                    payload={"error": str(exc)}

                )
                raise


class AgentA(BaseAgent):

    def __init__(self, store, bus, agent_id, session_id, trace_id):
        super().__init__(store, bus, agent_id, session_id, trace_id)

    def handle_message(self, message: InterAgentMessage):

        with self.tracer.start_as_current_span(
            self._span_name(self.handle_message),
            kind=SpanKind.CONSUMER,
            context=Context(),
            links=self._incoming_links(message),
        ) as span:
            span.set_attribute('agent.id', self.agent_id)
            span.set_attribute("workflow.session_id", self.session_id)
            span.set_attribute("workflow.root_trace_id", self.trace_id)
            activated = self.emit(
                span=span,
                event_type=f"{self.agent_id}.activated",
                caused_by=message.caused_by_event_id,
                payload={"task_instruction": message.task_instruction}
            )

            llm_req, _response = self.call_llm(
                message.task_instruction, caused_by=activated.event_id)

            # those are from the llm but for now we use those placeholder
            target_agent = 'agent_b'
            task = message.task_instruction

            token = self.make_token(
                target_agent, audience=target_agent, task=task)

            reply = self.send_message(
                recipient=target_agent,
                payload={'task_instruction': task},
                delegation_token=token,
                caused_by=_response.event_id,


            )

            return self.reply(message, reply.result, span)


class AgentB(BaseAgent):

    def __init__(self, store, bus, agent_id, session_id, trace_id):
        super().__init__(store, bus, agent_id, session_id, trace_id)

    @staticmethod
    def lookup_data(query: str):
        return lookup_data(query)

    def handle_message(self, message: InterAgentMessage):

        with self.tracer.start_as_current_span(
            self._span_name(self.handle_message),
            kind=SpanKind.CONSUMER,
            context=Context(),
            links=self._incoming_links(message),
        ) as span:
            span.set_attribute('agent.id', self.agent_id)
            span.set_attribute("workflow.session_id", self.session_id)
            span.set_attribute("workflow.root_trace_id", self.trace_id)

            self.validate_token(message)

            activated = self.emit(
                span=span,
                event_type=f"{self.agent_id}.activated",
                caused_by=message.caused_by_event_id,
                payload={"task_instruction": message.task_instruction}
            )

            llm_req_evt, _response = self.call_llm(
                message.task_instruction, caused_by=activated.event_id)

            # those are from the llm but for now we use those placeholder
            tool_name = 'lookup_data'
            input_payload = {"query": message.task_instruction}

            tool_req, tool_resp = self.call_tool(
                tool_name=tool_name,
                input_payload=input_payload,
                tool_fn=self.lookup_data,
                caused_by=activated.event_id,
            )

            result = self._create_object(
                span=span,
                spine_trigger=activated.event_id,
                llm_req_evt=llm_req_evt,
                request_event=tool_req,
                response_event=tool_resp,
                delegation_event_id=message.caused_by_event_id
            )

            # Hand off to agent_c
            task = message.task_instruction
            token = self.make_token("agent_c", audience="agent_c", task=task)
            reply = self.send_message(
                recipient="agent_c",
                payload={"task_instruction": task},
                delegation_token=token,
                caused_by=_response.event_id,

            )

            return self.reply(message, reply.result, span)


class AgentC(BaseAgent):
    @staticmethod
    def transform_data(query: str):
        return transform_data(query)

    def handle_message(self, message: InterAgentMessage):

        with self.tracer.start_as_current_span(
            self._span_name(self.handle_message),
            kind=SpanKind.CONSUMER,
            context=Context(),
            links=self._incoming_links(message),
        ) as span:
            span.set_attribute("agent.id", self.agent_id)
            span.set_attribute("workflow.session_id", self.session_id)
            span.set_attribute("workflow.root_trace_id", self.trace_id)
            self.validate_token(message)

            activated = self.emit(
                span, event_type=f"{self.agent_id}.activated",
                caused_by=message.caused_by_event_id,
                payload={"task_instruction": message.task_instruction},
            )

            llm_req_evt, _response = self.call_llm(
                message.task_instruction, caused_by=activated.event_id)

            tool_req, tool_resp = self.call_tool(
                tool_name="transform_data",
                input_payload={"query": message.task_instruction},
                tool_fn=self.transform_data,
                caused_by=activated.event_id,

            )

            self._create_object(
                span=span,
                spine_trigger=activated.event_id,
                llm_req_evt=llm_req_evt,
                request_event=tool_req,
                response_event=tool_resp,
                delegation_event_id=message.caused_by_event_id,
            )

            task = message.task_instruction
            token = self.make_token("agent_d", audience="agent_d", task=task)
            reply = self.send_message(
                recipient="agent_d",
                payload={"task_instruction": task},
                delegation_token=token,
                caused_by=_response.event_id,

            )

            return self.reply(message, reply.result, span)


class AgentD(BaseAgent):
    @staticmethod
    def finalize_data(query: str):
        return finalize_data(query)

    def handle_message(self, message: InterAgentMessage):

        with self.tracer.start_as_current_span(
            self._span_name(self.handle_message),
            kind=SpanKind.CONSUMER,
            context=Context(),
            links=self._incoming_links(message),
        ) as span:
            span.set_attribute("agent.id", self.agent_id)
            span.set_attribute("workflow.session_id", self.session_id)
            span.set_attribute("workflow.root_trace_id", self.trace_id)
            self.validate_token(message)

            activated = self.emit(
                span, event_type=f"{self.agent_id}.activated",
                caused_by=message.caused_by_event_id,
                payload={"task_instruction": message.task_instruction},
            )

            llm_req_evt, _response = self.call_llm(
                message.task_instruction, caused_by=activated.event_id)

            tool_req, tool_resp = self.call_tool(
                tool_name="finalize_data",
                input_payload={"query": message.task_instruction},
                tool_fn=self.finalize_data,
                caused_by=activated.event_id,

            )

            result = self._create_object(
                span=span,
                spine_trigger=activated.event_id,
                llm_req_evt=llm_req_evt,
                request_event=tool_req,
                response_event=tool_resp,
                delegation_event_id=message.caused_by_event_id,
            )

            # Leaf agent — no further delegation, just reply upward
            return self.reply(message, result, span)
