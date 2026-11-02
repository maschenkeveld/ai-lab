"""Which model to call, and where traces go. Every example has its own copy of this file on purpose: each folder
is self-contained, so change it freely without touching the others.

By default the model is OpenAI's gpt-4o-mini through the lab's LLM gateway (agentgateway): http://agentgateway.lab/llm/openai/v1,
with the gateway's client key. The gateway picks the model by *route*, so `--llm gemini|ollama|local` (or LLM_ROUTE) selects another;
set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL to use any other OpenAI-compatible endpoint. `task run:local` skips the gateway and
talks to the vLLM server on your Mac directly (start it with `cd k8s && task inference:run`).
"""
import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

ROUTES = {"openai": "gpt-4o-mini", "gemini": "gemini-2.5-pro", "ollama": "llama3.2", "local": "local"}  # route -> model label


def build_model(route: str | None = None) -> OpenAIChatModel:
    """The chat model for a gateway route (the gateway replaces the model name it is sent with the backend's own)."""
    if route:
        base_url, model = f"http://agentgateway.lab/llm/{route}/v1", ROUTES.get(route, route)
    else:
        env_route = os.getenv("LLM_ROUTE", "openai")
        base_url = os.getenv("LLM_BASE_URL") or f"http://agentgateway.lab/llm/{env_route}/v1"
        model = os.getenv("LLM_MODEL") or ROUTES.get(env_route, env_route)
    provider = OpenAIProvider(base_url=base_url, api_key=os.getenv("LLM_API_KEY", "sk-ai-lab-gateway"))
    return OpenAIChatModel(model, provider=provider)


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
