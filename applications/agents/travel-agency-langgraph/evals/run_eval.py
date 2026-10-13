"""Offline evaluation of the travel-orchestrator agent via LangSmith.

Calls the *deployed* orchestrator's A2A endpoint for each example in the dataset (same
protocol used for manual E2E testing and by real callers through agentgateway), scores each
run with the deterministic + semantic evaluators in evaluators.py, then runs a small
sequential conversation-state check that evaluate()'s concurrent per-example model can't
express (see check_thread_state).

Usage:
    export ORCHESTRATOR_URL=http://agentgateway.lab   # override for the local kind lab
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

# Defaults to the real shared-pve-home deployment, reachable on the home LAN — this is where
# evals actually get run in practice. Override to http://agentgateway.lab for the throwaway
# local kind lab instead.
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://agentgateway.shared.pve-home.schenkeveld.io")


def ensure_dataset(client: Client) -> str:
    """Create the dataset if missing, and otherwise make sure its examples actually match
    EXAMPLES in dataset.py - not just that *a* dataset with this name exists.

    `has_dataset` alone isn't enough: a dataset created by an older version of this script
    (schema-mismatched field names, no reference_outputs) silently stuck around under the
    same name and every later run kept reusing it unchanged, since the old code only ever
    checked existence. That produced a misleadingly "clean" run: with no reference_outputs on
    those stale examples, most evaluators here short-circuit to a vacuous "not applicable;
    skipped" pass rather than actually checking anything - confirmed live, where the real
    eval run silently fell back to 6 old examples with no reference_outputs for weeks of
    dataset.py changes. This replaces the dataset's examples outright on every run instead,
    trading "preserve hand-edits made in the LangSmith UI" for the dataset always reflecting
    what dataset.py actually says - the right tradeoff for a dev/CI eval set, not a
    manually-curated one.
    """
    target = [
        {"inputs": ex["inputs"], "outputs": ex["reference_outputs"], "metadata": ex["metadata"]}
        for ex in EXAMPLES
    ]
    if not client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.create_dataset(DATASET_NAME, description=DATASET_DESCRIPTION)
        client.create_examples(dataset_id=dataset.id, examples=target)
        return DATASET_NAME

    dataset = client.read_dataset(dataset_name=DATASET_NAME)
    existing = list(client.list_examples(dataset_id=dataset.id))
    if len(existing) != len(target) or any(ex.inputs != t["inputs"] for ex, t in zip(existing, target)):
        if existing:
            client.delete_examples(example_ids=[ex.id for ex in existing])
        client.create_examples(dataset_id=dataset.id, examples=target)
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
        "pending_approval": metadata.get("pending_approval"),
    }


def _approve(thread_id: str, approved: bool) -> dict[str, Any]:
    """POST a human decision for a run paused at book_flight's approval interrupt, and return
    its result in the same shape _call_orchestrator does (so evaluators don't care which path
    produced a given example's outputs)."""
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/a2a/travel-orchestrator/approve",
        json={"thread_id": thread_id, "approved": approved},
        timeout=90.0,
    )
    response.raise_for_status()
    body = response.json()
    return {
        "trip_request": None,
        "answer": body.get("answer"),
        "status": body.get("status"),
        "requirements": body.get("requirements"),
        "shortlist": body.get("shortlist"),
        "selected_destination": body.get("selected_destination"),
        "flight": body.get("flight"),
        "booking": body.get("booking"),
        "tool_calls": body.get("tool_calls"),
        "pending_approval": body.get("pending_approval"),
    }


