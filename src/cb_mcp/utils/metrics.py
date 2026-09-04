"""Prometheus ``/metrics`` endpoint.

Exposes ``prometheus_client``'s default collectors: process, GC, platform.
Also an event-loop-lag histogram, sampled by a background task.
"""

import asyncio
import functools
import logging

from fastmcp import FastMCP

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.metrics")

METRICS_PATH = "/metrics"
_LAG_SAMPLE_INTERVAL_SECONDS = 0.05


def register_metrics_route(mcp: FastMCP, enabled: bool) -> None:
    """Register the ``/metrics`` route on ``mcp`` if ``enabled``.

    Must run before the Starlette app is built (before ``mcp.run()`` /
    ``http_app()``). Caller must gate ``enabled`` to network transports.
    """
    if not enabled:
        return

    try:
        from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
    except ImportError:
        logger.warning(
            "metrics_enabled is set but prometheus-client isn't installed "
            "(pip install couchbase-mcp-server[otel]). /metrics stays absent."
        )
        return

    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route(METRICS_PATH, methods=["GET"], include_in_schema=False)
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    logger.info(f"Prometheus metrics exposed at {METRICS_PATH}")


@functools.cache
def _get_lag_histogram():
    from prometheus_client import Histogram

    return Histogram(
        "event_loop_lag_seconds",
        "Delay between a scheduled wakeup and when it actually ran",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    )


def start_event_loop_lag_monitor(enabled: bool) -> asyncio.Task | None:
    """Sample event-loop scheduling lag if ``enabled``.

    Wakes up on a fixed interval and measures how late the wakeup actually
    ran. This is a direct signal of event-loop contention, independent of any one
    code path. Must be called from a running event loop.
    """
    if not enabled:
        return None

    try:
        histogram = _get_lag_histogram()
    except ImportError:
        return None

    async def _monitor() -> None:
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(_LAG_SAMPLE_INTERVAL_SECONDS)
            lag = loop.time() - start - _LAG_SAMPLE_INTERVAL_SECONDS
            histogram.observe(max(0.0, lag))

    logger.info("Event-loop lag monitor started")
    return asyncio.create_task(_monitor())
