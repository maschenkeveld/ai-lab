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
        book_flight                (flight-booker specialist; pauses for human approval)
    → final answer message

Each tool sends an A2A JSON-RPC request to a specialist service via agentgateway.
The specialist does the actual work (REST call, LLM call, or computation) and
returns a JSON payload in the A2A response text. LangSmith traces the full agent
run (and, via header propagation in call_specialist, the specialist calls it makes)
when LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY are set.

Conversation state (including which destinations have been tried) is persisted per
`thread_id` in Postgres via AsyncPostgresSaver, not in pod-local memory — so the
orchestrator's compute is horizontally scalable (any replica can serve any request) while
conversation state lives externally in Postgres.

Unresolved issue, stated plainly rather than hidden: there is no session affinity /
distributed locking around a given `thread_id`. Two concurrent requests for the same
`thread_id` hitting two different replicas can both read the same checkpoint and then race
to write the next one — the checkpointer makes state external, it does not by itself make
concurrent access to one thread_id safe. This is why the production-style GitOps deployment
currently keeps this service at a single replica rather than scaling it blindly; scaling it
for real would need either sticky routing on thread_id at the gateway or per-thread locking
at the application level, neither of which exists today.

=====================================================================================
READING GUIDE FOR LEARNING THE AGENTIC PARTS OF THIS FILE
=====================================================================================
If you're new to LangChain/LangGraph, the "agentic" machinery in this file boils down to
four ideas, each explained in depth at its definition below:

1. A *tool* (the @tool decorator) is just a normal Python function the LLM is allowed to
   call. The LLM never runs the function itself — it decides to call it, LangGraph runs it,
   and the return value goes back into the conversation as a new message.

2. create_agent() builds a loop (technically a small LangGraph state machine, though you
   never see its nodes/edges) that alternates between "ask the LLM what to do next" and
   "run whatever tool it asked for" until the LLM stops calling tools and just replies.
   This is the classic "ReAct" pattern (Reason + Act), and it's why there's no hand-written
   if/else control flow for the booking process — the SYSTEM_PROMPT is the control flow,
   and the LLM is the thing executing it, one tool call at a time.

3. A *checkpointer* is what lets that loop be paused and resumed across separate HTTP
   requests. Every step of the loop gets saved, keyed by a thread_id you choose, so calling
   the agent again later with the same thread_id continues exactly where it left off.

4. interrupt() is a special way to pause the loop *in the middle of a tool call* and wait
   for an external decision (a human, in this case) before continuing. It only works because
   of #3 — without a checkpointer, there's nowhere to save the paused state.
=====================================================================================
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
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command, interrupt
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .verbose import CALLBACKS, VERBOSE, truncate

logger = logging.getLogger(__name__)


# Specialist A2A base URLs — routed through agentgateway so traffic is observable/proxied.
# "A2A" (Agent-to-Agent) here just means: one agent service calls another over plain HTTP
# using a JSON-RPC envelope, the same way you'd call any REST API — there's no special
# LangChain transport involved. The orchestrator doesn't import or run the specialists'
# code directly; it sends them a JSON payload and reads back a JSON response (see
# call_specialist at the bottom of this file).
AGENTGATEWAY_URL = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
SPECIALIST_DESTINATION          = os.getenv("SPECIALIST_DESTINATION",          f"{AGENTGATEWAY_URL}/a2a/destination")
SPECIALIST_DESTINATION_DECISION = os.getenv("SPECIALIST_DESTINATION_DECISION", f"{AGENTGATEWAY_URL}/a2a/destination-decision")
SPECIALIST_FLIGHT_FINDER        = os.getenv("SPECIALIST_FLIGHT_FINDER",        f"{AGENTGATEWAY_URL}/a2a/flight-finder")
SPECIALIST_FLIGHT_BOOKER        = os.getenv("SPECIALIST_FLIGHT_BOOKER",        f"{AGENTGATEWAY_URL}/a2a/flight-booker")

DEFAULT_PASSENGER = os.getenv("DEFAULT_PASSENGER", "Maarten")
DEFAULT_ORIGIN    = os.getenv("DEFAULT_ORIGIN", "AMS")
DEFAULT_DATE      = os.getenv("DEFAULT_TRAVEL_DATE", "2026-07-15")

