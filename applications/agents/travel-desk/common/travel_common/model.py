"""LLM access — every agent talks to LiteLLM's OpenAI-compatible endpoint, never to a provider directly."""
import os

from pydantic_ai import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

LITELLM_BASE_URL = "http://litellm.litellm.svc.cluster.local:4000/v1"
LITELLM_API_KEY = "sk-ai-lab-litellm"
LITELLM_MODEL = "openai-gpt"
LITELLM_EMBEDDING_MODEL = "embedding-openai"


def _provider() -> OpenAIProvider:
    return OpenAIProvider(
        base_url=os.getenv("LITELLM_BASE_URL", LITELLM_BASE_URL),
        api_key=os.getenv("LITELLM_API_KEY", LITELLM_API_KEY),
    )


def build_embedder(alias: str | None = None) -> Embedder:
    """An embedder behind LiteLLM (default alias `embedding-openai`)."""
    name = alias or os.getenv("LITELLM_EMBEDDING_MODEL", LITELLM_EMBEDDING_MODEL)
    return Embedder(OpenAIEmbeddingModel(name, provider=_provider()))


def build_model(alias: str | None = None) -> OpenAIChatModel:
    """A chat model behind LiteLLM. `alias` is a LiteLLM model alias (see k8s/oss/litellm/config.yaml)."""
    return OpenAIChatModel(alias or os.getenv("LITELLM_MODEL", LITELLM_MODEL), provider=_provider())
