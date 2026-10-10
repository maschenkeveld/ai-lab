"""
LangGraph orchestrator — a tool-calling agent that plans and books a trip by
calling specialist agents via A2A, exposed to the LLM as LangChain tools.

Flow (enforced by SYSTEM_PROMPT, not by graph topology):
  extract_requirements (structured LLM call)
    → agent loop, calling tools as needed:
        get_destination_shortlist  (destination specialist)
        pick_destination           (destination-decision specialist; retried with
                                     growing tried_iata_codes if find_flight fails)
        find_flight                (flight-finder specialist; None on failure)
        book_flight                (flight-booker specialist)
    → final answer message

Each tool sends an A2A JSON-RPC request to a specialist service via agentgateway.
The specialist does the actual work (REST call, LLM call, or computation) and
returns a JSON payload in the A2A response text. LangSmith traces the full agent
run (and, via header propagation in call_specialist, the specialist calls it makes)
when LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY are set.

Conversation state (including which destinations have been tried) is persisted per
`thread_id` in Postgres via AsyncPostgresSaver, not in pod-local memory — so the
orchestrator's compute is horizontally scalable (any replica can serve any request) while
conversation state lives externally in Postgres (IMPROVEMENTS.md §17).

Unresolved issue, stated plainly rather than hidden: there is no session affinity /
distributed locking around a given `thread_id`. Two concurrent requests for the same
`thread_id` hitting two different replicas can both read the same checkpoint and then race
to write the next one — the checkpointer makes state external, it does not by itself make
concurrent access to one thread_id safe. This is why the production-style GitOps deployment
currently keeps this service at a single replica rather than scaling it blindly; scaling it
for real would need either sticky routing on thread_id at the gateway or per-thread locking
at the application level, neither of which exists today.
"""

import json
import logging
import os
import uuid
from typing import Any, AsyncIterator

import httpx
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Specialist A2A base URLs — routed through agentgateway so traffic is observable/proxied.
AGENTGATEWAY_URL = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
SPECIALIST_DESTINATION          = os.getenv("SPECIALIST_DESTINATION",          f"{AGENTGATEWAY_URL}/a2a/destination")
SPECIALIST_DESTINATION_DECISION = os.getenv("SPECIALIST_DESTINATION_DECISION", f"{AGENTGATEWAY_URL}/a2a/destination-decision")
SPECIALIST_FLIGHT_FINDER        = os.getenv("SPECIALIST_FLIGHT_FINDER",        f"{AGENTGATEWAY_URL}/a2a/flight-finder")
SPECIALIST_FLIGHT_BOOKER        = os.getenv("SPECIALIST_FLIGHT_BOOKER",        f"{AGENTGATEWAY_URL}/a2a/flight-booker")

DEFAULT_PASSENGER = os.getenv("DEFAULT_PASSENGER", "Maarten")
DEFAULT_ORIGIN    = os.getenv("DEFAULT_ORIGIN", "AMS")
DEFAULT_DATE      = os.getenv("DEFAULT_TRAVEL_DATE", "2026-07-15")

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm.litellm.svc.cluster.local:4000/v1")
LITELLM_API_KEY  = os.getenv("LITELLM_API_KEY", "sk-ai-lab-litellm")
LITELLM_MODEL    = os.getenv("LITELLM_MODEL", "openai-gpt")

CHECKPOINT_DB_URL = os.getenv(
    "CHECKPOINT_DB_URL",
    "postgresql://postgres:postgres@postgres.llm-analytics.svc.cluster.local:5432/llm_analytics?sslmode=disable",
)

# Trace/run metadata (LangSmith §5): lets a trace or an eval experiment be tied
# back to a specific build and deployment target without grepping logs.
APP_VERSION = os.getenv("APP_VERSION", "dev")
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
SERVICE_NAME = "travel-orchestrator"
RUN_METADATA = {
    "service_name": SERVICE_NAME,
    "agent_name": SERVICE_NAME,
    "model_alias": LITELLM_MODEL,
    "app_version": APP_VERSION,
    "environment": ENVIRONMENT,
}


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def build_llm() -> ChatOpenAI:
    """A LangChain chat model pointed at the in-cluster LiteLLM proxy."""
    return ChatOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_MODEL, temperature=0)


