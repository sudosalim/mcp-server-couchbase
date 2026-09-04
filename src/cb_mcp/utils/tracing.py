"""OpenTelemetry tracing bootstrap.

FastMCP already wraps every ``tools/call`` in a SERVER-kind span, but it's a
no-op until an SDK + ``TracerProvider`` are registered. This module does
that registration; it adds no spans of its own.

Sets process-global state, so it must run once per process (including once
per ``--workers`` child — see ``build_mcp_server``).
"""

import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

# opentelemetry-api is a hard dependency via fastmcp's "server" extra, so
# it's always installed. Only the SDK/exporters (below) are optional.
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.tracing")

# Own instrumentation scope, distinct from FastMCP's "fastmcp" tracer.
_TRACER_NAME = f"{MCP_SERVER_NAME}-mcp-server.couchbase"


def configure_tracing(settings: Mapping[str, Any]) -> bool:
    """Register a global TracerProvider if ``otel_enabled`` is set.

    Safe to call more than once per process — later calls no-op.

    Returns whether tracing is actually active, not just the requested flag.
    """
    if not settings.get("otel_enabled"):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "otel_enabled is set but the OpenTelemetry SDK isn't installed "
            "(pip install couchbase-mcp-server[otel], or opentelemetry-sdk "
            "directly). tracing stays a no-op."
        )
        return False

    # ProxyTracerProvider is the untouched default; anything else means an
    # earlier call already won.
    existing = trace.get_tracer_provider()
    if type(existing).__name__ != "ProxyTracerProvider":
        logger.debug("TracerProvider already configured; leaving it as-is.")
        return True

    exporter_kind = settings.get("otel_exporter") or "console"
    exporter = _build_exporter(exporter_kind, settings.get("otel_exporter_endpoint"))
    if exporter is None:
        return False

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: MCP_SERVER_NAME}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info(f"OpenTelemetry tracing enabled (exporter={exporter_kind})")
    return True


@contextmanager
def couchbase_span(
    operation: str, **attributes: str | int | float | bool
) -> Generator[Span, None, None]:
    """CLIENT-kind span around a single Couchbase SDK call.

    No-op unless tracing is enabled. Safe on every call site regardless.

    ``operation`` sets the span name (``couchbase.<operation>``) and the
    ``db.operation`` attribute. Extra kwargs become span attributes, e.g.
    ``bucket=...``. Never pass document/query content, only identifiers.
    """
    tracer = trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(
        f"couchbase.{operation}",
        kind=SpanKind.CLIENT,
        # Off to avoid double-recording; handled explicitly below.
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        if span.is_recording():
            span.set_attribute("db.system", "couchbase")
            span.set_attribute("db.operation", operation)
            for key, value in attributes.items():
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as e:
            if span.is_recording():
                span.set_attribute("error.type", type(e).__qualname__)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def _build_exporter(kind: str, endpoint: str | None):
    if kind == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    if kind == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            logger.warning(
                "otel_exporter=otlp but opentelemetry-exporter-otlp-proto-http "
                "isn't installed. tracing stays a no-op."
            )
            return None
        # None falls back to the SDK's own default (OTEL_EXPORTER_OTLP_ENDPOINT, then localhost:4318).
        return OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    logger.warning(f"Unknown otel_exporter {kind!r}; tracing stays a no-op.")
    return None