# LiteLLM is a model-routing proxy: the agent always talks to "an OpenAI-compatible API",
# and LiteLLM decides which real provider/model sits behind the alias in LITELLM_MODEL
# (e.g. "openai-gpt" -> gpt-4o-mini, "gemini-pro" -> gemini-2.5-pro). This is what let us
# swap models for the baseline-vs-candidate experiment by changing one env var, with zero
# code changes — the agent code never hardcodes a specific model provider.
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
    """A LangChain chat model pointed at the in-cluster LiteLLM proxy.

    ChatOpenAI is LangChain's wrapper around "any API that speaks the OpenAI chat-completions
    protocol" — it's not tied to OpenAI specifically, which is exactly why pointing base_url
    at LiteLLM instead of https://api.openai.com works without any other code changes.
    temperature=0 asks the model to be as deterministic as it can be (still not perfectly
    deterministic — see the friction log on this), which matters for an agent that's
    supposed to follow the SYSTEM_PROMPT's steps reliably rather than improvise.
    """
    return ChatOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_MODEL, temperature=0)


# ---------------------------------------------------------------------------
# Requirement extraction — structured LLM call, replaces regex parsing
# ---------------------------------------------------------------------------

class TripRequirements(BaseModel):
    """A Pydantic schema the LLM is asked to fill in (see extract_requirements below).

    This is "structured output": instead of asking the model to reply in prose and then
    trying to regex/parse that prose for an origin airport and a date, we hand the model
    this schema and the LangChain/provider machinery (via with_structured_output) forces
    the model's reply to actually conform to it — every field present, correctly typed.
    Each Field's `description` isn't just documentation for humans; it's sent to the model
    as part of the schema, so it directly shapes what the model extracts. That's why the
    "food" exclusion note below isn't just a comment — changing that description is the
    actual fix for the bug it describes.
    """
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


# LCEL ("LangChain Expression Language") chain: ChatPromptTemplate | llm.with_structured_output
# The | operator is Python's bitwise-or, overloaded by LangChain so that chaining two
# "Runnable" objects with it builds a pipeline: the left side's output becomes the right
# side's input when you call .invoke(). Reading left to right: format a prompt, then feed
# it to a model that's been configured to return a TripRequirements object instead of a
# plain ChatMessage. `with_structured_output(TripRequirements)` is what actually attaches
# the schema from above — under the hood it typically asks the provider for JSON-mode or
# tool-calling output and parses the result back into that Pydantic model for you.
REQUIREMENTS_CHAIN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Extract structured trip requirements from the traveller's request below."),
    ("human", "{input}"),
])


@traceable(name="extract_trip_requirements", run_type="chain")
def extract_requirements(prompt: str, llm: ChatOpenAI) -> dict[str, Any]:
    """Extract structured trip requirements via an LCEL chain: prompt | llm with structured output.

    @traceable is a LangSmith decorator: it doesn't change what this function does, it just
    wraps the call so LangSmith records it as a named span in the trace (here, as a "chain"
    type span called "extract_trip_requirements"). Without it, this call would still work
    exactly the same, you'd just have a harder time telling it apart from everything else
    in a trace. This is a *plain* Python function (not a @tool) because the agent doesn't
    choose to call it — run_agent always calls it once, up front, before the agent loop
    even starts, to turn free text into structured data the agent's tools expect as input.
    """
    chain = REQUIREMENTS_CHAIN_PROMPT | llm.with_structured_output(TripRequirements)
    requirements: TripRequirements = chain.invoke({"input": prompt.strip()})
    return requirements.model_dump()


# ---------------------------------------------------------------------------
# Tools — each wraps an A2A call to a specialist agent
# ---------------------------------------------------------------------------
# Everything in this section is a plain Python function with a @tool decorator on it.
# @tool does three things: (1) it registers the function so create_agent() can hand it to
# the LLM, (2) it turns the function's type hints and docstring into a JSON-schema
# description the LLM actually reads to decide *when* and *with what arguments* to call it
# — the docstring here isn't just documentation for you, it's the tool's user manual as far
# as the model is concerned, and (3) it wraps the function so LangGraph can invoke it with
# the exact arguments the LLM chose. The LLM never sees or runs this Python code directly:
# it emits a structured "call get_destination_shortlist with these arguments" message, and
# LangGraph is the thing that actually executes the function and feeds the return value
# back in as a ToolMessage.

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
        # found no route" — same {"flight": null} shape so the agent's
        # existing retry logic is unaffected, but the extra "error" key lets tool_calls/traces
        # (and future evaluators) tell the two apart instead of treating every failure alike.
        result = {"flight": None, "error": "backend_unavailable"}
    return json.dumps(result)