# ---------------------------------------------------------------------------
# Requirement extraction — structured LLM call, replaces regex parsing
# ---------------------------------------------------------------------------

class TripRequirements(BaseModel):
    passenger_name: str = Field(description=f"Passenger's first name. Default '{DEFAULT_PASSENGER}' if not mentioned.")
    origin: str = Field(description=f"3-letter IATA origin airport code. Default '{DEFAULT_ORIGIN}' if not mentioned.")
    date: str = Field(description=f"Travel date as YYYY-MM-DD. Default '{DEFAULT_DATE}' if not mentioned.")
    region: str = Field(default="Europe", description="Region to travel within.")
    budget_level: str = Field(description="One of 'low', 'mid', 'high'. Default 'high' if not mentioned.")
    vibes: list[str] = Field(
        description="Desired trip vibes, chosen from: culture, city, food, design, nature, relax, "
                     "nightlife, beach. Default ['culture', 'city'] if none are mentioned."
    )
    activities: list[str] = Field(
        # "food" deliberately excluded here (confirmed live): the destinations backend's
        # activities field never contains that literal string (foodie interest is
        # modeled as the "food" *vibe*, not an activity - see rest-servers/destinations'
        # actual data), and get_destination_shortlist's activities filter is an exact-match
        # AND condition. Extracting "food" into activities here silently zeroes out every
        # candidate regardless of anything else the traveller asked for - a real request
        # like "somewhere with great food" should land on the vibes field instead.
        description="Desired activities, chosen from: museums, architecture, shopping, music, "
                     "walking. Default ['museums'] if none are mentioned."
    )


# LCEL chain: ChatPromptTemplate | llm.with_structured_output
# The | operator wires prompt → model as a Runnable sequence.
# ChatPromptTemplate formats the messages; with_structured_output parses the response into TripRequirements.
REQUIREMENTS_CHAIN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Extract structured trip requirements from the traveller's request below."),
    ("human", "{input}"),
])


@traceable(name="extract_trip_requirements", run_type="chain")
def extract_requirements(prompt: str, llm: ChatOpenAI) -> dict[str, Any]:
    """Extract structured trip requirements via an LCEL chain: prompt | llm with structured output."""
    chain = REQUIREMENTS_CHAIN_PROMPT | llm.with_structured_output(TripRequirements)
    requirements: TripRequirements = chain.invoke({"input": prompt.strip()})
    return requirements.model_dump()


# ---------------------------------------------------------------------------
# Tools — each wraps an A2A call to a specialist agent
# ---------------------------------------------------------------------------

@tool
def get_destination_shortlist(vibes: list[str], budget_level: str, activities: list[str], origin: str, limit: int = 5) -> str:
    """Look up a shortlist of candidate destinations from the destinations specialist.

    Args:
        vibes: desired travel vibes, e.g. ["culture", "city"].
        budget_level: one of "low", "mid", "high".
        activities: desired activities, e.g. ["museums"].
        origin: traveller's origin airport IATA code (used to exclude destinations reachable without flying).
        limit: maximum number of candidates to return.

    Returns:
        JSON string: {"destinations": [{"name", "country", "airport_iata_codes", "blurb", ...}, ...]}
    """
    result = call_specialist(SPECIALIST_DESTINATION, {
        "vibes": vibes, "budget_level": budget_level, "activities": activities, "limit": limit, "origin": origin,
    })
    return json.dumps(result)


@tool
def pick_destination(shortlist: list[dict[str, Any]], tried_iata_codes: list[str] | None = None) -> str:
    """Pick one destination from the shortlist, excluding any already-tried IATA codes.

    Call this again with tried_iata_codes extended by the last attempted IATA code whenever
    find_flight returns no flight, to get the next-best untried destination.

    Returns:
        JSON string: {"selected_destination": {...}}
    """
    result = call_specialist(SPECIALIST_DESTINATION_DECISION, {
        "shortlist": shortlist, "tried_iata_codes": tried_iata_codes or [],
    })
    return json.dumps(result)


