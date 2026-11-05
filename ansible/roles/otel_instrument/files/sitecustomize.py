"""kolla-otel startup bootstrap.

This module is imported by Python's :mod:`site` mechanism at interpreter
startup (before any user code). It runs BEFORE the OTel agent because our
directory comes first in ``PYTHONPATH`` — so this file shadows the agent's
own ``sitecustomize.py``.

Responsibilities (in order):

1. **Early eventlet monkey-patch.** If ``OTEL_PYTHON_EVENTLET_MONKEY_PATCH``
   is set, calls ``eventlet.monkey_patch()`` immediately so that threading
   primitives are green-aware before any other import. This prevents
   ``BackendAlreadySelected`` crashes in eventlet daemons (nova-compute,
   nova-conductor, cinder-*).

2. **Agent bootstrap.** Explicitly loads the OTel auto-instrumentation
   package (equivalent to what the agent's own ``sitecustomize.py`` does).
   Without this step the agent would never start, because its own
   ``sitecustomize.py`` is shadowed by this file.

3. **Log-correlation patch.** Patches ``logging.Logger.handle`` so that
   ``record.otelTraceID`` and ``record.otelSpanID`` are always set before
   any handler or formatter sees the record. When an OTel span is active
   the real 32/16-hex IDs are stamped; otherwise the OTel-spec all-zero
   "invalid" IDs are used. This eliminates::

       ValueError: Formatting field not found in record: 'otelTraceID'

   which occurs when a green thread or background task emits a log line
   while no span is active and the format string references
   ``%(otelTraceID)s``.

Safety
------
Every step is wrapped in try/except so a failure in one step does not
prevent the remaining steps from running. A partially-initialised agent
cannot take down a service's log output or prevent it from starting.
"""

import os
import sys
import logging as _logging

# ---------------------------------------------------------------------------
# 1. Early eventlet monkey-patch
# ---------------------------------------------------------------------------
# Must run before the OTel agent loads. The agent imports threading-based
# modules during its initialisation; if eventlet is not already active,
# eventlet daemons (nova-compute, etc.) then fail with BackendAlreadySelected
# when they call init_backend(EVENTLET).
#
# eventlet.monkey_patch() is idempotent: it records which patches have been
# applied and skips them on subsequent calls.
if os.environ.get("OTEL_PYTHON_EVENTLET_MONKEY_PATCH", "").lower() in (
    "true", "1", "yes"
):
    try:
        import eventlet.monkey_patch as _monkey_patch
        _monkey_patch()
    except Exception:
        # eventlet not installed — the service will crash later with a more
        # descriptive error; don't mask it here.
        pass

# ---------------------------------------------------------------------------
# 2. Bootstrap the OTel agent
# ---------------------------------------------------------------------------
# Our directory is first in PYTHONPATH, so Python's site module imported THIS
# file instead of the agent's own sitecustomize.py. We bootstrap the agent
# explicitly here to ensure it starts.
try:
    from opentelemetry.instrumentation.auto_instrumentation._load import _load
    _load()
except ImportError:
    # Agent package not present in this container — run without tracing.
    pass
except Exception as _exc:
    _logging.getLogger("kolla_otel").warning(
        "OTel agent bootstrap failed: %s", _exc
    )

# ---------------------------------------------------------------------------
# 3. Log-correlation patch
# ---------------------------------------------------------------------------
# Guarantees record.otelTraceID / record.otelSpanID exist on every log
# record before any handler or formatter sees it.
#
# Patches Logger.handle (not a logger filter) so it runs for every logger
# instance and every handler regardless of creation order — which is exactly
# the gap that breaks the OTel agent's own handler-level filter when oslo.log
# configures its handlers after the agent loads, or when a green thread has
# no active span.

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


_original_handle = _logging.Logger.handle


def _patched_handle(self, record):
    _ensure_otel_ids(record)
    return _original_handle(self, record)


_logging.Logger.handle = _patched_handle
