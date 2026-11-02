"""The coordinator's collaborators: the other agents (over A2A) and the two small LLM jobs it does itself.

`Specialists` and `Llm` are classes so tests can swap in stubs; the workflow only ever talks to these two.
Everything that decides anything about money or state is code elsewhere. Models are used for judgment here:
reading free text, and explaining a proposal to a human.
"""
import asyncio
import logging
import os
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic_ai import Agent
from travel_common.a2a_client import call_agent
from travel_common.contracts import (
    BookingCommand,
    BookingState,
    Destination,
    DestinationRequest,
    DestinationResult,
    FlightOptions,
    FlightQuoteRequest,
    TripRequirements,
    WatchRequest,
    WatchState,
)
from travel_common.model import build_model

from .settings import Agents

log = logging.getLogger(__name__)

DEFAULT_ORIGIN = os.getenv("DEFAULT_ORIGIN", "AMS")


class Specialists:
    """Typed A2A calls to the other agents, through agentgateway."""

    def __init__(self, agents: Agents | None = None):
        self.agents = agents or Agents()

    async def destinations(self, request: DestinationRequest) -> DestinationResult:
        return await call_agent(self.agents.destination_scout, request, DestinationResult, timeout=180)

    async def flights(self, request: FlightQuoteRequest) -> FlightOptions:
        return await call_agent(self.agents.flight_scout, request, FlightOptions, timeout=180)

    async def booking(self, command: BookingCommand) -> BookingState:
        return await call_agent(self.agents.booking_agent, command, BookingState, timeout=60)

    async def watch(self, request: WatchRequest) -> WatchState:
        return await call_agent(self.agents.fare_watcher, request, WatchState, timeout=60)


REQUIREMENTS_INSTRUCTIONS = """Extract a trip request from the traveller's message.
Today is {today}. Resolve relative dates ("next Friday") to YYYY-MM-DD.
- traveller: first name; 'Traveller' if none is given.
- origin: 3-letter IATA code; '{origin}' if the traveller does not say where from (Amsterdam = AMS).
- date: preferred travel date; if none, 30 days from today.
- flexible_days: days either side that are acceptable ("a day or two either way" = 2); 0 if not said.
- budget_level: 'low', 'mid' or 'high'; 'mid' if not said.
- vibes: only from culture, city, food, design, nature, relax, nightlife, beach. Empty if none are implied.
- activities: only from museums, architecture, shopping, music, walking. Empty if none are mentioned.
- max_price_eur: only if the traveller states a price ceiling for the flight.
- notes: anything else that should guide the choice. Do not invent preferences."""


class Llm:
    def __init__(self) -> None:
        self._requirements = Agent(build_model(), name="trip-requirements", output_type=TripRequirements,
                                   model_settings={"temperature": 0})
        self._summary = Agent(build_model(), name="proposal-summary", output_type=str, model_settings={"temperature": 0.2},
                              instructions="You brief a human who must approve or reject a flight booking. In at most 3 short "
                                           "sentences: what is proposed, why it fits the request, and anything they should weigh "
                                           "(price, date flexibility, alternatives). Use only the facts given; never invent any.")

    async def extract(self, text: str) -> TripRequirements:
        today = datetime.now(timezone.utc).date().isoformat()
        result = await self._requirements.run(text, instructions=REQUIREMENTS_INSTRUCTIONS.format(today=today, origin=DEFAULT_ORIGIN))
        return result.output

    async def summarize(self, facts: dict[str, Any]) -> str:
        try:
            return (await self._summary.run(str(facts))).output.strip() or fallback_summary(facts)
        except Exception as exc:  # a model hiccup must not block an approval
            log.warning("summary model failed (%s: %s); using the plain summary", type(exc).__name__, exc)
            return fallback_summary(facts)


def fallback_summary(facts: dict[str, Any]) -> str:
    flight = facts.get("flight", {})
    text = (f"{facts.get('origin')} to {facts.get('destination')} on {flight.get('date')}, flight "
            f"{flight.get('flight_number')} at {flight.get('price')} {flight.get('currency', 'EUR')}.")
    if facts.get("alternatives"):
        text += " Alternatives: " + ", ".join(f"{a['destination']} ({a['price']})" for a in facts["alternatives"]) + "."
    return text


# ---- research: pure code over the specialists --------------------------------------------------------------


def candidate_dates(date: str, flexible_days: int) -> list[str]:
    day = Date.fromisoformat(date)
    return [(day + timedelta(days=k)).isoformat() for k in range(-flexible_days, flexible_days + 1)]


async def research(specialists: Specialists, req: TripRequirements, overrides: dict[str, Any] | None = None,
                   max_quotes: int = 4) -> dict[str, Any] | None:
    """Shortlist destinations, quote flights for the best few, and propose the first that fits the constraints.

    `overrides` is what an approver changed when editing a proposal: {"destination": "Madrid"} and/or {"date": "..."}.
    Returns the proposal (with the other viable options as alternatives), or None if nothing fits.
    """
    overrides = overrides or {}
    dates = [overrides["date"]] if overrides.get("date") else candidate_dates(req.date, req.flexible_days)
    shortlist = await specialists.destinations(DestinationRequest(
        vibes=req.vibes, budget_level=req.budget_level, activities=req.activities, origin=req.origin, limit=5))
    candidates = list(shortlist.destinations)
    wanted = (overrides.get("destination") or "").lower()
    if wanted:
        candidates.sort(key=lambda d: d.name.lower() != wanted)  # stable: the approver's pick first, rest keep their rank

    async def quote(destination: Destination) -> dict[str, Any] | None:
        to_iata = destination.airport_iata_codes[0]
        options = await specialists.flights(FlightQuoteRequest(origin=req.origin, to_iata=to_iata, dates=dates,
                                                               preferences=req.notes))
        if not options.options:
            return None
        chosen = next((o for o in options.options if o.date == options.recommended_date), None) or \
            min(options.options, key=lambda o: o.price)
        if req.max_price_eur is not None and chosen.price > req.max_price_eur:
            return None
        return {"destination": destination.model_dump(), "to_iata": to_iata, "flight": chosen.model_dump(),
                "price": chosen.price, "currency": chosen.currency, "reasoning": options.reasoning}

    # The quotes are independent, so ask for them all at once; the shortlist's rank order is kept.
    quoted = await asyncio.gather(*(quote(d) for d in candidates[:max_quotes] if d.airport_iata_codes))
    viable = [q for q in quoted if q is not None]
    if not viable:
        return None
    best, rest = viable[0], viable[1:]
    return {
        **best,
        "origin": req.origin,
        "traveller": req.traveller,
        "destination_reasoning": shortlist.reasoning,
        "alternatives": [{"destination": v["destination"]["name"], "to_iata": v["to_iata"], "date": v["flight"]["date"],
                          "price": v["price"], "flight_number": v["flight"]["flight_number"]} for v in rest],
    }