@traceable(name="eval_target")
def target(inputs: dict, thread_id: str | None = None) -> dict:
    """evaluate()'s per-example target. Each example gets its own fresh thread_id (unless one
    is passed explicitly, as check_thread_state does) so concurrent examples can't bleed
    checkpointed state into each other.

    book_flight now pauses for human approval (the HITL requirement) before any booking
    happens — these dataset examples are about requirements extraction, retries, etc., not
    about the approval gate itself, so this auto-approves on their behalf and scores the
    post-approval outcome. The approval mechanism itself is exercised directly and for real
    by check_human_approval below, which specifically tests both the approve and reject paths.
    """
    tid = thread_id or str(uuid.uuid4())
    result = _call_orchestrator(inputs["trip_request"], tid)
    if result.get("status") == "awaiting_approval":
        result = _approve(tid, approved=True)
    return result


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
    sense with that turn's context on the same thread_id (should recall the booking) and on
    a different thread_id (should NOT leak it - there's no prior turn on that thread).

    Confirmed live against shared-pve-home, and worth stating plainly rather than papering
    over (IMPROVEMENTS.md §26): "same_thread_recalls_prior_turn" fails even on a genuinely
    continued thread. The Postgres checkpointer does persist the full message history across
    calls on one thread_id, but the orchestrator's SYSTEM_PROMPT only ever says to
    autonomously plan+book "one trip per conversation" and "infer sensible defaults and
    proceed autonomously" - it has no instruction for recognizing a follow-up question and
    answering from history instead. In practice, every call - including the second call on
    the SAME thread_id - re-extracts requirements from just that call's own prompt text
    (falling back to schema defaults when the prompt doesn't look like a trip request) and
    the agent re-runs the entire shortlist→pick→find→book sequence again, producing a brand
    new, unrelated booking. State persistence and conversational memory are not the same
    thing here: the checkpointer works, but nothing in this architecture uses it to answer
    "what did you just book me" conversationally. That's a real gap worth a design
    discussion, not a bug in this check - and it means a fresh thread_id's independent
    re-booking can legitimately land on the same destination as a prior thread's by chance
    (confirmed below), which is why isolation is checked via booking code, not destination.

    It did, however, expose a real bug in an earlier version of this check: isolation was
    compared by destination name, and with only ~5 shortlist candidates plus a stochastic
    dice roll in production (no DICE_ROLL_DETERMINISTIC on shared-pve-home), two independent
    fresh bookings landed on the same destination by sheer chance (confirmed by manually
    replaying turn 3 - it ran its own full shortlist→pick→find→book sequence and produced a
    different, unrelated booking code). Comparing the booking code instead fixes this -
    collision odds across independent 6-character alphanumeric codes are negligible, so a
    match is a real signal of leaked state rather than coincidence.

    Each run uses a fresh thread_id (THREAD_STATE_SAME_ID/OTHER_ID plus a random suffix)
    rather than a fixed one. An earlier version reused the same fixed thread_id across runs,
    and a stray out-of-band call into that thread while it was paused on book_flight's
    interrupt() permanently corrupted its checkpoint (an assistant message with a tool_calls
    entry that never got a matching tool response - OpenAI's API rejects this on every
    subsequent call). A fixed thread_id has no way to recover from that; a fresh one per run
    does, at the cost of not literally reusing "the same" thread across separate recordings.

    book_flight now pauses for human approval (the HITL gate) before turn1's booking
    completes, same as target() handles for the main dataset - this turn1 is auto-approved on
    its own behalf (this check is about thread continuity, not the approval gate itself, which
    check_human_approval exercises directly) before turn2 is sent, since sending a second
    message into a still-paused thread is exactly the corruption scenario described above.
    """
    same_id = f"{THREAD_STATE_SAME_ID}-{uuid.uuid4().hex[:8]}"
    other_id = f"{THREAD_STATE_OTHER_ID}-{uuid.uuid4().hex[:8]}"

    turn1 = _call_orchestrator(THREAD_STATE_BOOKING_REQUEST, same_id)
    if turn1.get("status") == "awaiting_approval":
        turn1 = _approve(same_id, approved=True)
    destination_name = (turn1.get("selected_destination") or {}).get("name")
    booking_code = (turn1.get("booking") or {}).get("booking_code")

    turn2 = _call_orchestrator(THREAD_STATE_FOLLOWUP, same_id)
    turn3 = _call_orchestrator(THREAD_STATE_FOLLOWUP, other_id)

    recalls = bool(booking_code) and booking_code in (turn2.get("answer") or "")
    leaked = bool(booking_code) and booking_code in (turn3.get("answer") or "")

    return {
        "turn1_status": turn1.get("status"),
        "destination_name": destination_name,
        "booking_code": booking_code,
        "same_thread_recalls_prior_turn": recalls,
        "other_thread_does_not_leak_state": not leaked,
    }


# ---------------------------------------------------------------------------
# Human-in-the-loop check: book_flight's approval interrupt. Also sequential/
# not through evaluate() — approving and rejecting are two separate full runs,
# not independent dataset rows, and rejecting specifically needs to assert a
# negative (the booking specialist is never reached), which only a live,
# controlled call-then-decide sequence can show.
# ---------------------------------------------------------------------------

HITL_APPROVE_REQUEST = {
    "passenger_name": "HitlApprove", "origin": "AMS", "date": "2026-09-20",
    "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
}
HITL_REJECT_REQUEST = {
    "passenger_name": "HitlReject", "origin": "AMS", "date": "2026-09-21",
    "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
}


def check_human_approval() -> dict[str, Any]:
    """Exercises both outcomes of the book_flight human-approval gate for real: a normal
    booking request must pause with the pending booking's details before anything is booked,
    approving it must complete the booking, and rejecting a separate request must leave it
    unbooked rather than booking anyway. This is the assignment's human-in-the-loop
    requirement — tested directly, not just implemented.

    Each run uses a fresh thread_id (see check_thread_state's docstring for why a fixed one
    is unsafe): if a prior run ever crashed between pausing and approving, a fixed thread_id
    would leave that thread permanently stuck on an unresolved interrupt, and this run's call
    into the same thread_id would corrupt it the same way.
    """
    approve_id = f"eval-hitl-approve-{uuid.uuid4().hex[:8]}"
    reject_id = f"eval-hitl-reject-{uuid.uuid4().hex[:8]}"

    paused = _call_orchestrator(HITL_APPROVE_REQUEST, approve_id)
    pauses_for_approval = paused.get("status") == "awaiting_approval" and paused.get("booking") is None and bool(paused.get("pending_approval"))

    approved_result = _approve(approve_id, approved=True)
    approval_completes_booking = approved_result.get("status") == "booked" and approved_result.get("booking") is not None

    paused2 = _call_orchestrator(HITL_REJECT_REQUEST, reject_id)
    rejected_result = _approve(reject_id, approved=False)
    rejection_blocks_booking = (
        paused2.get("status") == "awaiting_approval"
        and rejected_result.get("status") == "rejected"
        and rejected_result.get("booking") is None
    )

    return {
        "pauses_for_approval": pauses_for_approval,
        "approval_completes_booking": approval_completes_booking,
        "rejection_blocks_booking": rejection_blocks_booking,
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

    print("\n--- human-in-the-loop approval check (sequential, not part of the LangSmith experiment) ---")
    print(json.dumps(check_human_approval(), indent=2))
