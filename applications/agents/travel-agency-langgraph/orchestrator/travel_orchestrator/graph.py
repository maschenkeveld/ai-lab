"""
LangGraph orchestrator — calls specialist agents via A2A rather than REST APIs directly.

Flow:
  parse_requirements
    → call_destination         (destination specialist: get shortlist)
      → call_destination_decision  (pick one from shortlist, skip already tried)
        → call_flight_finder       (look for a flight; None on failure)
          → [route_after_flight]
              "call_destination_decision"  → retry with next destination
              "call_flight_booker"
                → call_flight_booker
                  → summarize → END

Each call_* node sends an A2A JSON-RPC request to a specialist service via agentgateway.
The specialist does the actual work (REST call or computation) and returns a JSON payload
in the A2A response text. LangSmith traces the full graph automatically when
LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY are set.
"""

import json
import os
import re
import uuid
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph


# Specialist A2A base URLs — routed through agentgateway so traffic is observable/proxied.
AGENTGATEWAY_URL = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
SPECIALIST_DESTINATION          = os.getenv("SPECIALIST_DESTINATION",          f"{AGENTGATEWAY_URL}/a2a/destination")
SPECIALIST_DESTINATION_DECISION = os.getenv("SPECIALIST_DESTINATION_DECISION", f"{AGENTGATEWAY_URL}/a2a/destination-decision")
SPECIALIST_FLIGHT_FINDER        = os.getenv("SPECIALIST_FLIGHT_FINDER",        f"{AGENTGATEWAY_URL}/a2a/flight-finder")
SPECIALIST_FLIGHT_BOOKER        = os.getenv("SPECIALIST_FLIGHT_BOOKER",        f"{AGENTGATEWAY_URL}/a2a/flight-booker")

DEFAULT_PASSENGER = os.getenv("DEFAULT_PASSENGER", "Maarten")
DEFAULT_ORIGIN    = os.getenv("DEFAULT_ORIGIN", "AMS")
DEFAULT_DATE      = os.getenv("DEFAULT_TRAVEL_DATE", "2026-07-15")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class TravelState(TypedDict, total=False):
    prompt: str
    requirements: dict[str, Any]
    shortlist: list[dict[str, Any]]
    tried_iata_codes: list[str]            # IATA codes already attempted — used by retry loop
    selected_destination: dict[str, Any]
    flight: dict[str, Any] | None          # None means no flight found
    booking: dict[str, Any]
    answer: str
    errors: list[str]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def parse_requirements(state: TravelState) -> TravelState:
    """Node 1 — extract structured requirements from the free-text prompt."""
    prompt = state.get("prompt", "").strip()
    requirements = {
        "passenger_name": extract_name(prompt) or DEFAULT_PASSENGER,
        "origin":         extract_iata(prompt, default=DEFAULT_ORIGIN),
        "date":           extract_date(prompt) or DEFAULT_DATE,
        "region":         "Europe",
        "budget_level":   extract_budget(prompt) or "high",
        "vibes":          extract_terms(prompt, ["culture", "city", "food", "design", "nature", "relax", "nightlife"]),
        "activities":     extract_terms(prompt, ["museums", "architecture", "food", "shopping", "music", "walking"]),
    }
    if not requirements["vibes"]:
        requirements["vibes"] = ["culture", "city"]
    if not requirements["activities"]:
        requirements["activities"] = ["museums"]
    return {**state, "requirements": requirements, "errors": []}


def call_destination(state: TravelState) -> TravelState:
    """
    Node 2 — call the destination specialist to get a shortlist.

    Sends vibes/budget/activities to the destination specialist via A2A.
    The specialist queries the REST destinations API and returns up to 5 candidates.
    """
    req = state["requirements"]
    result = call_specialist(SPECIALIST_DESTINATION, {
        "vibes":        req["vibes"],
        "budget_level": req["budget_level"],
        "activities":   req["activities"],
        "limit":        5,
        "origin":       req["origin"],
    })
    return {**state, "shortlist": result.get("destinations", [])}


def call_destination_decision(state: TravelState) -> TravelState:
    """
    Node 3 — call the destination-decision specialist to pick one destination.

    Passes the full shortlist and the list of already-tried IATA codes so the
    specialist can exclude previous picks. Called once on the first pass and
    again on each retry when flight-finder returns nothing.
    """
    result = call_specialist(SPECIALIST_DESTINATION_DECISION, {
        "shortlist":          state["shortlist"],
        "tried_iata_codes":   state.get("tried_iata_codes") or [],
        "seed":               state.get("prompt", ""),
    })
    return {**state, "selected_destination": result["selected_destination"]}


def call_flight_finder(state: TravelState) -> TravelState:
    """
    Node 4 — call the flight-finder specialist.

    On success, `flight` is populated. On failure (no availability, HTTP error),
    `flight` is set to None so the router sends us back to call_destination_decision.
    `tried_iata_codes` is updated here so the retry always excludes this pick.
    """
    req         = state["requirements"]
    destination = state["selected_destination"]
    to_iata     = destination["airport_iata_codes"][0]
    tried       = list(state.get("tried_iata_codes") or []) + [to_iata]
    try:
        result = call_specialist(SPECIALIST_FLIGHT_FINDER, {
            "origin":   req["origin"],
            "to_iata":  to_iata,
            "date":     req["date"],
        })
        return {**state, "flight": result.get("flight"), "tried_iata_codes": tried}
    except Exception:
        return {**state, "flight": None, "tried_iata_codes": tried}


