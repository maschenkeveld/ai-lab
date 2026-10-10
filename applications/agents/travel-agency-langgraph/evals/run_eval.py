"""Offline evaluation of the travel-orchestrator agent via LangSmith.

Calls the *deployed* orchestrator's A2A endpoint for each example in the dataset (same
protocol used for manual E2E testing and by real callers through agentgateway), scores each
run with the deterministic + semantic evaluators in evaluators.py, then runs a small
sequential conversation-state check that evaluate()'s concurrent per-example model can't
express (see check_thread_state).

Usage:
    export ORCHESTRATOR_URL=http://agentgateway.lab            # or the shared-pve-home host
    export LANGCHAIN_API_KEY=...       # LangSmith EU service key
    export LITELLM_BASE_URL=... LITELLM_API_KEY=...   # for the LLM-as-judge evaluator (defaults to in-cluster LiteLLM)
    uv run run_eval.py
"""

import json
import os
import uuid
from typing import Any

import httpx
from dataset import (
    DATASET_DESCRIPTION,
    DATASET_NAME,
    EXAMPLES,
    THREAD_STATE_OTHER_ID,
    THREAD_STATE_SAME_ID,
)
from dotenv import load_dotenv
from evaluators import ALL_EVALUATORS
from langsmith import Client
from langsmith.evaluation import evaluate
from langsmith.run_helpers import get_current_run_tree, traceable

load_dotenv()

os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://eu.api.smith.langchain.com")

# `.lab` is this repo's own convention (AGENTS.md) for the kind lab; a shared-pve-home
# deployment overrides this via env rather than the other way around.
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://agentgateway.lab")


def ensure_dataset(client: Client) -> str:
    """Create the dataset (idempotent) and return its name, carrying reference_outputs and
    category metadata so evaluators can actually check expected behavior, not just presence."""
    if not client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.create_dataset(DATASET_NAME, description=DATASET_DESCRIPTION)
        client.create_examples(
            dataset_id=dataset.id,
            examples=[
                {
                    "inputs": ex["inputs"],
                    "outputs": ex["reference_outputs"],
                    "metadata": ex["metadata"],
                }
                for ex in EXAMPLES
            ],
        )
    return DATASET_NAME


def _call_orchestrator(trip_request: Any, thread_id: str) -> dict[str, Any]:
    """POST one message to the orchestrator's A2A endpoint and return its structured result.

    Propagates the current LangSmith run's trace context (§6) so this call's specialist
    sub-traces nest under the eval experiment's run tree in LangSmith, the same way
    graph.py's call_specialist nests specialist calls under the orchestrator's own run tree.
    """
    headers = {"Content-Type": "application/json", "A2A-Version": "0.3.0"}
    run_tree = get_current_run_tree()
    if run_tree is not None:
        try:
            headers.update(run_tree.to_headers())
        except Exception:
            pass
    text = trip_request if isinstance(trip_request, str) else json.dumps(trip_request)
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/a2a/travel-orchestrator/a2a/jsonrpc",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                    "metadata": {"thread_id": thread_id},
                }
            },
        },
        timeout=90.0,
    )
    response.raise_for_status()
    result = response.json().get("result", {})
    answer = (result.get("parts") or [{}])[0].get("text", "")
    metadata = result.get("metadata") or {}
    return {
        "trip_request": trip_request,
        "answer": answer,
        "status": metadata.get("status"),
        "requirements": metadata.get("requirements"),
        "shortlist": metadata.get("shortlist"),
        "selected_destination": metadata.get("selected_destination"),
        "flight": metadata.get("flight"),
        "booking": metadata.get("booking"),
        "tool_calls": metadata.get("tool_calls"),
    }


@traceable(name="eval_target")
def target(inputs: dict, thread_id: str | None = None) -> dict:
    """evaluate()'s per-example target. Each example gets its own fresh thread_id (unless one
    is passed explicitly, as check_thread_state does) so concurrent examples can't bleed
    checkpointed state into each other."""
    return _call_orchestrator(inputs["trip_request"], thread_id or str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Sequential conversation-state check (§17) — deliberately NOT run through
# evaluate(), which scores independent examples concurrently. Thread
# continuity/isolation is inherently ordered (turn 2 depends on turn 1 having
# already happened), so it's a separate 3-call script instead of a dataset row.
# ---------------------------------------------------------------------------

THREAD_STATE_BOOKING_REQUEST = {
    "passenger_name": "ThreadStateCheck", "origin": "AMS", "date": "2026-09-05",
    "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
}
THREAD_STATE_FOLLOWUP = "What destination did you just book for me?"


def check_thread_state() -> dict[str, Any]:
    """Book a trip on THREAD_STATE_SAME_ID, then ask a follow-up question that only makes
    sense with that turn's context on the same thread_id (should recall the destination) and
    on a different thread_id (should NOT leak it - there's no prior turn on that thread).

    Caveat, stated plainly rather than papered over: the orchestrator's SYSTEM_PROMPT tells
    the agent to autonomously plan+book "one trip per conversation" - it has no explicit
    instruction for answering a conversational follow-up instead of starting a new booking
    flow. If the follow-up turn just re-runs the full tool chain instead of answering from
    history, that's a real, informative finding about this architecture (worth a design
    discussion per IMPROVEMENTS.md §26), not a bug in this check - the isolation half of the
    check (other_thread_does_not_leak_state) still holds either way.
    """
    turn1 = _call_orchestrator(THREAD_STATE_BOOKING_REQUEST, THREAD_STATE_SAME_ID)
    destination_name = (turn1.get("selected_destination") or {}).get("name")

    turn2 = _call_orchestrator(THREAD_STATE_FOLLOWUP, THREAD_STATE_SAME_ID)
    turn3 = _call_orchestrator(THREAD_STATE_FOLLOWUP, THREAD_STATE_OTHER_ID)

    recalls = bool(destination_name) and destination_name.lower() in (turn2.get("answer") or "").lower()
    isolated = bool(destination_name) and destination_name.lower() not in (turn3.get("answer") or "").lower()

    return {
        "turn1_status": turn1.get("status"),
        "destination_name": destination_name,
        "same_thread_recalls_prior_turn": recalls,
        "other_thread_does_not_leak_state": isolated,
    }


if __name__ == "__main__":
    client = Client()
    dataset_name = ensure_dataset(client)
    results = evaluate(
        target,
        data=dataset_name,
        evaluators=ALL_EVALUATORS,
        experiment_prefix="travel-orchestrator",
        max_concurrency=2,
    )
    print(results)

    print("\n--- thread-state check (sequential, not part of the LangSmith experiment) ---")
    print(json.dumps(check_thread_state(), indent=2))
