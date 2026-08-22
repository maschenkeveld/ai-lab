"""FastAPI server wrapping the orchestrator LangGraph graph. See graph.py for full architecture notes."""

import os
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from .graph import build_graph

AGENT_NAME = os.getenv("AGENT_NAME", "travel-orchestrator-langgraph")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/travel-orchestrator")

app = FastAPI(title=AGENT_NAME)
graph = build_graph()


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
        "description": "Travel agency orchestrator backed by LangGraph. Coordinates destination, flight-finder, and flight-booker specialist agents via A2A.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{
            "id": "plan-trip",
            "name": "Plan trip",
            "description": "Shortlist destinations, choose one, find a flight, and book it — using specialist agents for each step.",
            "tags": ["travel", "planning", "booking"],
            "examples": ["Plan a high budget museum trip for Maarten from AMS on 2026-07-15"],
        }],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/travel-orchestrator/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    prompt = extract_prompt(request.params or {})
    result = graph.invoke({"prompt": prompt})
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": result["answer"]}],
        },
    }


@app.post("/plan")
@app.post("/a2a/travel-orchestrator/plan")
def plan_trip(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or payload.get("text") or "")
    result = graph.invoke({"prompt": prompt})
    return {
        "answer": result["answer"],
        "requirements": result.get("requirements"),
        "shortlist": result.get("shortlist"),
        "selected_destination": result.get("selected_destination"),
        "flight": result.get("flight"),
        "booking": result.get("booking"),
    }


def extract_prompt(params: dict[str, Any]) -> str:
    message = params.get("message")
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
            return "\n".join(t for t in texts if t)
    text = params.get("text")
    return text if isinstance(text, str) else ""


if __name__ == "__main__":
    uvicorn.run("travel_orchestrator.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4200")))
