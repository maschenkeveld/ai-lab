"""Offline evaluation of the travel-orchestrator agent via LangSmith.

Calls the *deployed* orchestrator (same A2A endpoint used for manual E2E
testing) for each example in the dataset, then scores each run with:
  - contains_booking_code: heuristic — did the trip actually get booked
  - trip_matches_request:  LLM-as-judge — does the pick fit the ask

Usage:
    export ORCHESTRATOR_URL=http://agentgateway.shared.pve-home.schenkeveld.io
    export LANGCHAIN_API_KEY=...       # LangSmith EU service key
    export OPENAI_API_KEY=...          # for the LLM-as-judge evaluator
    uv run run_eval.py
"""

import json
import os
import re
import uuid

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langsmith import Client
from langsmith.evaluation import evaluate
from pydantic import BaseModel, Field

from dataset import DATASET_DESCRIPTION, DATASET_NAME, EXAMPLES

load_dotenv()

os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://eu.api.smith.langchain.com")

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL", "http://agentgateway.shared.pve-home.schenkeveld.io"
)
BOOKING_CODE_RE = re.compile(r"\b[A-Z0-9]{6}\b")


def ensure_dataset(client: Client) -> str:
    """Create the dataset (idempotent) and return its name."""
    if not client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.create_dataset(DATASET_NAME, description=DATASET_DESCRIPTION)
        client.create_examples(
            dataset_id=dataset.id,
            examples=[{"inputs": ex["inputs"]} for ex in EXAMPLES],
        )
    return DATASET_NAME


def target(inputs: dict) -> dict:
    """Call the deployed orchestrator's A2A endpoint with one trip request."""
    trip_request = inputs["trip_request"]
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/a2a/travel-orchestrator/a2a/jsonrpc",
        headers={"Content-Type": "application/json", "A2A-Version": "0.3.0"},
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"kind": "text", "text": json.dumps(trip_request)}],
                }
            },
        },
        timeout=60.0,
    )
    response.raise_for_status()
    body = response.json()
    text = body.get("result", {}).get("parts", [{}])[0].get("text", "")
    return {"trip_request": trip_request, "answer": text}


def contains_booking_code(outputs: dict) -> dict:
    """Heuristic: the final answer should include a 6-char alphanumeric PNR."""
    found = bool(BOOKING_CODE_RE.search(outputs.get("answer", "")))
    return {"key": "contains_booking_code", "score": found}


class TripMatchGrade(BaseModel):
    matches: bool = Field(description="Does the booked trip fit the requested vibe/budget/activity?")
    reasoning: str = Field(description="One sentence explaining the judgment")


_judge = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(TripMatchGrade)


def trip_matches_request(inputs: dict, outputs: dict) -> dict:
    """LLM-as-judge: does the chosen destination fit the requested preferences?"""
    trip_request = outputs.get("trip_request", inputs.get("trip_request", {}))
    grade = _judge.invoke([
        (
            "system",
            "You grade whether a travel agent's trip plan fits the traveler's stated "
            "preferences. Be lenient about specific destination choice — judge fit to "
            "vibe/budget/activity, not whether it's your personal favorite.",
        ),
        (
            "human",
            f"Requested: {json.dumps(trip_request)}\n\n"
            f"Agent's response:\n{outputs.get('answer', '')}",
        ),
    ])
    return {"key": "trip_matches_request", "score": grade.matches, "comment": grade.reasoning}


if __name__ == "__main__":
    client = Client()
    dataset_name = ensure_dataset(client)
    results = evaluate(
        target,
        data=dataset_name,
        evaluators=[contains_booking_code, trip_matches_request],
        experiment_prefix="travel-orchestrator",
        max_concurrency=2,
    )
    print(results)
