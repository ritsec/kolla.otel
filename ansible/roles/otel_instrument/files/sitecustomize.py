"""kolla-otel log-correlation bootstrap.

Guarantees ``record.otelTraceID`` and ``record.otelSpanID`` exist on every
log record before any handler or formatter sees it, eliminating the::

    ValueError: Formatting field not found in record: 'otelTraceID'

crash that occurs when a green thread (eventlet) or any background thread
emits a log line while no OTel span is active.

When an OTel span *is* active the real 32/16-hex IDs are stamped; otherwise
the OTel-spec all-zero "invalid" IDs are used so the Datadog log-pipeline
grok rule (``[0-9a-fA-F]{16,32}`` / ``[0-9a-fA-F]{8,16}``) still matches and
uncorrelated lines pass through with a valid-but-null ID instead of raising.

Mechanism
---------
Patches ``logging.Logger.handle`` so the IDs are set before the normal
handle path (filter check → handler dispatch) runs. This covers every logger
instance and every handler regardless of creation order — which is exactly
the gap that breaks the OTel agent's own handler-level filter when oslo.log
configures its handlers after the agent loads, or when a green thread has no
active span.

The module is auto-imported by Python's :mod:`site` mechanism: the role
stages it into the agent mount directory (``/otel-auto-instrumentation-python``)
which is already on ``PYTHONPATH``, so no extra env-var change is required.

Safety
------
The patched code never raises: any import or SDK error falls through to the
zero-ID default, so a partially-initialised agent can not take down a
service's log output.
"""

import logging

_ZERO_TRACE_ID = "0" * 32   # OTel "invalid trace ID"
_ZERO_SPAN_ID = "0" * 16    # OTel "invalid span ID"


def _ensure_otel_ids(record):
    """Set ``otelTraceID`` / ``otelSpanID`` on *record* if not already present.

    Reads the active OTel span when one exists; otherwise stamps the all-zero
    "invalid" IDs so format strings referencing ``%(otelTraceID)s`` and
    ``%(otelSpanID)s`` always resolve.
    """
    # Already stamped by a preceding filter (e.g. the OTel agent's own
    # log-correlation filter) — leave the real IDs alone.
    if getattr(record, "otelTraceID", None) and getattr(record, "otelSpanID", None):
        return

    trace_id = _ZERO_TRACE_ID
    span_id = _ZERO_SPAN_ID
    try:
        from opentelemetry.trace import get_current_span  # noqa: WPS433
        ctx = get_current_span().get_span_context()
        if ctx is not None and getattr(ctx, "is_valid", False):
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
    except Exception:
        # SDK not loaded, no active span, or any other error — fall through to
        # the zero-ID default. Never propagate an exception from here: it would
        # break the service's log output entirely.
        pass

    record.otelTraceID = trace_id
    record.otelSpanID = span_id


# --- Patch Logger.handle -----------------------------------------------------
# ``Logger.filter()`` only checks that logger's *own* filter list, not
# ancestors', so a filter attached to the root logger is never consulted when
# a child logger (e.g. ``nova.compute.manager``) is used directly. Patching
# ``handle`` — which every log call goes through, for every logger instance —
# guarantees the IDs are present before any handler dispatch or formatting.
_original_handle = logging.Logger.handle


def _patched_handle(self, record):
    _ensure_otel_ids(record)
    return _original_handle(self, record)


logging.Logger.handle = _patched_handle
