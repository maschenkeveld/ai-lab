"""OpenTelemetry bootstrap — traces, metrics, and logs via OTLP/HTTP to the LGTM stack.

Call setup_otel() once before creating the FastAPI/Starlette/FastMCP app.
FastAPIInstrumentor adds server spans to every inbound request.
HTTPXClientInstrumentor injects traceparent into every outbound httpx call,
propagating the trace through A2A hops and MCP tool calls.
"""
import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_log = logging.getLogger(__name__)
_initialized = False


def setup_otel(default_service_name: str = "python-service") -> None:
    """Initialize OTEL providers and global auto-instrumentation. Idempotent."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    service_name = os.getenv("OTEL_SERVICE_NAME", default_service_name)
    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-lgtm.otel-lgtm.svc.cluster.local:4318",
    )
    resource = Resource.create({"service.name": service_name})

    # Traces
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(tp)

    # Metrics
    mp = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        )],
    )
    metrics.set_meter_provider(mp)

    # Logs — OTLP export + bridge Python stdlib logging so trace/span IDs appear in log records
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    from opentelemetry import _logs as _otel_logs_api
    _otel_logs_api.set_logger_provider(lp)
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Auto-instrument: FastAPI/Starlette server spans + httpx client spans with traceparent injection
    FastAPIInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    _log.info("OTEL initialized  service=%s  endpoint=%s", service_name, endpoint)
