"""Prometheus ``/metrics`` endpoint.

Exposes ``prometheus_client``'s default collectors: process, GC, platform.
Also an event-loop-lag histogram (background task) and a thread-pool
queue-wait/execution split (monkeypatch — see
``instrument_thread_pool_queue_wait``).
"""

import asyncio
import functools
import logging
import time

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


@functools.cache
def _get_thread_pool_histograms():
    from prometheus_client import Histogram

    buckets = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
    return (
        Histogram(
            "thread_pool_queue_wait_seconds",
            "Time from requesting a thread-pool worker to it actually starting",
            buckets=buckets,
        ),
        Histogram(
            "thread_pool_execution_seconds",
            "Time a tool call spent actually running once dispatched",
            buckets=buckets,
        ),
    )


def _patch_call_sync_fn_in_threadpool(
    module, queue_wait_histogram, execution_histogram
) -> bool:
    """Wrap one module's bound ``call_sync_fn_in_threadpool`` reference.

    A ``from .async_utils import call_sync_fn_in_threadpool`` binds a local
    name in the importing module, so patching the original in
    ``fastmcp.utilities.async_utils`` wouldn't reach callers that already
    imported it as each importing module needs its own patch. Returns
    whether it patched (False if already instrumented).
    """
    original = module.call_sync_fn_in_threadpool
    if getattr(original, "_cb_mcp_instrumented", False):
        return False

    async def patched(fn, *args, **kwargs):
        t_submit = time.monotonic()
        t_start: list[float] = []

        def timed_fn(*a, **kw):
            t_start.append(time.monotonic())
            return fn(*a, **kw)

        result = await original(timed_fn, *args, **kwargs)
        t_end = time.monotonic()
        started = t_start[0] if t_start else t_end
        queue_wait_histogram.observe(max(0.0, started - t_submit))
        execution_histogram.observe(max(0.0, t_end - started))
        return result

    patched._cb_mcp_instrumented = True
    module.call_sync_fn_in_threadpool = patched
    return True


def instrument_thread_pool_queue_wait(enabled: bool) -> None:
    """Split FastMCP's sync-tool dispatch into queue-wait vs. execution time.

    Monkeypatches the ``call_sync_fn_in_threadpool`` reference in every
    FastMCP module that calls it. Tools with a ``Context``/``Depends()``
    parameter (every tool here) go through
    ``fastmcp.server.dependencies``'s wrapper; tools without one go through
    ``fastmcp.tools.function_tool`` directly as both are patched so the
    split covers either shape. Queue wait is the gap between requesting a
    worker thread and the thread actually starting; anything after that is
    real execution time.

    No-op (with a warning) if these internals have moved as this is coupled
    to FastMCP's implementation, not its public API.
    """
    if not enabled:
        return

    try:
        queue_wait_histogram, execution_histogram = _get_thread_pool_histograms()
    except ImportError:
        return

    patched_any = False
    for module_path in (
        "fastmcp.server.dependencies",
        "fastmcp.tools.function_tool",
    ):
        try:
            module = __import__(module_path, fromlist=["call_sync_fn_in_threadpool"])
            patched_any |= _patch_call_sync_fn_in_threadpool(
                module, queue_wait_histogram, execution_histogram
            )
        except (ImportError, AttributeError):
            logger.warning(
                f"Could not instrument {module_path} — internals may have moved."
            )

    if patched_any:
        logger.info("Thread-pool queue-wait/execution split enabled")
