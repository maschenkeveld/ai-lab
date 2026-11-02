"""OpenTelemetry bootstrap — traces, metrics and logs via OTLP/HTTP to the LGTM stack.

Call setup_otel() once, before creating the app. Starlette instrumentation adds a server span to every
inbound request; httpx instrumentation injects `traceparent` into every outbound call, so one trace
follows a request through every A2A hop and MCP tool call; Pydantic AI's own instrumentation adds the
agent-run, model-request and tool-call spans.
"""
import logging
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from opentelemetry import _logs as otel_logs
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent

OTEL_ENDPOINT = "http://otel-lgtm.otel-lgtm.svc.cluster.local:4318"

_log = logging.getLogger(__name__)
_initialized = False


def setup_otel(default_service_name: str = "python-service") -> None:
    """Initialize OTEL providers and instrumentation. Idempotent."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    service_name = os.getenv("OTEL_SERVICE_NAME", default_service_name)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", OTEL_ENDPOINT)
    resource = Resource.create({"service.name": service_name})

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(tp)

    mp = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))],
    )
    metrics.set_meter_provider(mp)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")))
    otel_logs.set_logger_provider(lp)
    LoggingInstrumentor().instrument(set_logging_format=True)

    StarletteInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    Agent.instrument_all()

    _log.info("OTEL initialized  service=%s  endpoint=%s", service_name, endpoint)
