"""Which model to call, and where traces go. Every example has its own copy of this file on purpose: each folder
is self-contained, so change it freely without touching the others.

By default the model is `openai-gpt` behind the lab's LiteLLM (http://litellm.lab/v1). To use anything else that
speaks the OpenAI API, set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL; `task run:local` does that for the vLLM
server on your Mac (started with `cd k8s && task inference:run`).
"""
import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def build_model(name: str | None = None) -> OpenAIChatModel:
    provider = OpenAIProvider(
        base_url=os.getenv("LLM_BASE_URL", "http://litellm.lab/v1"),
        api_key=os.getenv("LLM_API_KEY", "sk-ai-lab-litellm"),
    )
    return OpenAIChatModel(name or os.getenv("LLM_MODEL", "openai-gpt"), provider=provider)


_provider = None


def configure_telemetry(service_name: str) -> None:
    """Send Pydantic AI's spans (agent run, model call, tool call) to an OTLP collector, if OTEL_EXPORTER_OTLP_ENDPOINT
    is set (the lab's is http://grafana.lab:4318). Unset = nothing is sent. Prompts are included only with AGENT_TRACE_CONTENT=1."""
    global _provider
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    _provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(_provider)
    Agent.instrument_all(InstrumentationSettings(include_content=os.getenv("AGENT_TRACE_CONTENT") == "1"))


def shutdown_telemetry() -> None:
    """Flush queued spans; call before the process exits."""
    if _provider is not None:
        _provider.shutdown()
