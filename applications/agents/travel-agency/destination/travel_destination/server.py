"""Destination agent — shortlists travel destinations that fit the trip request.

A2A in:  DestinationRequest {"vibes": [...], "budget_level": "high", "activities": [...], "limit": 5, "origin": "AMS"}
A2A out: DestinationResult  {"destinations": [...]}

`shortlist_destinations` calls the destinations MCP server (via agentgateway) for a keyword-filtered pool,
drops destinations reachable from the origin airport, then re-ranks the rest by embedding similarity to the
trip request (LiteLLM's `embedding-openai` alias). If the embedding call fails it keeps the keyword order,
so a down embedding model degrades ranking quality rather than failing the request.
"""
import logging
import math
import os

from travel_common.otel import setup_otel

setup_otel("travel-destination")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent, Embedder
from travel_common.a2a_server import create_a2a_app, current_request
from travel_common.contracts import Destination, DestinationRequest, DestinationResult
from travel_common.mcp import mcp_toolset
from travel_common.model import build_embedder, build_model

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "travel-destination")
PREFIX = "/a2a/destination"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

INSTRUCTIONS = """\
You shortlist destinations for a trip. The trip request is already available to your tool, so call
shortlist_destinations exactly once (it takes no arguments) and return its result unchanged as
destinations. Never invent or alter destinations."""


def destination_text(d: dict) -> str:
    return ", ".join(filter(None, [
        d.get("name"), d.get("country"), d.get("blurb"),
        "vibes: " + "/".join(d.get("vibes", [])),
        "activities: " + "/".join(d.get("activities", [])),
    ]))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a, norm_b = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


async def rank_by_similarity(candidates: list[dict], request: DestinationRequest, embedder: Embedder) -> list[dict]:
    """Re-rank keyword-filtered candidates by embedding similarity to the trip request."""
    if len(candidates) <= 1:
        return candidates
    query = (f"A {request.budget_level or 'any'}-budget trip with vibes: {', '.join(request.vibes) or 'any'}. "
             f"Preferred activities: {', '.join(request.activities) or 'any'}.")
    try:
        query_vector = (await embedder.embed_query(query)).embeddings[0]
        doc_vectors = (await embedder.embed_documents([destination_text(d) for d in candidates])).embeddings
    except Exception:
        log.warning("embedding-based ranking unavailable, keeping keyword-filter order", exc_info=True)
        return candidates
    ranked = sorted(zip(candidates, doc_vectors), key=lambda pair: cosine(query_vector, pair[1]), reverse=True)
    return [candidate for candidate, _ in ranked]


def parse_pool(raw) -> list[dict]:
    """The destinations list from an MCP tool result (structured dict, or a dict wrapped as `result`)."""
    if isinstance(raw, dict):
        raw = raw.get("result", raw) if "destinations" not in raw else raw
        return list(raw.get("destinations", [])) if isinstance(raw, dict) else []
    return []


embedder = build_embedder()
destinations_mcp = mcp_toolset("mcp-destinations")

agent = Agent(
    build_model(),
    name="destination",
    output_type=DestinationResult,
    instructions=INSTRUCTIONS,
    model_settings={"temperature": 0},
)


@agent.tool_plain
async def shortlist_destinations() -> dict:
    """Shortlist destinations for the current trip request (filter, drop origin, re-rank, trim)."""
    request = DestinationRequest.model_validate(current_request())
    async with destinations_mcp:
        raw = await destinations_mcp.direct_call_tool("list_destinations", {
            "vibes": request.vibes,
            "budget_level": request.budget_level or "high",
            "activities": request.activities,
            "limit": request.limit * 8,  # a wider pool, so re-ranking has something to work with
        })
    pool = [d for d in parse_pool(raw) if request.origin not in d.get("airport_iata_codes", [])]
    ranked = await rank_by_similarity(pool, request, embedder)
    return {"destinations": [Destination.model_validate(d).model_dump() for d in ranked[: request.limit]]}


app = create_a2a_app(
    agent,
    name=AGENT_NAME,
    description="Shortlists travel destinations matching vibes, budget and activities, via the destinations MCP server.",
    prefix=PREFIX,
    public_url=PUBLIC_URL,
    skills=[Skill(id="shortlist-destinations", name="Shortlist destinations",
                  description="Return destinations that fit a trip request.", tags=["travel", "destinations", "mcp"],
                  input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4201")))
