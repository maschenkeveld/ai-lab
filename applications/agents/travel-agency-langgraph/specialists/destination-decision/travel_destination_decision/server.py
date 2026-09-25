"""
Destination-decision specialist — picks one destination from the shortlist by rolling a D20.

Receives: {"shortlist": [...], "tried_iata_codes": [...]}
Returns:  {"selected_destination": {...}}

Calls the dice-roller MCP server through agentgateway. The D20 result is used as an index
(modulo the number of untried candidates) to pick the destination — genuinely random,
no LLM call required.
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

setup_otel("travel-destination-decision")
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

AGENTGATEWAY_URL      = os.getenv("AGENTGATEWAY_URL", "http://agentgateway.lab")
AGENT_NAME            = os.getenv("AGENT_NAME", "travel-destination-decision")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination-decision")

_mcp_client: MultiServerMCPClient | None = None
_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _tools
    _mcp_client = MultiServerMCPClient({
        "dice-roller": {
            "url": f"{AGENTGATEWAY_URL}/mcp-dice-roller",
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
@app.get("/a2a/destination-decision/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Picks one destination from a shortlist by rolling a D20 via MCP dice-roller, "
                       "excluding already-tried destinations.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "pick-destination", "name": "Pick destination", "tags": ["travel", "decision", "mcp"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination-decision/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = await pick_destination(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


@traceable(name="pick_destination")
async def pick_destination(payload: dict[str, Any]) -> dict[str, Any]:
    shortlist  = payload.get("shortlist") or []
    tried      = set(payload.get("tried_iata_codes") or [])
    candidates = [d for d in shortlist if d.get("airport_iata_codes", [""])[0] not in tried] or shortlist

    if len(candidates) == 1:
        return {"selected_destination": candidates[0]}

    # Roll D20 via MCP — result is 1..20; map to a candidate index.
    roll_tool = next((t for t in _tools if "20" in t.name), None)
    roll_result = 1
    if roll_tool is not None:
        raw = await roll_tool.ainvoke({})
        data = _parse(raw)
        roll_result = data.get("result", 1) if isinstance(data, dict) else 1

    index = (roll_result - 1) % len(candidates)
    return {"selected_destination": candidates[index]}


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
    return {}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_destination_decision.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4202")))