def call_flight_booker(state: TravelState) -> TravelState:
    """Node 5 — call the flight-booker specialist to confirm the booking."""
    req    = state["requirements"]
    flight = state["flight"]
    result = call_specialist(SPECIALIST_FLIGHT_BOOKER, {
        "passenger_name": req["passenger_name"],
        "from":           flight["from"],
        "to":             flight["to"],
        "date":           flight["date"],
    })
    return {**state, "booking": result["booking"]}


def summarize(state: TravelState) -> TravelState:
    """Node 6 — build the final human-readable answer. Always the last node before END."""
    destination  = state.get("selected_destination") or {}
    flight       = state.get("flight")
    booking      = state.get("booking")
    shortlist_lines = [
        f"{idx}. {item['name']}, {item['country']} ({item['airport_iata_codes'][0]}) - {item['blurb']}"
        for idx, item in enumerate(state.get("shortlist") or [], start=1)
    ]
    if flight is None:
        answer = "\n".join([
            "Travel workflow completed — no available flights found for any shortlisted destination.",
            "", "Shortlist:", *shortlist_lines,
        ])
    else:
        answer = "\n".join([
            "Travel workflow completed.", "", "Shortlist:", *shortlist_lines, "",
            f"Selected destination: {destination.get('name')}, {destination.get('country')} ({destination.get('airport_iata_codes', ['?'])[0]})",
            f"Flight: {flight['flight_number']} {flight['from']} -> {flight['to']} on {flight['date']} for {flight['currency']} {flight['price']:.2f}",
            f"Booking: {booking['booking_code']} for {booking['name']} ({booking['status']})",
        ])
    return {**state, "answer": answer}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def route_after_flight(state: TravelState) -> str:
    """
    If flight-finder returned nothing and the shortlist isn't exhausted, loop
    back to call_destination_decision for the next pick. Otherwise proceed to booking.
    """
    if state.get("flight") is None:
        tried    = state.get("tried_iata_codes") or []
        shortlist = state.get("shortlist") or []
        if len(tried) < len(shortlist):
            return "call_destination_decision"
    return "call_flight_booker"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph():
    """
    Compile the orchestrator graph.

    parse_requirements → call_destination → call_destination_decision ←──────────┐
                                                  ↓                               │
                                           call_flight_finder                     │
                                                  ↓                               │
                                     [route_after_flight]                         │
                                       "call_destination_decision" ───────────────┘
                                       "call_flight_booker"
                                                  ↓
                                          call_flight_booker → summarize → END
    """
    graph = StateGraph(TravelState)

    graph.add_node("parse_requirements",        parse_requirements)
    graph.add_node("call_destination",          call_destination)
    graph.add_node("call_destination_decision", call_destination_decision)
    graph.add_node("call_flight_finder",        call_flight_finder)
    graph.add_node("call_flight_booker",        call_flight_booker)
    graph.add_node("summarize",                 summarize)

    graph.set_entry_point("parse_requirements")
    graph.add_edge("parse_requirements",        "call_destination")
    graph.add_edge("call_destination",          "call_destination_decision")
    graph.add_edge("call_destination_decision", "call_flight_finder")
    graph.add_conditional_edges(
        "call_flight_finder",
        route_after_flight,
        {"call_flight_booker": "call_flight_booker", "call_destination_decision": "call_destination_decision"},
    )
    graph.add_edge("call_flight_booker", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# A2A helper
# ---------------------------------------------------------------------------

def call_specialist(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send an A2A JSON-RPC request to a specialist agent and return the parsed response.

    The payload is serialized as JSON and placed in the A2A message text field.
    The specialist returns its result the same way — JSON in the response text.
    Routing through agentgateway means all inter-agent traffic is observable.
    """
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tasks/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(payload)}],
            }
        },
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(f"{base_url}/a2a/jsonrpc", json=body)
        response.raise_for_status()
    result = response.json()
    text = result["result"]["parts"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Prompt parsing helpers
# ---------------------------------------------------------------------------

def extract_name(prompt: str) -> str | None:
    match = re.search(r"\bfor\s+([A-Z][a-zA-Z-]{1,40})\b", prompt)
    return match.group(1) if match else None

def extract_iata(prompt: str, default: str) -> str:
    match = re.search(r"\bfrom\s+([A-Z]{3})\b", prompt)
    return match.group(1) if match else default

def extract_date(prompt: str) -> str | None:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", prompt)
    return match.group(1) if match else None

def extract_budget(prompt: str) -> str | None:
    lowered = prompt.lower()
    for budget in ("low", "mid", "high"):
        if re.search(rf"\b{budget}\b", lowered):
            return budget
    return None

def extract_terms(prompt: str, allowed: list[str]) -> list[str]:
    lowered = prompt.lower()
    return [term for term in allowed if re.search(rf"\b{re.escape(term)}\b", lowered)]
