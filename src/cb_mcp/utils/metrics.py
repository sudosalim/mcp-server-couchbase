"""Prometheus ``/metrics`` endpoint.

Exposes ``prometheus_client``'s default collectors process, GC, and platform (Python version, for labeling).
"""

import logging

from fastmcp import FastMCP

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.metrics")

METRICS_PATH = "/metrics"


def register_metrics_route(mcp: FastMCP, enabled: bool) -> None:
    """Register the ``/metrics`` route on ``mcp`` if ``enabled``.

    Must run before the Starlette app is built from ``mcp`` (i.e. before
    ``mcp.run()`` or ``mcp.http_app()``), since ``custom_route`` registers
    onto the FastMCP instance itself. No-op for non-HTTP transports: stdio
    has no HTTP surface to attach a route to, so it's the caller's job to
    only enable this for network transports.
    """
    if not enabled:
        return

    try:
        from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY,
                                       generate_latest)
    except ImportError:
        logger.warning(
            "metrics_enabled is set but prometheus-client isn't installed "
            "(pip install couchbase-mcp-server[otel]) — /metrics stays absent."
        )
        return

    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route(METRICS_PATH, methods=["GET"], include_in_schema=False)
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    logger.info(f"Prometheus metrics exposed at {METRICS_PATH}")
