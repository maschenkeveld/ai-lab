"""Flight-booker agent — books a confirmed flight via the book-flights MCP server.

A2A in:  BookingRequest {"passenger_name": "Maarten", "from_iata": "AMS", "to_iata": "BCN", "date": "2026-07-15"}
A2A out: BookingResult  {"booking": {...}}

This agent has no idea whether a human approved the booking: by the time a request reaches it, that decision
has been made. The approval gate lives in the orchestrator; this agent only ever sees "book it now".
"""
import os

from travel_common.otel import setup_otel

setup_otel("travel-flight-booker")

import uvicorn
from fasta2a.schema import Skill
from pydantic_ai import Agent
from travel_common.a2a_server import create_a2a_app
from travel_common.contracts import BookingResult
from travel_common.mcp import mcp_toolset
from travel_common.model import build_model

AGENT_NAME = os.getenv("AGENT_NAME", "travel-flight-booker")
PREFIX = "/a2a/flight-booker"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

INSTRUCTIONS = """\
You book flights. The user message is JSON: {"passenger_name", "from_iata", "to_iata", "date"}. Call the
create_booking tool exactly once with those four values and return the booking it creates, unchanged, as
booking. If the tool fails, let the error surface; never invent a booking."""

# Booking is a side effect: an error must fail the task, not be retried or papered over by the model.
agent = Agent(
    build_model(),
    name="flight-booker",
    output_type=BookingResult,
    instructions=INSTRUCTIONS,
    toolsets=[mcp_toolset("mcp-book-flights", tool_error_behavior="error").filtered(lambda ctx, tool: tool.name == "create_booking")],
    model_settings={"temperature": 0},
)

app = create_a2a_app(
    agent,
    name=AGENT_NAME,
    description="Books a flight for a passenger via the book-flights MCP server.",
    prefix=PREFIX,
    public_url=PUBLIC_URL,
    skills=[Skill(id="book-flight", name="Book flight", description="Create a flight booking.",
                  tags=["travel", "booking", "mcp"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4204")))
