"""OpenTelemetry tracing bootstrap.

FastMCP already wraps every ``tools/call`` (and other MCP operations) in a
SERVER-kind span via ``fastmcp.server.telemetry.server_span``. The spans
are a no-op until a consumer installs an SDK and registers a ``TracerProvider``.
This module is that registration step; it does not add any spans of its own.
Once configured, span attributes already include the tool name, session id, and auth context.

This only sets process-global OpenTelemetry state (the global
``TracerProvider``), so it must run once per process, including once per
``--workers`` child, since each is a separate interpreter. It's called from
``build_mcp_server``, which already runs in both the single-process and per-worker paths.
"""

import logging
from collections.abc import Mapping
from typing import Any

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.tracing")


def configure_tracing(settings: Mapping[str, Any]) -> bool:
    """Register a global TracerProvider if ``otel_enabled`` is set.

    Safe to call more than once per process, does nothing on later calls once a
    real provider is already registered, since OpenTelemetry itself only
    accepts the first ``set_tracer_provider`` call and warns on subsequent ones.

    Returns whether tracing is active after this call, so the caller can
    reflect the real state rather than just echoing the requested flag.
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

    # A no-op ProxyTracerProvider is the untouched default; a real provider
    # here means an earlier call already won.
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


def _build_exporter(kind: str, endpoint: str | None):
    if kind == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    if kind == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import \
                OTLPSpanExporter
        except ImportError:
            logger.warning(
                "otel_exporter=otlp but opentelemetry-exporter-otlp-proto-http "
                "isn't installed. tracing stays a no-op."
            )
            return None
        # Endpoint left as None falls back to the SDK's own default
        # resolution (OTEL_EXPORTER_OTLP_ENDPOINT env var, then http://localhost:4318/v1/traces).
        return OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    logger.warning(f"Unknown otel_exporter {kind!r}; tracing stays a no-op.")
    return None
