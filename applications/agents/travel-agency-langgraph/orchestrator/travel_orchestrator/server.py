"""FastAPI server wrapping the orchestrator's LangGraph tool-calling agent. See graph.py for full architecture notes."""

import json
import os
import uuid
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .graph import run_agent, serialize_event, stream_agent

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
            "metadata": {"thread_id": thread_id},
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
