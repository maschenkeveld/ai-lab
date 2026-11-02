"""Flight-finder agent — quotes a flight between two airports via the flights MCP server.

A2A in:  FlightRequest {"origin": "AMS", "to_iata": "BCN", "date": "2026-07-15"}
A2A out: FlightResult  {"flight": {...}} on success, {"flight": null} if no flight is available.

`{"flight": null}` is the signal the orchestrator's retry loop uses to try a different destination.
The agent reaches the `flight_price` tool through agentgateway (`/mcp-flights`).
"""
import os

from travel_common.otel import setup_otel

setup_otel("travel-flight-finder")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent
from travel_common.a2a_server import create_a2a_app
from travel_common.contracts import FlightResult
from travel_common.mcp import mcp_toolset
from travel_common.model import build_model

AGENT_NAME = os.getenv("AGENT_NAME", "travel-flight-finder")
PREFIX = "/a2a/flight-finder"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

INSTRUCTIONS = """\
You find flights. The user message is JSON: {"origin": <IATA>, "to_iata": <IATA>, "date": <YYYY-MM-DD>}.
Call the flight_price tool exactly once with from_iata=origin, to_iata=to_iata and the date, and return the
quote it gives you as the flight. If the tool reports an error or gives no price, return flight=null.
Never invent flight numbers or prices."""

# "retry" lets the model see a tool error (e.g. an unknown airport code) and answer flight=null,
# rather than failing the whole task.
agent = Agent(
    build_model(),
    name="flight-finder",
    output_type=FlightResult,
    instructions=INSTRUCTIONS,
    toolsets=[mcp_toolset("mcp-flights", tool_error_behavior="retry")],
    model_settings={"temperature": 0},
)

app = create_a2a_app(
    agent,
    name=AGENT_NAME,
    description="Quotes a flight between two airports on a given date, via the flights MCP server.",
    prefix=PREFIX,
    public_url=PUBLIC_URL,
    skills=[Skill(id="find-flight", name="Find flight", description="Quote a flight for origin, destination and date.",
                  tags=["travel", "flights", "mcp"], input_modes=["application/json"],
                  output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4203")))
