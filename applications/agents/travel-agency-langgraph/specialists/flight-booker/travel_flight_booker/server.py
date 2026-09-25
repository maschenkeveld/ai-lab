"""
Flight-booker specialist — books a confirmed flight via the MCP book-flights adapter.

Receives: {"passenger_name": "Maarten", "from": "AMS", "to": "BCN", "date": "2026-07-15"}
Returns:  {"booking": {...}}

Calls the book-flights MCP adapter through agentgateway using MultiServerMCPClient.
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from langchain_mcp_adapters.client import MultiServerMCPClient
from .otel import setup_otel

setup_otel("travel-flight-booker")
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

AGENTGATEWAY_URL      = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
AGENT_NAME            = os.getenv("AGENT_NAME", "travel-flight-booker")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/flight-booker")

_mcp_client: MultiServerMCPClient | None = None
_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _tools
    _mcp_client = MultiServerMCPClient({
        "book-flights": {
            "url": f"{AGENTGATEWAY_URL}/mcp-book-flights",
            "transport": "streamable_http",
        }
    })
    _tools = await _mcp_client.get_tools()
    yield


app = FastAPI(title=AGENT_NAME, lifespan=lifespan)


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
        "description": "Books a flight given passenger name, origin, destination, and date via MCP.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "book-flight", "name": "Book flight", "tags": ["travel", "booking", "mcp"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/flight-booker/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = await book_flight(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


@traceable(name="book_flight")
async def book_flight(payload: dict[str, Any]) -> dict[str, Any]:
    tool = next(t for t in _tools if t.name == "create_booking")
    raw = await tool.ainvoke({
        "passenger_name": payload["passenger_name"],
        "from_iata":      payload["from"],
        "to_iata":        payload["to"],
        "date":           payload["date"],
    })
    data = _parse(raw)
    booking = data.get("booking", data) if isinstance(data, dict) else data
    return {"booking": booking}


def _parse(raw: Any) -> Any:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
        raw = raw[0]["text"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    return raw


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_flight_booker.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4204")))
