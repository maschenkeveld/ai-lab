"""
Destination specialist — RAG-backed wrapper around the destinations REST API.

Receives: {"vibes": [...], "budget_level": "high", "activities": [...], "limit": 5, "origin": "AMS"}
Returns:  {"destinations": [...]}

On startup, fetches the full destination catalog from the REST API and embeds it into an
in-memory FAISS index. Embeddings are computed via the in-cluster LiteLLM proxy (OpenAI
text-embedding-3-small by default) rather than a local model — no local ML runtime, so this
service stays as lightweight as the other specialists. Each request synthesizes a
natural-language query from the requested vibes/budget/activities and runs a semantic
similarity search over the catalog, then applies the same hard guardrails the old tag-matching
version enforced (exact budget match, exclude the origin airport) as a post-filter before
truncating to the requested limit.
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

API_BASE_URL    = os.getenv("API_BASE_URL", "http://traefik.traefik.svc.cluster.local")
API_HOST_HEADER = os.getenv("API_HOST_HEADER", "traefik-api-gw.lab")
AGENT_NAME      = os.getenv("AGENT_NAME", "travel-destination")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination")

LITELLM_BASE_URL      = os.getenv("LITELLM_BASE_URL", "http://litellm.litellm.svc.cluster.local:4000/v1")
LITELLM_API_KEY       = os.getenv("LITELLM_API_KEY", "sk-ai-lab-litellm")
LITELLM_EMBEDDING_MODEL = os.getenv("LITELLM_EMBEDDING_MODEL", "embedding-openai")
CATALOG_FETCH_LIMIT   = int(os.getenv("CATALOG_FETCH_LIMIT", "500"))

_vectorstore: FAISS | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _vectorstore
    _vectorstore = build_vectorstore()
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
                       "via semantic search over the destination catalog.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "shortlist-destinations", "name": "Shortlist destinations", "tags": ["travel", "destinations", "rag"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = get_destinations(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


def fetch_catalog() -> list[dict[str, Any]]:
    response = httpx.get(
        f"{API_BASE_URL}/destinations/v1/destinations",
        params={"limit": CATALOG_FETCH_LIMIT},
        headers={"host": API_HOST_HEADER},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json().get("destinations", [])


def render_destination(d: dict[str, Any]) -> str:
    return (
        f"{d.get('name')}, {d.get('country')} ({d.get('region')}). "
        f"Budget: {d.get('budget_level')}. Vibes: {', '.join(d.get('vibes', []))}. "
        f"Activities: {', '.join(d.get('activities', []))}. {d.get('blurb', '')}"
    )


def build_vectorstore() -> "FAISS | None":
    try:
        catalog = fetch_catalog()
    except Exception:
        return None
    if not catalog:
        return None
    embeddings = OpenAIEmbeddings(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_EMBEDDING_MODEL)
    documents = [Document(page_content=render_destination(d), metadata=d) for d in catalog]
    return FAISS.from_documents(documents, embeddings)


@traceable(name="get_destinations")
def get_destinations(payload: dict[str, Any]) -> dict[str, Any]:
    origin     = payload.get("origin", "")
    budget     = payload.get("budget_level", "high")
    vibes      = payload.get("vibes", [])
    activities = payload.get("activities", [])
    limit      = payload.get("limit", 5)

    query = (
        f"A trip with a {budget} budget, vibes: {', '.join(vibes) or 'any'}, "
        f"activities: {', '.join(activities) or 'any'}."
    )

    if _vectorstore is not None:
        hits = _vectorstore.similarity_search(query, k=max(limit * 4, 20))
        candidates = [hit.metadata for hit in hits]
    else:
        candidates = fetch_catalog()

    destinations = [
        d for d in candidates
        if d.get("budget_level") == budget and origin not in d.get("airport_iata_codes", [])
    ][:limit]
    return {"destinations": destinations}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON payload out of the A2A message text field."""
    message = params.get("message")
    if isinstance(message, dict):
        parts = message.get("parts", [])
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_destination.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4201")))