@tool
def book_flight(passenger_name: str, from_: str, to: str, date: str) -> str:
    """Book the previously-found flight for the passenger. Only call this after find_flight succeeded.

    This pauses the run for human approval before actually booking — a human must approve or
    reject via POST /a2a/travel-orchestrator/approve with this run's thread_id before the
    booking happens (or is skipped). Call it once you're ready to book; you'll see the
    outcome (booked, or cancelled by the reviewer) once a decision is made.

    Args:
        passenger_name: name to book the flight under.
        from_: origin airport IATA code (the flight's "from").
        to: destination airport IATA code (the flight's "to").
        date: travel date as YYYY-MM-DD.

    Returns:
        JSON string {"booking": {...}, "approval": "approved"} on success, or
        {"booking": null, "approval": "rejected"} if the human reviewer declined.
    """
    # --- Human-in-the-loop, explained ---
    # interrupt(payload) does something unusual for a normal function call: it hands `payload`
    # out of the agent entirely (run_agent sees it as result["__interrupt__"][0].value) and
    # then *stops executing this function right here* — literally does not return, does not
    # continue to the lines below — until someone resumes the run.
    #
    # "Resuming" means a separate, later call to agent.ainvoke(Command(resume=<value>), ...)
    # using the *same* thread_id (see resume_agent below). When that happens, LangGraph
    # doesn't start book_flight over from the top — it restores exactly the state this
    # function was in when it called interrupt(), and interrupt() itself returns <value> as
    # if it had been a normal blocking call all along (that's the `decision` variable below).
    # Everything from this line down only runs *after* a human has responded.
    #
    # This is only possible because create_agent() was built with a checkpointer (see
    # build_checkpointer/build_agent) — the checkpointer is what actually saves "book_flight
    # was paused here, with these local variables" to Postgres/memory so it can be restored
    # later, possibly by a completely different process handling the /approve HTTP request.
    decision = interrupt({
        "action": "approve_booking",
        "passenger_name": passenger_name,
        "from": from_,
        "to": to,
        "date": date,
    })
    if not decision.get("approved"):
        return json.dumps({"booking": None, "approval": "rejected"})
    result = call_specialist(SPECIALIST_FLIGHT_BOOKER, {
        "passenger_name": passenger_name, "from": from_, "to": to, "date": date,
    })
    result["approval"] = "approved"
    return json.dumps(result)


# The list create_agent() actually receives. Order doesn't matter here — the LLM decides
# which tool to call and when based on the SYSTEM_PROMPT and each tool's own docstring, not
# on this list's order.
TOOLS = [get_destination_shortlist, pick_destination, find_flight, book_flight]

# This is the agent's *entire* control flow. There is no Python code anywhere that says
# "first call get_destination_shortlist, then pick_destination, then..." — that sequencing
# exists only as instructions to the LLM, which decides, one step at a time, which tool (if
# any) to call next. This is sometimes called a "ReAct" agent (Reason, then Act, repeat):
# at each step the model reasons about what it's seen so far and either calls one more tool
# or decides it's done and writes a final reply. create_agent() provides the loop that
# keeps asking "what next?" until the model stops calling tools.
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


# ---------------------------------------------------------------------------
# Agent — built via langchain.agents.create_agent (LangChain v1), not a
# hand-authored StateGraph. Still runs on LangGraph under the hood (same
# CompiledStateGraph runtime create_react_agent returned), so .ainvoke()/
# .astream_events() and the Postgres checkpointer behave identically.
#
# What create_agent() actually builds, conceptually: a small graph with (roughly) two kinds
# of steps that alternate — "call the LLM with the conversation so far" and "run whichever
# tool(s) the LLM just asked for" — plus the routing logic that decides, after each LLM
# call, whether to go run a tool or stop and return the LLM's reply as final. You never
# construct that graph by hand here; create_agent() does it from just an llm, a TOOLS list,
# and a system_prompt. This is the "modernized" replacement for the older
# langgraph.prebuilt.create_react_agent — same idea, newer LangChain v1 API surface.
# ---------------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None
_agent = None

