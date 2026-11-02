"""Destination-decision agent — picks one destination from a shortlist by rolling a D20.

A2A in:  DecisionRequest {"shortlist": [...], "tried_iata_codes": [...]}
A2A out: DecisionResult  {"selected_destination": {...}}

The agent rolls through the dice-roller MCP server (via agentgateway) and passes the roll to
`select_candidate`, which does the index arithmetic in code — an LLM shouldn't do modular arithmetic or
copy a shortlist back out of its context. The shortlist comes from the request itself.

DICE_ROLL_DETERMINISTIC=true always picks the first untried candidate (reproducible evals).
"""
import os

from travel_common.otel import setup_otel

setup_otel("travel-destination-decision")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent
from travel_common.a2a_server import create_a2a_app, current_request
from travel_common.contracts import DecisionRequest, DecisionResult, Destination
from travel_common.mcp import mcp_toolset
from travel_common.model import build_model

AGENT_NAME = os.getenv("AGENT_NAME", "travel-destination-decision")
PREFIX = "/a2a/destination-decision"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")
DETERMINISTIC = os.getenv("DICE_ROLL_DETERMINISTIC", "false").lower() in ("1", "true", "yes")

INSTRUCTIONS = """\
You pick one destination from a shortlist. The shortlist and the already-tried airports are in the request;
you do not need to read them. Call roll-20 exactly once, then call select_candidate with the number rolled
(the tool result contains it as an integer). Return the destination select_candidate gives you, unchanged,
as selected_destination. If it returns null, return selected_destination=null."""


def choose(shortlist: list[Destination], tried_iata_codes: list[str], roll: int, deterministic: bool = False) -> Destination | None:
    """Pick by D20 roll among destinations not yet tried (all of them if every one has been tried)."""
    tried = set(tried_iata_codes)
    candidates = [d for d in shortlist if (d.airport_iata_codes[:1] or [""])[0] not in tried] or shortlist
    if not candidates:
        return None
    return candidates[0] if deterministic else candidates[(roll - 1) % len(candidates)]


agent = Agent(
    build_model(),
    name="destination-decision",
    output_type=DecisionResult,
    instructions=INSTRUCTIONS,
    toolsets=[mcp_toolset("mcp-dice-roller", tool_error_behavior="retry").filtered(lambda ctx, tool: tool.name == "roll-20")],
    model_settings={"temperature": 0},
)


@agent.tool_plain
def select_candidate(roll: int) -> dict | None:
    """Pick the destination for this D20 roll from the request's shortlist, skipping tried airports."""
    request = DecisionRequest.model_validate(current_request())
    picked = choose(request.shortlist, request.tried_iata_codes, roll, DETERMINISTIC)
    return picked.model_dump() if picked else None


app = create_a2a_app(
    agent,
    name=AGENT_NAME,
    description="Picks one destination from a shortlist by rolling a D20 via the dice-roller MCP server.",
    prefix=PREFIX,
    public_url=PUBLIC_URL,
    skills=[Skill(id="pick-destination", name="Pick destination", description="Choose one destination from a shortlist.",
                  tags=["travel", "decision", "mcp"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4202")))
