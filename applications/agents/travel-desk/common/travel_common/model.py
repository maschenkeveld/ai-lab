"""LLM access: every agent talks to agentgateway's OpenAI-compatible LLM routes, never to a provider directly.

The gateway chooses the model by *route* (`/llm/openai`, `/llm/gemini`, `/llm/ollama`, `/llm/local`) and replaces whatever
`model` the client sends with the backend's own, so LLM_MODEL is only a label (it shows up in traces). Point LLM_BASE_URL at
another route to change model. Embeddings only exist on the OpenAI route, so they have their own URL.

    LLM_BASE_URL             default http://agentgateway.lab/llm/openai/v1
    LLM_API_KEY              the gateway's client key (the gateway holds the provider keys)
    LLM_MODEL                default gpt-4o-mini
    LLM_EMBEDDINGS_BASE_URL  default http://agentgateway.lab/llm/openai/v1
    LLM_EMBEDDING_MODEL      default text-embedding-3-small
"""
import os

from pydantic_ai import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

LLM_BASE_URL = "http://agentgateway.lab/llm/openai/v1"
LLM_API_KEY = "sk-ai-lab-gateway"
LLM_MODEL = "gpt-4o-mini"
LLM_EMBEDDING_MODEL = "text-embedding-3-small"


def _provider(base_url_var: str = "LLM_BASE_URL") -> OpenAIProvider:
    return OpenAIProvider(
        base_url=os.getenv(base_url_var) or (os.getenv("LLM_BASE_URL", LLM_BASE_URL) if base_url_var == "LLM_BASE_URL" else LLM_BASE_URL),
        api_key=os.getenv("LLM_API_KEY", LLM_API_KEY),
    )


def build_embedder(model_name: str | None = None) -> Embedder:
    """An embedder behind the gateway's OpenAI route."""
    name = model_name or os.getenv("LLM_EMBEDDING_MODEL", LLM_EMBEDDING_MODEL)
    return Embedder(OpenAIEmbeddingModel(name, provider=_provider("LLM_EMBEDDINGS_BASE_URL")))


def build_model(model_name: str | None = None) -> OpenAIChatModel:
    """A chat model behind the gateway. The route in LLM_BASE_URL decides which model actually answers."""
    return OpenAIChatModel(model_name or os.getenv("LLM_MODEL", LLM_MODEL), provider=_provider())
