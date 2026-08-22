"""
Flight-booker specialist — books a confirmed flight.

Receives: {"passenger_name": "Maarten", "from": "AMS", "to": "BCN", "date": "2026-07-15"}
Returns:  {"booking": {...}}
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
AGENT_NAME      = os.getenv("AGENT_NAME", "travel-flight-booker")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/flight-booker")

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
@app.get("/a2a/flight-booker/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Books a flight given passenger name, origin, destination, and date.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "book-flight", "name": "Book flight", "tags": ["travel", "booking"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/flight-booker/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    result  = book_flight(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


def book_flight(payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{API_BASE_URL}/book-flights/v1/bookings",
        json={
            "name": payload["passenger_name"],
            "from": payload["from"],
            "to":   payload["to"],
            "date": payload["date"],
        },
        headers={"host": API_HOST_HEADER},
        timeout=10.0,
    )
    response.raise_for_status()
    data = response.json()
    return {"booking": data.get("booking", data)}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_flight_booker.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4204")))
