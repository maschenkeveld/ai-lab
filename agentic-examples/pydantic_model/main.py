"""Simple example of using Pydantic AI to construct a Pydantic model from a text input.

Local copy of pydantic_ai_examples.pydantic_model (pydantic-ai 2.31.1, MIT).
Source: https://pydantic.dev/docs/ai/examples/getting-started/pydantic-model/

Changes from upstream: the model is served by local vLLM (OpenAI-compatible API, no proxy) instead
of a hosted model, and the Caching() capability is left out. The folder is self-contained: nothing
is imported from elsewhere in the repo.

Run with (needs `task inference:run` in another terminal):

    task run        # in this folder

Environment (running this file needs VLLM_API_KEY and VLLM_MODEL; importing it needs nothing):
    VLLM_API_KEY    the key `task inference:run` serves with
    VLLM_MODEL      the model vLLM is serving
    VLLM_BASE_URL   optional, default http://127.0.0.1:8000/v1
    OTEL_EXPORTER_OTLP_ENDPOINT
                    send traces to an OTLP/HTTP collector, e.g. the lab's LGTM at http://grafana.lab:4318;
                    unset = nothing is sent
    AGENT_TRACE_CONTENT=1   include prompts and responses in spans (off by default)
"""

import os

from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


class MyModel(BaseModel):
    city: str
    country: str


# The agent is defined without a model; every caller supplies one. Here it is the vLLM server
# (server_model); evals.py supplies models loaded in-process with the vLLM Python API.
agent = Agent(output_type=MyModel)


def server_model() -> OpenAIChatModel:
    """The model served by `task inference:run` (vLLM's OpenAI-compatible API)."""
    client = AsyncOpenAI(
        base_url=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=os.environ["VLLM_API_KEY"],
    )
    return OpenAIChatModel(os.environ["VLLM_MODEL"], provider=OpenAIProvider(openai_client=client))


_tracer_provider = None


def configure_telemetry(service_name: str) -> None:
    """Export Pydantic AI's spans (agent runs, model calls, tool calls) to the OTLP collector, if one is set."""
    global _tracer_provider
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _tracer_provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(_tracer_provider)
    Agent.instrument_all(InstrumentationSettings(include_content=os.environ.get("AGENT_TRACE_CONTENT") == "1"))


def shutdown_telemetry() -> None:
    """Flush spans that are still queued; call before the process exits."""
    if _tracer_provider is not None:
        _tracer_provider.shutdown()


if __name__ == "__main__":
    configure_telemetry("pydantic_model")
    model = server_model()
    print(f"Using model: {model.model_name}")
    result = agent.run_sync("The windy city in the US of A.", model=model)
    print(result.output)
    print(result.usage)
    shutdown_telemetry()