# In-memory registry of runs currently paused at book_flight's approval interrupt, so a human
# reviewer (CLI tool, UI, whatever calls /approve) can discover what's pending without already
# knowing a thread_id out of band. Scoped to this process's lifetime, same as the orchestrator's
# single-replica constraint already documented above for the checkpointer itself.
_pending_approvals: dict[str, dict[str, Any]] = {}


async def build_checkpointer():
    """A Postgres-backed checkpointer for cross-request conversation memory.

    A checkpointer is the piece of infrastructure that makes an agent "resumable": after
    every step of the agent loop (every LLM call, every tool call), LangGraph serializes the
    current state — the full message history, where interrupt() last paused, everything —
    and saves it under a key you choose (thread_id, see _run_config below). The *next* time
    you call .ainvoke() with that same thread_id, LangGraph loads that saved state first and
    continues from there instead of starting fresh. Without a checkpointer, every call to
    the agent would be a brand-new conversation with no memory of any previous call, and
    interrupt() (see book_flight) would have nowhere to save a paused run at all.

    Falls back to an in-memory checkpointer (lost on restart, but otherwise fully
    functional for the lifetime of this pod) if Postgres isn't reachable, rather than no
    checkpointer at all. This matters beyond conversation memory: book_flight's human-approval
    interrupt (see below) requires *some* checkpointer to persist the paused run - with no
    checkpointer, a run could never actually pause for approval, it could only fail outright.
    """
    global _pool
    pool = AsyncConnectionPool(
        conninfo=CHECKPOINT_DB_URL, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    try:
        # wait=True makes this actually attempt a connection and raise on failure (bad
        # credentials, unreachable host) within `timeout` seconds. The previous
        # fire-and-forget `await pool.open()` (wait=False, the default) returned
        # successfully regardless of whether Postgres was reachable at all, leaving the
        # pool's background workers retrying a doomed connection forever - confirmed live,
        # this didn't just silently no-op: the endless retry churn built up enough resource
        # pressure under real request load to get this pod OOM/SIGKilled outright.
        await pool.open(wait=True, timeout=10)
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        _pool = pool
        return checkpointer
    except Exception:
        logger.warning("Postgres checkpointer unavailable at %s — falling back to an in-memory checkpointer", CHECKPOINT_DB_URL, exc_info=True)
        await pool.close()
        return InMemorySaver()


async def build_agent():
    """Assemble the actual runnable agent: one LLM, the four tools above, the system prompt
    that tells the LLM how to use them, and a checkpointer so runs can be paused/resumed.
    This is the one place all four pieces come together into something .ainvoke()-able."""
    llm = build_llm()
    checkpointer = await build_checkpointer()
    # name= gives this agent's run a meaningful LangSmith trace root instead of
    # the library's generic default (LangSmith §5 — avoid invoke/run/function names).
    return create_agent(llm, TOOLS, system_prompt=SYSTEM_PROMPT, checkpointer=checkpointer, name=SERVICE_NAME)


async def get_agent():
    """Build the agent once per process and reuse it — building it opens the checkpointer's
    connection pool, which you don't want to redo on every single request."""
    global _agent
    if _agent is None:
        _agent = await build_agent()
    return _agent


def _run_config(thread_id: str) -> dict[str, Any]:
    """Shared RunnableConfig for a single trip-planning run: thread_id for checkpointing,
    plus the metadata LangSmith attaches to the resulting trace (LangSmith §5), plus the
    console trace callback (verbose.py) that prints each tool call when VERBOSE is set.

    `thread_id` is the only thing that actually controls checkpointing behavior here — it's
    the key the checkpointer uses to find (or create) this conversation's saved state. Every
    call this file makes to .ainvoke() passes this same config shape, built from a thread_id,
    so "same thread_id" is the one and only thing that makes two separate HTTP requests
    share state (conversation history, a paused interrupt, anything).
    """
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {**RUN_METADATA, "thread_id": thread_id},
        "run_name": SERVICE_NAME,
        "callbacks": CALLBACKS,
    }


