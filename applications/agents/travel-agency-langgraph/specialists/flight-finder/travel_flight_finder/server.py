"""
Flight-finder specialist — searches for a flight via the MCP flights adapter.

Receives: {"origin": "AMS", "to_iata": "BCN", "date": "2026-07-15"}
Returns:  {"flight": {...}} on success, or {"flight": null} if none found.

The null response signals the orchestrator's retry loop to try a different destination.
Calls the flights MCP adapter through agentgateway using MultiServerMCPClient.
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

setup_otel("travel-flight-finder")
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

AGENTGATEWAY_URL      = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
AGENT_NAME            = os.getenv("AGENT_NAME", "travel-flight-finder")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/flight-finder")

_mcp_client: MultiServerMCPClient | None = None
_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _tools
    _mcp_client = MultiServerMCPClient({
        "flights": {
            "url": f"{AGENTGATEWAY_URL}/mcp-flights",
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
@app.get("/a2a/flight-finder/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Searches for a flight between two airports on a given date via MCP.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "find-flight", "name": "Find flight", "tags": ["travel", "flights", "mcp"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/flight-finder/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = await find_flight(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


@traceable(name="find_flight")
async def find_flight(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        tool = next(t for t in _tools if t.name == "flight_price")
        raw = await tool.ainvoke({
            "from_iata": payload["origin"],
            "to_iata":   payload["to_iata"],
            "date":      payload["date"],
        })
        data = _parse(raw)
        return {"flight": data}
    except Exception:
        return {"flight": None}


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
    uvicorn.run("travel_flight_finder.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4203")))
