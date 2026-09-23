"""
OpenTelemetry setup and span-ID helpers.
"""
from __future__ import annotations

from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

_tracer: Optional[trace.Tracer] = None


def setup_tracer(
    service_name: str = "multi-agent-trial",
    verbose: bool = False,
) -> trace.Tracer:
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    if verbose:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


def get_tracer(verbose: bool = False,) -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = setup_tracer(verbose=verbose)
    return _tracer


def reset_tracer_for_testing() -> None:
    """Clear the cached tracer so the next get_tracer() reinitialises.

    Note: OTel's set_tracer_provider is honoured only once per process.
    Tests that need a fresh provider must run in a fresh interpreter.
    """
    global _tracer
    _tracer = None


def span_ids(span) -> tuple[str, str, Optional[str]]:
    """Return (trace_id_hex, span_id_hex, parent_span_id_hex_or_None).

    Contract #1: parent_span_id is taken from span.parent, never invented.
    Returns None for root spans or invalid parents.
    """
    ctx = span.get_span_context()
    parent = span.parent
    return (
        format(ctx.trace_id, "032x"),
        format(ctx.span_id, "016x"),
        format(parent.span_id, "016x")
        if parent and parent.is_valid
        else None,
    )