async def run_agent(prompt: str, thread_id: str) -> dict[str, Any]:
    """Extract requirements, run the agent until it completes or pauses for approval, and
    shape a backward-compatible result dict."""
    if VERBOSE:
        logger.info("=== [%s] New request: %s ===", thread_id, truncate(prompt))
    requirements = extract_requirements(prompt, build_llm())
    if VERBOSE:
        logger.info("  requirements: %s", requirements)
    agent = await get_agent()
    initial_message = HumanMessage(content=json.dumps({"trip_request": prompt, "requirements": requirements}))
    # This is the actual agent loop kicking off: .ainvoke() keeps alternating LLM calls and
    # tool calls (see the big comment above the Agent section) until either the model stops
    # calling tools and just replies, or a tool calls interrupt() and the whole run pauses.
    # Either way, .ainvoke() returns once — it's not a generator; for a step-by-step view of
    # the same run, see stream_agent()'s .astream_events() below instead.
    result = await agent.ainvoke({"messages": [initial_message]}, config=_run_config(thread_id))
    # "__interrupt__" showing up in the result dict is exactly how you tell, from the
    # caller's side, that the run paused instead of finishing — see book_flight's interrupt()
    # call and the big comment there for what's actually happening when this is true.
    if "__interrupt__" in result:
        summary = summarize_pending(result, requirements)
        _pending_approvals[thread_id] = summary["pending_approval"]
        if VERBOSE:
            logger.info("=== [%s] Awaiting approval: %s ===", thread_id, summary["pending_approval"])
        return summary
    summary = summarize_result(result, requirements)
    if VERBOSE:
        logger.info("=== [%s] Done: status=%s ===", thread_id, summary["status"])
    return summary


async def resume_agent(thread_id: str, approved: bool) -> dict[str, Any]:
    """Resume a run paused at book_flight's human-approval interrupt with a human decision.

    Command(resume=...) is the counterpart to interrupt(): calling .ainvoke() with a Command
    instead of a fresh {"messages": [...]} input tells LangGraph "don't start a new run — go
    find the run that's paused on thread_id and continue it, handing this value back as
    interrupt()'s return value." That's the `decision` book_flight receives. This only finds
    the right paused run because of the checkpointer and because we pass the *same*
    thread_id (via _run_config) that the original run used.
    """
    if VERBOSE:
        logger.info("=== [%s] Resuming with approved=%s ===", thread_id, approved)
    agent = await get_agent()
    result = await agent.ainvoke(Command(resume={"approved": approved}), config=_run_config(thread_id))
    _pending_approvals.pop(thread_id, None)
    requirements = _requirements_from_messages(result.get("messages") or [])
    summary = summarize_result(result, requirements)
    if VERBOSE:
        logger.info("=== [%s] Done: status=%s ===", thread_id, summary["status"])
    return summary


def list_pending_approvals() -> dict[str, dict[str, Any]]:
    """All runs currently paused awaiting a human booking decision, keyed by thread_id."""
    return dict(_pending_approvals)


