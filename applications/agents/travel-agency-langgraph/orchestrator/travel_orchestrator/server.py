"""FastAPI server wrapping the orchestrator's LangGraph tool-calling agent. See graph.py for full architecture notes.

This file is deliberately "dumb": every endpoint just translates one HTTP shape into a call
to run_agent/resume_agent/stream_agent (all defined in graph.py) and translates the result
back into another HTTP shape. None of the actual agent logic — the tool-calling loop, the
human-approval interrupt, the checkpointer — lives here; this is just the network-facing door
into it. If you're trying to understand *how the agent thinks*, read graph.py instead; this
file is about *how other services talk to it*.
"""

import json
import os
import uuid
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .graph import (
    list_pending_approvals,
    resume_agent,
    run_agent,
    serialize_event,
    stream_agent,
)
from .otel import setup_otel

setup_otel("travel-orchestrator")

AGENT_NAME = os.getenv("AGENT_NAME", "travel-orchestrator-langgraph")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/travel-orchestrator")

app = FastAPI(title=AGENT_NAME)


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = Field(default_factory=lambda: str(uuid.uuid4()))
    method: str
    params: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/travel-orchestrator/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Travel agency orchestrator backed by a LangGraph tool-calling agent. Coordinates "
                       "destination, flight-finder, and flight-booker specialist agents via A2A tools.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.2.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "skills": [{
            "id": "plan-trip",
            "name": "Plan trip",
            "description": "Shortlist destinations, choose one, find a flight, and book it — using "
                           "specialist agents as tools. Conversations persist across calls via thread_id.",
            "tags": ["travel", "planning", "booking"],
            "examples": ["Plan a high budget museum trip for Maarten from AMS on 2026-07-15"],
        }],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/travel-orchestrator/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    """The main entry point: a specialist, the eval harness, or any other A2A caller sends a
    trip request here. This is where run_agent (graph.py) gets called — everything "agentic"
    happens inside that call; this function just unwraps the JSON-RPC envelope going in and
    wraps the result back into one going out. A single call here can kick off several LLM
    calls and several more A2A calls to specialists before it returns — or it can return
    almost immediately with status="awaiting_approval" if the agent reached book_flight's
    human-approval pause (see graph.py's interrupt() comment for what that means)."""
    params = request.params or {}
    prompt = extract_prompt(params)
    thread_id = extract_thread_id(params)
    result = await run_agent(prompt, thread_id)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": result["answer"]}],
            # Structured outcome alongside the prose answer - lets callers (the eval harness
            # in particular) check status/sequence/requirements
            # without re-parsing free text.
            "metadata": {
                "thread_id": thread_id,
                "status": result["status"],
                "requirements": result["requirements"],
                "shortlist": result["shortlist"],
                "selected_destination": result["selected_destination"],
                "flight": result["flight"],
                "booking": result["booking"],
                "tool_calls": result["tool_calls"],
                "pending_approval": result["pending_approval"],
            },
        },
    }


@app.post("/plan")
@app.post("/a2a/travel-orchestrator/plan")
async def plan_trip(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or payload.get("text") or "")
    thread_id = str(payload.get("thread_id") or uuid.uuid4())
    result = await run_agent(prompt, thread_id)
    return {"thread_id": thread_id, **result}


@app.post("/plan/stream")
@app.post("/a2a/travel-orchestrator/plan/stream")
async def plan_trip_stream(payload: dict[str, Any]) -> StreamingResponse:
    prompt = str(payload.get("prompt") or payload.get("text") or "")
    thread_id = str(payload.get("thread_id") or uuid.uuid4())

    async def event_source() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'event': 'start', 'thread_id': thread_id})}\n\n"
        async for event in stream_agent(prompt, thread_id):
            yield f"data: {json.dumps(serialize_event(event))}\n\n"
        yield f"data: {json.dumps({'event': 'end'})}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


class ApprovalRequest(BaseModel):
    thread_id: str
    approved: bool


# --- The two endpoints behind the human-in-the-loop gate ---
# Neither of these touches the LLM or the tool-calling loop directly — they're thin wrappers
# around graph.py's list_pending_approvals()/resume_agent(). The interesting part (interrupt()
# pausing the run, Command(resume=...) continuing it) all happens inside graph.py; this is
# just how a human reviewer's HTTP request reaches that mechanism.

@app.get("/pending")
@app.get("/a2a/travel-orchestrator/pending")
def pending_approvals() -> dict[str, Any]:
    """Runs currently paused awaiting a human booking decision — lets a reviewer (CLI tool,
    UI) discover what's waiting without already knowing a thread_id (this is the
    human-in-the-loop requirement: book_flight pauses here until approved or rejected).

    This reads an in-memory dict (graph.py's _pending_approvals), not the checkpointer
    directly — LangGraph itself has no built-in "list every currently-interrupted thread"
    query, which is exactly the gap this registry works around (see the friction log)."""
    return {"pending": list_pending_approvals()}


@app.post("/approve")
@app.post("/a2a/travel-orchestrator/approve")
async def approve(payload: ApprovalRequest) -> dict[str, Any]:
    """Resume a paused run with a human's approve/reject decision for its pending booking.

    `payload.thread_id` has to be one of the keys GET /pending just showed you — it's what
    tells LangGraph which specific paused run to continue (see resume_agent/Command(resume=...)
    in graph.py). `payload.approved` becomes the `decision` dict that book_flight's
    `interrupt(...)` call receives back, as if it had been waiting synchronously for it."""
    result = await resume_agent(payload.thread_id, payload.approved)
    return {"thread_id": payload.thread_id, **result}


def extract_prompt(params: dict[str, Any]) -> str:
    message = params.get("message")
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
            return "\n".join(t for t in texts if t)
    text = params.get("text")
    return text if isinstance(text, str) else ""


def extract_thread_id(params: dict[str, Any]) -> str:
    for holder in (params, params.get("message") if isinstance(params.get("message"), dict) else None):
        if not isinstance(holder, dict):
            continue
        metadata = holder.get("metadata")
        if isinstance(metadata, dict) and metadata.get("thread_id"):
            return str(metadata["thread_id"])
    return str(uuid.uuid4())


if __name__ == "__main__":
    uvicorn.run("travel_orchestrator.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4200")))
