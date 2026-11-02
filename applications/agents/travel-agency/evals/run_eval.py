"""Offline evaluation of the travel orchestrator against the live cluster.

    uv run python run_eval.py              # deterministic checks + LLM judge
    uv run python run_eval.py --no-judge   # deterministic checks only (no extra LLM calls)

Needs the lab running (`task up`) and a working LLM key. ORCHESTRATOR_URL defaults to the agentgateway route;
LITELLM_BASE_URL (for the judge) defaults to litellm.lab.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

import httpx
from evaluators import (
    DecisionHonored,
    NothingBookedBeforeApproval,
    PendingMatchesFlight,
    RequirementsMatch,
    SelectionFromShortlist,
    ShortlistExcludesOrigin,
)
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import LLMJudge

BASE = os.getenv("ORCHESTRATOR_URL", "http://agentgateway.lab/a2a/travel-orchestrator").rstrip("/")
os.environ.setdefault("LITELLM_BASE_URL", "http://litellm.lab/v1")

JUDGE_RUBRIC = (
    "The answer is a clear reply to a traveller. If a booking was approved it states the destination, flight "
    "and confirmation code; if it was rejected it says nothing was booked; it never claims a booking that did "
    "not happen, and it does not invent details that are absent from the run."
)

CASES = [
    Case(
        name="culture-city-approve",
        inputs="Cultural city trip from Amsterdam, mid budget, museums, flying 2026-12-18. Book it.",
        expected_output={"origin": "AMS", "date": "2026-12-18"},
        metadata={"decision": "approve"},
    ),
    Case(
        name="culture-city-reject",
        inputs="Cultural city trip from Amsterdam, mid budget, museums, flying 2026-12-20. Book it.",
        expected_output={"origin": "AMS", "date": "2026-12-20"},
        metadata={"decision": "reject"},
    ),
    Case(
        name="other-origin",
        inputs="I'm in Madrid (MAD). City and culture trip, mid budget, museums, 2026-12-22. Book it.",
        expected_output={"origin": "MAD", "date": "2026-12-22"},
        metadata={"decision": "reject"},
    ),
]


def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"{BASE}{path}", json=body, timeout=300)
    response.raise_for_status()
    return response.json()


def run_trip(prompt: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan a trip, then give the paused booking the case's human decision."""
    decision = (metadata or {}).get("decision", "reject")
    thread_id = f"eval-{uuid.uuid4().hex[:8]}"
    plan = post("/plan", {"prompt": prompt, "thread_id": thread_id})
    final = None
    if plan.get("status") == "awaiting_approval":
        final = post("/approve", {"thread_id": thread_id, "approved": decision == "approve"})
    return {"plan": plan, "final": final}


def thread_continuity() -> bool:
    """A second request on the same thread_id sees the first: it can name the destination that was picked."""
    thread_id = f"eval-{uuid.uuid4().hex[:8]}"
    plan = post("/plan", {"prompt": CASES[0].inputs, "thread_id": thread_id})
    if plan.get("status") == "awaiting_approval":
        post("/approve", {"thread_id": thread_id, "approved": False})
    selected = (plan.get("selected_destination") or {}).get("name")
    follow_up = post("/plan", {"prompt": "Which destination did you pick for me?", "thread_id": thread_id})
    return bool(selected) and selected.lower() in follow_up.get("answer", "").lower()


def build_dataset(judge: bool) -> Dataset[str, dict[str, Any], dict[str, Any]]:
    evaluators: list[Any] = [
        RequirementsMatch(),
        ShortlistExcludesOrigin(),
        SelectionFromShortlist(),
        PendingMatchesFlight(),
        DecisionHonored(),
        NothingBookedBeforeApproval(),
    ]
    if judge:
        from travel_common.model import build_model

        evaluators.append(LLMJudge(
            rubric=JUDGE_RUBRIC,
            model=build_model(),
            include_input=True,
            assertion={"evaluation_name": "answer_quality", "include_reason": True},
            score=False,
        ))
    return Dataset(name="travel-orchestrator", cases=CASES, evaluators=evaluators)


def main() -> int:
    dataset = build_dataset(judge="--no-judge" not in sys.argv)

    async def task(prompt: str) -> dict[str, Any]:
        return await asyncio.to_thread(run_trip, prompt, CASE_METADATA[prompt])

    report = dataset.evaluate_sync(task, max_concurrency=1)
    report.print(include_input=False, include_output=False, include_reasons=True)

    continuity = thread_continuity()
    print(f"\nthread_continuity: {'PASS' if continuity else 'FAIL'}")

    failed = any(not a.value for case in report.cases for a in case.assertions.values())
    return 1 if failed or not continuity else 0


CASE_METADATA = {case.inputs: case.metadata for case in CASES}

if __name__ == "__main__":
    sys.exit(main())
