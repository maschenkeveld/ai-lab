"""
Flight-finder specialist — searches for a flight to a given destination.

Receives: {"origin": "AMS", "to_iata": "BCN", "date": "2026-07-15"}
Returns:  {"flight": {...}}  on success, or {"flight": null} if none found.

The null response is the signal to the orchestrator's retry loop to try
a different destination rather than failing the whole workflow.
"""

import json
import os
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

API_BASE_URL    = os.getenv("API_BASE_URL",    "http://traefik.traefik.svc.cluster.local")
API_HOST_HEADER = os.getenv("API_HOST_HEADER", "traefik-api-gw.lab")
AGENT_NAME      = os.getenv("AGENT_NAME", "travel-flight-finder")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/flight-finder")

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
@app.get("/a2a/flight-finder/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Searches for a flight between two airports on a given date.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "find-flight", "name": "Find flight", "tags": ["travel", "flights"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/flight-finder/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    result  = find_flight(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


def find_flight(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{API_BASE_URL}/flights/v1/price",
            json={"from": payload["origin"], "to": payload["to_iata"], "date": payload["date"]},
            headers={"host": API_HOST_HEADER},
            timeout=10.0,
        )
        response.raise_for_status()
        return {"flight": response.json()}
    except Exception:
        # Return null flight — the orchestrator's route_after_flight will retry with a different destination.
        return {"flight": None}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_flight_finder.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4203")))
