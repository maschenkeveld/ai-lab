"""
Destination-decision specialist — picks one destination from the shortlist.

Receives: {"shortlist": [...], "tried_iata_codes": [...], "seed": "...prompt..."}
Returns:  {"selected_destination": {...}}

Pure computation — no REST API calls. Filters out tried destinations and
uses a deterministic-but-varying random seed so each retry picks a different spot.
"""

import json
import os
import random
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

AGENT_NAME = os.getenv("AGENT_NAME", "travel-destination-decision")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination-decision")

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
@app.get("/a2a/destination-decision/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Picks one destination from a shortlist, excluding already-tried ones.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "pick-destination", "name": "Pick destination", "tags": ["travel", "decision"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination-decision/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    result = pick_destination(payload)
    return {
        "jsonrpc": "2.0",
        "id": request.id,
        "result": {
            "messageId": str(uuid.uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": json.dumps(result)}],
        },
    }


def pick_destination(payload: dict[str, Any]) -> dict[str, Any]:
    shortlist = payload.get("shortlist", [])
    tried     = set(payload.get("tried_iata_codes") or [])
    seed      = (payload.get("seed", "") or "ai-lab-oss") + str(len(tried))
    candidates = [d for d in shortlist if d["airport_iata_codes"][0] not in tried] or shortlist
    selected   = candidates[random.Random(seed).randrange(len(candidates))]
    return {"selected_destination": selected}


def extract_json(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("kind") == "text":
                return json.loads(part["text"])
    return {}


if __name__ == "__main__":
    uvicorn.run("travel_destination_decision.server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4202")))