async def stream_agent(prompt: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
    """Stream agent execution events (token chunks, tool start/end) for a single trip-planning run.

    Same agent, same config shape as run_agent — the only difference is .astream_events()
    instead of .ainvoke(). Where .ainvoke() waits for the whole run and hands back one final
    result, .astream_events() yields an event for every intermediate step as it happens
    (a token as the model generates it, a tool starting, a tool finishing) — useful for a
    live-updating UI, but not currently taught to handle the approval interrupt gracefully;
    if a streamed run reaches book_flight, the stream just ends without a clean "paused"
    event (a known gap, not yet needed by any current caller).
    """
    if VERBOSE:
        logger.info("=== [%s] New streaming request: %s ===", thread_id, truncate(prompt))
    requirements = extract_requirements(prompt, build_llm())
    if VERBOSE:
        logger.info("  requirements: %s", requirements)
    agent = await get_agent()
    initial_message = HumanMessage(content=json.dumps({"trip_request": prompt, "requirements": requirements}))
    async for event in agent.astream_events({"messages": [initial_message]}, config=_run_config(thread_id), version="v2"):
        yield event
    if VERBOSE:
        logger.info("=== [%s] Streaming request done ===", thread_id)


def _scan_tool_calls(messages: list[Any]) -> dict[str, Any]:
    """Shared by summarize_result/summarize_pending: pull each tool's result and the full
    tool-call sequence out of the agent's message history.

    `messages` here is the agent's whole conversation so far, as LangChain message objects —
    HumanMessage (what the user/system sent in), AIMessage (what the LLM said, including any
    tool calls it requested), and ToolMessage (what a tool returned, tagged with which tool
    produced it). Reading your own tool outputs back out of this list, rather than having
    each tool push its result somewhere else, is deliberate: the message history *is* the
    agent's state, and it's exactly what the checkpointer saves/restores — so anything a
    caller needs to know about a run should be derivable from it.
    """
    shortlist = selected_destination = flight = booking = None
    rejected = False
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
            rejected = payload.get("approval") == "rejected"
    return {
        "shortlist": shortlist, "selected_destination": selected_destination,
        "flight": flight, "booking": booking, "rejected": rejected, "tool_calls": tool_calls,
    }


def summarize_result(result: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    """Pull the final answer, each tool's last result, and the tool-call sequence out of the
    agent's message history - a structured result alongside the prose answer, so callers
    (including the eval harness) don't have to re-derive outcome/sequence from free text."""
    messages = result.get("messages") or []
    answer = messages[-1].content if messages else ""
    scan = _scan_tool_calls(messages)
    if scan["booking"] is not None:
        status = "booked"
    elif scan["rejected"]:
        status = "rejected"
    elif scan["shortlist"] is not None:
        status = "no_flight"
    else:
        status = "failed"
    return {
        "answer": answer,
        "requirements": requirements,
        "shortlist": scan["shortlist"],
        "selected_destination": scan["selected_destination"],
        "flight": scan["flight"],
        "booking": scan["booking"],
        "status": status,
        "tool_calls": scan["tool_calls"],
        "pending_approval": None,
    }


def summarize_pending(result: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    """Shape a result for a run paused at book_flight's human-approval interrupt: everything up
    to the pending booking is already known from completed tool calls; only the booking itself
    is on hold until a human resolves it via POST /a2a/travel-orchestrator/approve.

    `result["__interrupt__"]` is a list (LangGraph supports multiple simultaneous interrupts
    in more complex graphs; this agent only ever produces one at a time) of Interrupt objects.
    `.value` is exactly the dict book_flight passed into interrupt(...) — this is how the
    paused tool call's "I need a decision on this" payload makes it all the way out to an
    HTTP response.
    """
    messages = result.get("messages") or []
    scan = _scan_tool_calls(messages)
    pending = result["__interrupt__"][0].value
    return {
        "answer": "Awaiting human approval before booking — see pending_approval for details.",
        "requirements": requirements,
        "shortlist": scan["shortlist"],
        "selected_destination": scan["selected_destination"],
        "flight": scan["flight"],
        "booking": None,
        "status": "awaiting_approval",
        "tool_calls": scan["tool_calls"],
        "pending_approval": pending,
    }


def _requirements_from_messages(messages: list[Any]) -> dict[str, Any]:
    """Recover the requirements dict a paused run was started with, from its first message —
    used when resuming, so resume_agent doesn't need the original prompt re-supplied or
    re-extracted (which would needlessly call the LLM again with no new information)."""
    for message in messages:
        if isinstance(message, HumanMessage):
            try:
                return json.loads(message.content).get("requirements", {})
            except (TypeError, ValueError):
                return {}
    return {}


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Reduce a LangGraph astream_events payload to a JSON-serializable summary for SSE.

    Each event from .astream_events() is a dict with an "event" name describing what just
    happened under the hood — "on_chat_model_stream" for one streamed token of the LLM's
    reply, "on_tool_start"/"on_tool_end" for a tool call beginning/finishing, plus several
    other event kinds this function doesn't bother forwarding. This just picks out the
    handful that are useful for a live UI and flattens them into something json.dumps can
    serialize directly (the raw events contain LangChain objects, not plain dicts).
    """
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

    This is plain, synchronous httpx — nothing LangChain-specific about the HTTP call
    itself. The only LangSmith-aware part is the trace-header propagation below: it's what
    makes five separate services' traces show up as one connected tree in LangSmith instead
    of five unrelated ones.

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
    # get_current_run_tree() asks LangSmith "is there a trace currently active on this
    # thread of execution, and if so, what is it?" — it works because @traceable (on
    # extract_requirements) and create_agent's own internal tracing set up that context
    # before calling into tool functions like the ones above, which call this helper.
    # .to_headers() turns that active trace's identity into HTTP headers (similar in spirit
    # to how distributed tracing systems like OpenTelemetry propagate a trace ID across
    # service calls). The specialist on the other end reads those headers back out via
    # `tracing_context(parent=...)` and nests its own spans underneath this one.
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
