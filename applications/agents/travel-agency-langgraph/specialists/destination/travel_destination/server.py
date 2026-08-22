"""
Destination specialist — A2A wrapper around the destinations REST API.

Receives: {"vibes": [...], "budget_level": "high", "activities": [...], "limit": 5, "origin": "AMS"}
Returns:  {"destinations": [...]}  (filtered to exclude the origin airport)

Called by the orchestrator via agentgateway as an A2A JSON-RPC request.
"""

import json
import os
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

API_BASE_URL  = os.getenv("API_BASE_URL",  "http://traefik.traefik.svc.cluster.local")
API_HOST_HEADER = os.getenv("API_HOST_HEADER", "traefik-api-gw.lab")
AGENT_NAME    = os.getenv("AGENT_NAME", "travel-destination")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination")

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
@app.get("/a2a/destination/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Returns a shortlist of travel destinations matching vibes, budget, and activities.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "shortlist-destinations", "name": "Shortlist destinations", "tags": ["travel", "destinations"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    result = get_destinations(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


def get_destinations(payload: dict[str, Any]) -> dict[str, Any]:
    origin = payload.get("origin", "")
    response = httpx.post(
        f"{API_BASE_URL}/destinations/v1/destinations",
        json={
            "vibes":        payload.get("vibes", []),
            "budget_level": payload.get("budget_level", "high"),
            "activities":   payload.get("activities", []),
            "limit":        payload.get("limit", 5),
        },
        headers={"host": API_HOST_HEADER},
        timeout=10.0,
    )
    response.raise_for_status()
    data = response.json()
    destinations = [
        d for d in data.get("destinations", [])
        if origin not in d.get("airport_iata_codes", [])
    ][:5]
    return {"destinations": destinations}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON payload out of the A2A message text field."""
    message = params.get("message")
    if isinstance(message, dict):
        parts = message.get("parts", [])
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_destination.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4201")))
