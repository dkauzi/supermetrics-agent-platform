"""OpenTelemetry spans, alongside the decision trace.

Why both, since this is the obvious question:

They answer different questions, and neither answers the other's.

* **OpenTelemetry** answers *"where did the time go, and what called what?"* It is
  the right tool for latency, dependency maps and correlating this service with
  everything else in the estate. It is a well-supported standard, so Cloud Trace,
  Jaeger, Tempo, Datadog and Honeycomb all work with no code change.

* **The decision trace** (`run_steps`) answers *"why did this agent conclude
  that?"* Which rule matched, which values it matched on, which prompt version
  produced the analysis, what a human later said about it. That is a business
  record with a retention policy, queryable in SQL by an analyst joining it to
  revenue. Span attributes in a tracing backend are sampled, expire in weeks, and
  no CS lead is ever going to open Jaeger.

Using OTel for the second job is the common mistake. Sampling means you lose the
one run someone asks about, and "why did the agent do that" becomes unanswerable
precisely when it matters.

So: every step emits both. The OTel span carries our `trace_id` as an attribute
and the decision row carries the OTel trace and span ids, so you can jump from
a latency spike in Jaeger to the business reason for that run, and back.

The dependency is optional. Without `opentelemetry-sdk` installed this degrades
to no-ops and the platform is unaffected, because observability tooling must
never be able to take down the thing it observes.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_TRACER: Any = None
_ENABLED = False


def init_telemetry(service_name: str = "agent-platform") -> bool:
    """Wire up the tracer if the SDK is present and an endpoint is configured.

    Returns whether tracing is live, so `/healthz` can report it honestly rather
    than implying spans are being exported when nothing is listening.
    """
    global _TRACER, _ENABLED

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # Explicitly not an error. Running without the SDK is a supported mode.
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    _TRACER = trace.get_tracer(service_name)
    _ENABLED = True
    return True


def enabled() -> bool:
    return _ENABLED


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[dict[str, str]]:
    """Open a span, yielding its ids so they can be stored on the decision row.

    Yields empty ids when tracing is off, so callers need no branching.
    """
    if not _ENABLED or _TRACER is None:
        yield {}
        return

    with _TRACER.start_as_current_span(name) as otel_span:
        for key, value in attributes.items():
            if value is not None:
                otel_span.set_attribute(key, value)

        context = otel_span.get_span_context()
        ids = {
            "otel_trace_id": format(context.trace_id, "032x"),
            "otel_span_id": format(context.span_id, "016x"),
        }
        try:
            yield ids
        except Exception as exc:
            otel_span.record_exception(exc)
            _set_error(otel_span, str(exc))
            raise


def _set_error(otel_span: Any, message: str) -> None:
    try:
        from opentelemetry.trace import Status, StatusCode
        otel_span.set_status(Status(StatusCode.ERROR, message))
    except ImportError:
        pass