@tool
def find_flight(origin: str, to_iata: str, date: str) -> str:
    """Look up a flight from origin to a destination airport on a given date.

    Returns:
        JSON string {"flight": {...}} on success, or {"flight": null} if none is available — in that
        case call pick_destination again with to_iata added to tried_iata_codes and retry.
    """
    try:
        result = call_specialist(SPECIALIST_FLIGHT_FINDER, {"origin": origin, "to_iata": to_iata, "date": date})
    except httpx.HTTPError:
        # Distinguish "the flight-finder backend itself is unreachable" from "it answered and
        # found no route" (IMPROVEMENTS.md §15) — same {"flight": null} shape so the agent's
        # existing retry logic is unaffected, but the extra "error" key lets tool_calls/traces
        # (and future evaluators) tell the two apart instead of treating every failure alike.
        result = {"flight": None, "error": "backend_unavailable"}
    return json.dumps(result)


@tool
def book_flight(passenger_name: str, from_: str, to: str, date: str) -> str:
    """Book the previously-found flight for the passenger. Only call this after find_flight succeeded.

    Args:
        passenger_name: name to book the flight under.
        from_: origin airport IATA code (the flight's "from").
        to: destination airport IATA code (the flight's "to").
        date: travel date as YYYY-MM-DD.

    Returns:
        JSON string {"booking": {...}}.
    """
    result = call_specialist(SPECIALIST_FLIGHT_BOOKER, {
        "passenger_name": passenger_name, "from": from_, "to": to, "date": date,
    })
    return json.dumps(result)


TOOLS = [get_destination_shortlist, pick_destination, find_flight, book_flight]

SYSTEM_PROMPT = """You are a travel booking orchestrator. Plan and book one trip per conversation by \
calling tools in this order:

1. get_destination_shortlist — using the traveller's vibes/budget/activities/origin.
2. pick_destination — pick one candidate from the shortlist you haven't tried yet.
3. find_flight — look for a flight to the picked destination.
   - If find_flight returns {"flight": null}, call pick_destination again, passing every IATA code \
you've tried so far in tried_iata_codes, then call find_flight again for the new pick.
   - If every destination in the shortlist has been tried with no flight found, stop and tell the \
user no flights were available, listing the shortlist you considered.
4. book_flight — as soon as find_flight succeeds, book that flight immediately.

Once booked, reply with a human-readable summary: the shortlist you considered, the destination you \
picked, the flight details (flight number, route, date, price), and the booking confirmation (code, \
status). Never call book_flight before find_flight has succeeded for that destination. Do not ask the \
user clarifying questions — infer sensible defaults and proceed autonomously."""


# ---------------------------------------------------------------------------
# Agent — built via langchain.agents.create_agent (LangChain v1), not a
# hand-authored StateGraph. Still runs on LangGraph under the hood (same
# CompiledStateGraph runtime create_react_agent returned), so .ainvoke()/
# .astream_events() and the Postgres checkpointer behave identically.
# ---------------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None
_agent = None


async def build_checkpointer():
    """A Postgres-backed checkpointer for cross-request conversation memory.

    Falls back to no checkpointer (stateless runs) if Postgres isn't reachable,
    so the agent still works in environments without the llm-analytics Postgres.
    """
    global _pool
    try:
        _pool = AsyncConnectionPool(
            conninfo=CHECKPOINT_DB_URL, max_size=10, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await _pool.open()
        checkpointer = AsyncPostgresSaver(_pool)
        await checkpointer.setup()
        return checkpointer
    except Exception:
        logger.warning("Postgres checkpointer unavailable at %s — falling back to stateless runs", CHECKPOINT_DB_URL, exc_info=True)
        return None


async def build_agent():
    llm = build_llm()
    checkpointer = await build_checkpointer()
    # name= gives this agent's run a meaningful LangSmith trace root instead of
    # the library's generic default (LangSmith §5 — avoid invoke/run/function names).
    return create_agent(llm, TOOLS, system_prompt=SYSTEM_PROMPT, checkpointer=checkpointer, name=SERVICE_NAME)


async def get_agent():
    global _agent
    if _agent is None:
        _agent = await build_agent()
    return _agent


def _run_config(thread_id: str) -> dict[str, Any]:
    """Shared RunnableConfig for a single trip-planning run: thread_id for checkpointing,
    plus the metadata LangSmith attaches to the resulting trace (LangSmith §5)."""
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {**RUN_METADATA, "thread_id": thread_id},
        "run_name": SERVICE_NAME,
    }


