"""flight-scout: quotes a route across several candidate dates and recommends one.

A2A in:  FlightQuoteRequest {"origin": "AMS", "to_iata": "VIE", "dates": ["2026-12-17", "2026-12-18"], "preferences": "..."}
A2A out: FlightOptions      {"options": [{date, flight_number, price, currency, priced_at}], "recommended_date", "reasoning"}

The `quote_dates` tool asks the travel-search MCP server (through agentgateway) for every date, concurrently. The
prices are facts and an output validator restores them, so the model's only contribution is the recommendation:
which date, and why (for example "the Saturday is 12 EUR cheaper and the traveller prefers weekends").
"""
import asyncio
import logging
import os

from travel_common.otel import setup_otel

setup_otel("flight-scout")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent, ModelRetry
from travel_common.a2a_server import create_a2a_app, current_request
from travel_common.contracts import FlightOption, FlightOptions, FlightQuoteRequest
from travel_common.mcp import mcp_toolset, unwrap
from travel_common.model import build_model

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "flight-scout")
PREFIX = "/a2a/flight-scout"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

INSTRUCTIONS = """\
You recommend a flight date. The request (origin, destination, candidate dates and the traveller's preferences) is
already available to your tool, so call quote_dates exactly once (it takes no arguments). Return the quotes it
gives you unchanged as options. Set recommended_date to the best date: normally the cheapest, unless the
traveller's preferences clearly favour another quoted date. Give one or two sentences of reasoning that mention
prices. Never invent dates, flight numbers or prices. If there are no quotes, return empty options."""

travel_search = mcp_toolset("mcp-travel-search", tool_error_behavior="error")

agent = Agent(build_model(), name="flight-scout", output_type=FlightOptions, instructions=INSTRUCTIONS,
              model_settings={"temperature": 0})


async def quote_one(date: str, request: FlightQuoteRequest) -> dict | None:
    try:
        raw = unwrap(await travel_search.direct_call_tool(
            "flight_price", {"from_iata": request.origin, "to_iata": request.to_iata, "date": date}))
    except Exception as exc:  # an unknown airport or a bad date is "no quote", not a failed task
        log.info("no quote for %s %s->%s: %s", date, request.origin, request.to_iata, exc)
        return None
    return FlightOption(date=date, flight_number=raw["flight_number"], price=raw["price"],
                        currency=raw.get("currency", "EUR"), priced_at=raw.get("priced_at", "")).model_dump()


@agent.tool_plain
async def quote_dates() -> dict:
    """Quote the current request's route for every candidate date."""
    request = FlightQuoteRequest.model_validate(current_request())
    async with travel_search:
        quotes = await asyncio.gather(*(quote_one(date, request) for date in request.dates))
    options = [q for q in quotes if q is not None]
    current_request()["_facts"] = options  # tools run in their own task: share through the request dict, not a contextvar
    return {"options": options, "preferences": request.preferences}


@agent.output_validator
def keep_the_tools_quotes(result: FlightOptions) -> FlightOptions:
    """Prices are facts from the tool; the model only recommends one of them."""
    facts = current_request().get("_facts")
    if facts is not None:
        result.options = [FlightOption.model_validate(q) for q in facts]
    if result.options and result.recommended_date not in {o.date for o in result.options}:
        raise ModelRetry(f"recommended_date must be one of {[o.date for o in result.options]}")
    if not result.options:
        result.recommended_date = None
    return result


app = create_a2a_app(
    agent, name=AGENT_NAME, prefix=PREFIX, public_url=PUBLIC_URL,
    description="Quotes a flight route across candidate dates via the travel-search MCP server and recommends one.",
    skills=[Skill(id="quote-flights", name="Quote flights", description="Quote a route for several dates and recommend one.",
                  tags=["travel", "flights", "mcp"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4302")))
