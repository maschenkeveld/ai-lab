"""
Destination-decision specialist — picks one destination from the shortlist using an LLM.

Receives: {"shortlist": [...], "tried_iata_codes": [...]}
Returns:  {"selected_destination": {...}}

Calls the in-cluster LiteLLM proxy via langchain-openai's ChatOpenAI with structured output to
choose a destination, excluding already-tried ones. Traced via LangSmith; nested under the
orchestrator's run when trace headers are propagated from the A2A caller.
"""

import json
import os
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from langchain_openai import ChatOpenAI
from langsmith import traceable
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel, Field

AGENT_NAME = os.getenv("AGENT_NAME", "travel-destination-decision")
PUBLIC_AGENT_BASE_URL = os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/destination-decision")

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm.litellm.svc.cluster.local:4000/v1")
LITELLM_API_KEY  = os.getenv("LITELLM_API_KEY", "sk-ai-lab-litellm")
LITELLM_MODEL    = os.getenv("LITELLM_MODEL", "openai-gpt")

app = FastAPI(title=AGENT_NAME)


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = Field(default_factory=lambda: str(uuid.uuid4()))
    method: str
    params: dict[str, Any] | None = None


class DestinationChoice(BaseModel):
    selected_iata: str = Field(description="The airport_iata_codes[0] of the chosen destination.")
    reason: str = Field(description="One-sentence justification for the pick.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/destination-decision/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": "Picks one destination from a shortlist using an LLM, excluding already-tried ones.",
        "url": PUBLIC_AGENT_BASE_URL,
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "pick-destination", "name": "Pick destination", "tags": ["travel", "decision"]}],
    }


@app.post("/a2a/jsonrpc")
@app.post("/a2a/destination-decision/a2a/jsonrpc")
def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
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


@traceable(name="pick_destination")
def pick_destination(payload: dict[str, Any]) -> dict[str, Any]:
    shortlist = payload.get("shortlist") or []
    tried     = payload.get("tried_iata_codes") or []
    candidates = [d for d in shortlist if d["airport_iata_codes"][0] not in tried] or shortlist
    if len(candidates) <= 1:
        return {"selected_destination": candidates[0]}

    llm = ChatOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_MODEL, temperature=0)
    choice = llm.with_structured_output(DestinationChoice).invoke([
        {"role": "system", "content": "Pick the single best travel destination for a generically "
                                       "appealing trip from the candidates below. Respond with its IATA code."},
        {"role": "user", "content": json.dumps(candidates)},
    ])
    selected = next(
        (d for d in candidates if d["airport_iata_codes"][0] == choice.selected_iata),
        candidates[0],
    )
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
