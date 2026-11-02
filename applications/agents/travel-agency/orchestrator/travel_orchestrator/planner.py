"""The two agents behind the orchestrator, and the specialist agents it calls over A2A.

`requirements_agent` turns free text into `TripRequirements` (one structured LLM call). `planner` is the
tool-calling agent: it shortlists destinations, picks one, finds a flight and books it, retrying with a
growing exclusion list when no flight is found. Each tool is one A2A call to a specialist agent through
agentgateway. `book_flight` requires human approval: the run ends with `DeferredToolRequests` and resumes
once someone approves or rejects it (see service.py).
"""
import os
from dataclasses import dataclass

import httpx
from pydantic_ai import Agent, DeferredToolRequests, RunContext
from travel_common.a2a_client import A2AError, call_agent
from travel_common.contracts import (
    BookingRequest,
    BookingResult,
    DecisionRequest,
    DecisionResult,
    Destination,
    DestinationRequest,
    DestinationResult,
    FlightRequest,
    FlightResult,
)
from travel_common.mcp import agentgateway_url
from travel_common.model import build_model

from .models import TripRequirements


@dataclass
class Specialists:
    """Where the specialist agents live. Every call goes through agentgateway, so every hop is observable there."""
    destination: str
    destination_decision: str
    flight_finder: str
    flight_booker: str

    @classmethod
    def from_env(cls) -> "Specialists":
        base = agentgateway_url()
        return cls(
            destination=os.getenv("SPECIALIST_DESTINATION", f"{base}/a2a/destination"),
            destination_decision=os.getenv("SPECIALIST_DESTINATION_DECISION", f"{base}/a2a/destination-decision"),
            flight_finder=os.getenv("SPECIALIST_FLIGHT_FINDER", f"{base}/a2a/flight-finder"),
            flight_booker=os.getenv("SPECIALIST_FLIGHT_BOOKER", f"{base}/a2a/flight-booker"),
        )

    async def shortlist(self, request: DestinationRequest) -> DestinationResult:
        return await call_agent(self.destination, request, DestinationResult)

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        return await call_agent(self.destination_decision, request, DecisionResult)

    async def find_flight(self, request: FlightRequest) -> FlightResult:
        return await call_agent(self.flight_finder, request, FlightResult)

    async def book(self, request: BookingRequest) -> BookingResult:
        return await call_agent(self.flight_booker, request, BookingResult, timeout=90)


SYSTEM_PROMPT = """You are a travel booking orchestrator. Plan and book one trip per conversation by \
calling tools in this order:
1. get_destination_shortlist — using the traveller's vibes/budget/activities/origin.
2. pick_destination — pick one candidate from the shortlist you haven't tried yet.
3. find_flight — look for a flight to the picked destination.
   - If find_flight returns {"flight": null}, call pick_destination again, passing every IATA code \
you've tried so far in tried_iata_codes, then call find_flight again for the new pick.
   - If every destination in the shortlist has been tried with no flight found, stop and tell the \
user no flights were available, listing the shortlist you considered.
4. book_flight — as soon as find_flight succeeds, call this immediately. It pauses for human \
approval before the booking actually happens; you won't see the outcome until a human decides.
Once book_flight returns, reply based on its outcome: if booked, give a human-readable summary \
(the shortlist you considered, the destination you picked, the flight details — flight number, \
route, date, price — and the booking confirmation code/status); if the human reviewer rejected \
it, say plainly that the booking was not made and no confirmation exists. Never call book_flight \
before find_flight has succeeded for that destination. Do not ask the user clarifying questions \
— infer sensible defaults and proceed autonomously (the human-approval step for booking is the \
one deliberate exception to that)."""

requirements_agent = Agent(
    build_model(),
    name="trip-requirements",
    output_type=TripRequirements,
    instructions="Extract structured trip requirements from the traveller's request below.",
    model_settings={"temperature": 0},
)

planner = Agent(
    build_model(),
    name="travel-orchestrator",
    deps_type=Specialists,
    output_type=[str, DeferredToolRequests],
    instructions=SYSTEM_PROMPT,
    model_settings={"temperature": 0},
)


@planner.tool
async def get_destination_shortlist(
    ctx: RunContext[Specialists], vibes: list[str], budget_level: str, activities: list[str], origin: str, limit: int = 5,
) -> dict:
    """Look up a shortlist of candidate destinations from the destinations specialist.

    Args:
        vibes: desired travel vibes, e.g. ["culture", "city"].
        budget_level: one of "low", "mid", "high".
        activities: desired activities, e.g. ["museums"].
        origin: traveller's origin airport IATA code (used to exclude destinations reachable without flying).
        limit: maximum number of candidates to return.

    Returns:
        {"destinations": [{"name", "country", "airport_iata_codes", "blurb", ...}, ...]}
    """
    request = DestinationRequest(vibes=vibes, budget_level=budget_level, activities=activities, origin=origin, limit=limit)
    return (await ctx.deps.shortlist(request)).model_dump()


@planner.tool
async def pick_destination(
    ctx: RunContext[Specialists], shortlist: list[Destination], tried_iata_codes: list[str] | None = None,
) -> dict:
    """Pick one destination from the shortlist, excluding any already-tried IATA codes.

    Call this again with tried_iata_codes extended by the last attempted IATA code whenever
    find_flight returns no flight, to get the next-best untried destination.

    Returns:
        {"selected_destination": {...}}
    """
    return (await ctx.deps.decide(DecisionRequest(shortlist=shortlist, tried_iata_codes=tried_iata_codes or []))).model_dump()


@planner.tool
async def find_flight(ctx: RunContext[Specialists], origin: str, to_iata: str, date: str) -> dict:
    """Look up a flight from origin to a destination airport on a given date.

    Returns:
        {"flight": {...}} on success, or {"flight": null} if none is available — in that case call
        pick_destination again with to_iata added to tried_iata_codes and retry.
    """
    try:
        return (await ctx.deps.find_flight(FlightRequest(origin=origin, to_iata=to_iata, date=date))).model_dump()
    except (A2AError, httpx.HTTPError):
        # Same {"flight": null} shape so the retry logic is unaffected; the extra key lets traces and
        # evaluators tell "the flight-finder is down" apart from "it answered and found no route".
        return {"flight": None, "error": "backend_unavailable"}


@planner.tool(requires_approval=True)
async def book_flight(ctx: RunContext[Specialists], passenger_name: str, from_iata: str, to_iata: str, date: str) -> dict:
    """Book the previously-found flight for the passenger. Only call this after find_flight succeeded.

    This pauses the run for human approval before actually booking — a human must approve or
    reject it before the booking happens (or is skipped). Call it once you're ready to book; you'll
    see the outcome (booked, or rejected by the reviewer) once a decision is made.

    Args:
        passenger_name: name to book the flight under.
        from_iata: origin airport IATA code (the flight's "from").
        to_iata: destination airport IATA code (the flight's "to").
        date: travel date as YYYY-MM-DD.

    Returns:
        {"booking": {...}, "approval": "approved"}
    """
    booking = await ctx.deps.book(BookingRequest(passenger_name=passenger_name, from_iata=from_iata, to_iata=to_iata, date=date))
    return {**booking.model_dump(), "approval": "approved"}
