"""destination-scout: shortlists destinations that fit a trip request and says why they rank as they do.

A2A in:  DestinationRequest {"vibes": [...], "budget_level": "mid", "activities": [...], "limit": 5, "origin": "AMS"}
A2A out: DestinationResult  {"destinations": [...], "reasoning": "..."}

The `shortlist_destinations` tool does the exact work (MCP `list_destinations` through agentgateway, drop anything
served by the origin airport, re-rank by embedding similarity, with a keyword-order fallback). The model's only
job is the short reasoning; an output validator puts the tool's destinations back, so a model can never alter them.
"""
import logging
import os

from travel_common.otel import setup_otel

setup_otel("destination-scout")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent
from travel_common.a2a_server import create_a2a_app, current_request
from travel_common.contracts import Destination, DestinationRequest, DestinationResult
from travel_common.mcp import mcp_toolset, unwrap
from travel_common.model import build_embedder, build_model

from .ranking import parse_pool, rank_by_similarity

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "destination-scout")
PREFIX = "/a2a/destination-scout"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

INSTRUCTIONS = """\
You shortlist destinations for a trip. The trip request is already available to your tool, so call
shortlist_destinations exactly once (it takes no arguments). Return its destinations unchanged and add one or two
sentences of reasoning: why the top destinations fit the request's vibes, budget and activities. Never invent,
reorder or alter destinations."""

embedder = build_embedder()
travel_search = mcp_toolset("mcp-travel-search")

agent = Agent(build_model(), name="destination-scout", output_type=DestinationResult, instructions=INSTRUCTIONS,
              model_settings={"temperature": 0})


@agent.tool_plain
async def shortlist_destinations() -> dict:
    """Shortlist destinations for the current trip request (filter, drop origin, re-rank, trim)."""
    request = DestinationRequest.model_validate(current_request())
    async with travel_search:
        raw = await travel_search.direct_call_tool("list_destinations", {
            "vibes": request.vibes,
            "budget_level": request.budget_level or "high",
            "activities": request.activities,
            "limit": request.limit * 8,  # a wider pool, so re-ranking has something to work with
        })
    pool = [d for d in parse_pool(unwrap(raw)) if request.origin not in d.get("airport_iata_codes", [])]
    ranked = await rank_by_similarity(pool, request, embedder)
    shortlist = [Destination.model_validate(d).model_dump() for d in ranked[: request.limit]]
    current_request()["_facts"] = shortlist  # tools run in their own task: share through the request dict
    return {"destinations": shortlist}


@agent.output_validator
def keep_the_tools_destinations(result: DestinationResult) -> DestinationResult:
    """The destinations are facts from the tool; the model only contributes the reasoning."""
    facts = current_request().get("_facts")
    if facts is not None:
        result.destinations = [Destination.model_validate(d) for d in facts]
    return result


app = create_a2a_app(
    agent, name=AGENT_NAME, prefix=PREFIX, public_url=PUBLIC_URL,
    description="Shortlists and ranks travel destinations for vibes, budget and activities, via the travel-search MCP server.",
    skills=[Skill(id="shortlist-destinations", name="Shortlist destinations",
                  description="Return ranked destinations that fit a trip request, with reasoning.",
                  tags=["travel", "destinations", "mcp"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4301")))
