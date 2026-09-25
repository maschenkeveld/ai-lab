"""
Destination specialist — shortlists travel destinations via the MCP destinations adapter.

Receives: {"vibes": [...], "budget_level": "high", "activities": [...], "limit": 5, "origin": "AMS"}
Returns:  {"destinations": [...]}

Calls the destinations MCP adapter through agentgateway using MultiServerMCPClient.
Tools are discovered via tools/list at startup; the list_destinations tool filters by
vibes/budget/activities server-side (keyword match). The origin airport is excluded
post-filter, and the remaining candidates are then semantically re-ranked against the
trip request using OpenAI embeddings (via LiteLLM's `embedding-openai` alias).
"""

import json
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import OpenAIEmbeddings
from .otel import setup_otel

setup_otel("travel-destination")
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

AGENTGATEWAY_URL      = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
AGENT_NAME            = os.getenv("AGENT_NAME", "travel-destination")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination")

LITELLM_BASE_URL        = os.getenv("LITELLM_BASE_URL", "http://litellm.litellm.svc.cluster.local:4000/v1")
LITELLM_API_KEY         = os.getenv("LITELLM_API_KEY", "sk-ai-lab-litellm")
LITELLM_EMBEDDING_MODEL = os.getenv("LITELLM_EMBEDDING_MODEL", "embedding-openai")

_embeddings: OpenAIEmbeddings | None = None


def get_embeddings() -> OpenAIEmbeddings:
    """A LangChain embeddings client pointed at the in-cluster LiteLLM proxy."""
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_EMBEDDING_MODEL)
    return _embeddings


_mcp_client: MultiServerMCPClient | None = None
_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _tools
    _mcp_client = MultiServerMCPClient({
        "destinations": {
            "url": f"{AGENTGATEWAY_URL}/mcp-destinations",
            "transport": "streamable_http",
        }
    })
    _tools = await _mcp_client.get_tools()
    yield


app = FastAPI(title=AGENT_NAME, lifespan=lifespan)


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = Field(default_factory=lambda: str(uuid.uuid4()))
    method: str
    params: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/destination/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Returns a shortlist of travel destinations matching vibes, budget, and activities "
                       "via MCP tool calls to the destinations adapter.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "shortlist-destinations", "name": "Shortlist destinations", "tags": ["travel", "destinations", "mcp"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = await get_destinations(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


@traceable(name="get_destinations")
async def get_destinations(payload: dict[str, Any]) -> dict[str, Any]:
    origin     = payload.get("origin", "")
    budget     = payload.get("budget_level", "high")
    vibes      = payload.get("vibes", [])
    activities = payload.get("activities", [])
    limit      = int(payload.get("limit", 5))

    tool = next((t for t in _tools if t.name == "list_destinations"), None)
    if tool is None:
        return {"destinations": []}

    raw = await tool.ainvoke({
        "vibes": vibes,
        "budget_level": budget,
        "activities": activities,
        "limit": limit * 8,  # fetch a wider pool so semantic re-ranking has something to work with
    })
    data = _parse(raw)
    candidates = [
        d for d in data.get("destinations", [])
        if origin not in d.get("airport_iata_codes", [])
    ]
    destinations = await rank_by_similarity(candidates, vibes, activities, budget)
    return {"destinations": destinations[:limit]}


@traceable(name="rank_by_similarity")
async def rank_by_similarity(
    candidates: list[dict[str, Any]], vibes: list[str], activities: list[str], budget: str,
) -> list[dict[str, Any]]:
    """Re-rank keyword-filtered candidates by embedding similarity to the trip request.

    Falls back to the incoming (keyword-filtered) order if LiteLLM's embedding endpoint is
    unreachable, so a down embedding model degrades ranking quality rather than the request.
    """
    if len(candidates) <= 1:
        return candidates
    query = (
        f"A {budget}-budget trip with vibes: {', '.join(vibes) or 'any'}. "
        f"Preferred activities: {', '.join(activities) or 'any'}."
    )
    try:
        embeddings = get_embeddings()
        query_vector = await embeddings.aembed_query(query)
        document_vectors = await embeddings.aembed_documents([_destination_text(d) for d in candidates])
    except Exception:
        logger.warning("embedding-based ranking unavailable, falling back to keyword-filter order", exc_info=True)
        return candidates
    ranked = sorted(
        zip(candidates, document_vectors), key=lambda pair: _cosine_similarity(query_vector, pair[1]), reverse=True,
    )
    return [destination for destination, _ in ranked]


def _destination_text(d: dict[str, Any]) -> str:
    return ", ".join(filter(None, [
        d.get("name"), d.get("country"), d.get("blurb"),
        "vibes: " + "/".join(d.get("vibes", [])),
        "activities: " + "/".join(d.get("activities", [])),
    ]))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _parse(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
        raw = raw[0]["text"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    return {}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_destination.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4201")))