async def run_agent(prompt: str, thread_id: str) -> dict[str, Any]:
    """Extract requirements, run the agent to completion, and shape a backward-compatible result dict."""
    requirements = extract_requirements(prompt, build_llm())
    agent = await get_agent()
    initial_message = HumanMessage(content=json.dumps({"trip_request": prompt, "requirements": requirements}))
    result = await agent.ainvoke({"messages": [initial_message]}, config=_run_config(thread_id))
    return summarize_result(result, requirements)


async def stream_agent(prompt: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
    """Stream agent execution events (token chunks, tool start/end) for a single trip-planning run."""
    requirements = extract_requirements(prompt, build_llm())
    agent = await get_agent()
    initial_message = HumanMessage(content=json.dumps({"trip_request": prompt, "requirements": requirements}))
    async for event in agent.astream_events({"messages": [initial_message]}, config=_run_config(thread_id), version="v2"):
        yield event


def summarize_result(result: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    """Pull the final answer, each tool's last result, and the tool-call sequence out of the
    agent's message history (IMPROVEMENTS.md §14: a structured result alongside the prose
    answer, so callers - including the eval harness - don't have to re-derive outcome/sequence
    from free text)."""
    messages = result.get("messages") or []
    answer = messages[-1].content if messages else ""
    shortlist = selected_destination = flight = booking = None
    tool_calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            payload = None
        tool_calls.append({"tool": message.name, "output": payload})
        if payload is None:
            continue
        if message.name == "get_destination_shortlist":
            shortlist = payload.get("destinations")
        elif message.name == "pick_destination":
            selected_destination = payload.get("selected_destination")
        elif message.name == "find_flight":
            flight = payload.get("flight")
        elif message.name == "book_flight":
            booking = payload.get("booking")
    status = "booked" if booking is not None else ("no_flight" if shortlist is not None else "failed")
    return {
        "answer": answer,
        "requirements": requirements,
        "shortlist": shortlist,
        "selected_destination": selected_destination,
        "flight": flight,
        "booking": booking,
        "status": status,
        "tool_calls": tool_calls,
    }


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Reduce a LangGraph astream_events payload to a JSON-serializable summary for SSE."""
    kind = event.get("event")
    name = event.get("name")
    data = event.get("data") or {}
    out: dict[str, Any] = {"event": kind, "name": name}
    if kind == "on_chat_model_stream":
        chunk = data.get("chunk")
        out["content"] = getattr(chunk, "content", "") or ""
    elif kind == "on_tool_start":
        out["input"] = data.get("input")
    elif kind == "on_tool_end":
        output = data.get("output")
        out["output"] = getattr(output, "content", output)
    return out


# ---------------------------------------------------------------------------
# A2A helper
# ---------------------------------------------------------------------------

def call_specialist(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send an A2A JSON-RPC request to a specialist agent and return the parsed response.

    The payload is serialized as JSON and placed in the A2A message text field.
    The specialist returns its result the same way — JSON in the response text.
    Routing through agentgateway means all inter-agent traffic is observable.

    The current LangSmith run's trace headers are attached (when tracing is active and this
    is called from within a traced tool run) so specialists that read them via
    `langsmith.run_helpers.tracing_context` nest their spans under this run.
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
    headers = {}
    run_tree = get_current_run_tree()
    if run_tree is not None:
        try:
            headers = run_tree.to_headers()
        except Exception:
            headers = {}
    with httpx.Client(timeout=30.0) as client:
        response = client.post(f"{base_url}/a2a/jsonrpc", json=body, headers=headers)
        response.raise_for_status()
    result = response.json()
    text = result["result"]["parts"][0]["text"]
    return json.loads(text)
