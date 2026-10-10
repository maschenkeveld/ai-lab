"""Deterministic + semantic evaluators for the travel-orchestrator eval dataset (IMPROVEMENTS.md §9-10).

All evaluators read the structured `outputs` dict `run_eval.py`'s `target()` builds from the
orchestrator's A2A response metadata (status/shortlist/selected_destination/flight/booking/
tool_calls - see graph.py's summarize_result and server.py's jsonrpc handler), not just the
prose answer. LangSmith's evaluate() introspects each function's parameter names and passes
whichever of (inputs, outputs, reference_outputs) it declares.

Kept deterministic and narrow on purpose (§9/§10: "Do not use an LLM judge for things that can
be checked deterministically") - only trip_matches_request at the bottom is an LLM-as-judge.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

BOOKING_CODE_RE = re.compile(r"\b[A-Z0-9]{6}\b")


def _tool_outputs(outputs: dict[str, Any], tool_name: str) -> list[Any]:
    """All outputs (in call order) for one tool name from the run's tool_calls."""
    return [tc.get("output") for tc in outputs.get("tool_calls") or [] if tc.get("tool") == tool_name]


# ---------------------------------------------------------------------------
# Booking outcome
# ---------------------------------------------------------------------------

def booking_succeeded(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """When a booking is expected: booking exists, has a code, status is 'booked'."""
    if not reference_outputs.get("must_book"):
        return {"key": "booking_succeeded", "score": True, "comment": "not expected to book; skipped"}
    booking = outputs.get("booking")
    ok = (
        outputs.get("status") == "booked"
        and isinstance(booking, dict)
        and bool(booking.get("code") or booking.get("booking_code") or booking.get("confirmation_code"))
    )
    return {"key": "booking_succeeded", "score": ok, "comment": f"status={outputs.get('status')!r}, booking={booking!r}"}


def no_false_booking(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """When no valid route exists: no booking, no fake booking code, no invented flight."""
    if reference_outputs.get("must_book") is not False:
        return {"key": "no_false_booking", "score": True, "comment": "booking expected or unspecified; skipped"}
    no_booking = outputs.get("booking") is None and outputs.get("status") != "booked"
    no_fake_code = not BOOKING_CODE_RE.search(outputs.get("answer", ""))
    return {
        "key": "no_false_booking",
        "score": no_booking and no_fake_code,
        "comment": f"booking={outputs.get('booking')!r}, answer_has_code_pattern={not no_fake_code}",
    }


def contains_booking_code(outputs: dict[str, Any]) -> dict[str, Any]:
    """Heuristic sanity check: the final answer should include a 6-char alphanumeric PNR
    whenever a booking actually happened (kept from the original eval as a cheap smoke check)."""
    if outputs.get("status") != "booked":
        return {"key": "contains_booking_code", "score": True, "comment": "not booked; skipped"}
    found = bool(BOOKING_CODE_RE.search(outputs.get("answer", "")))
    return {"key": "contains_booking_code", "score": found}


# ---------------------------------------------------------------------------
# Correctness of booked details
# ---------------------------------------------------------------------------

def origin_correct(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """The booked/searched flight starts at the expected origin."""
    expected = reference_outputs.get("origin")
    if not expected:
        return {"key": "origin_correct", "score": True, "comment": "no expected origin given; skipped"}
    flight = outputs.get("flight") or {}
    actual = flight.get("from") or (outputs.get("requirements") or {}).get("origin")
    return {"key": "origin_correct", "score": actual == expected, "comment": f"expected={expected!r}, actual={actual!r}"}


def date_correct(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """The booked flight's date matches the expected/requested date, when one was given."""
    expected = reference_outputs.get("date") or (outputs.get("requirements") or {}).get("date")
    if not expected:
        return {"key": "date_correct", "score": True, "comment": "no expected date given; skipped"}
    flight = outputs.get("flight") or {}
    actual = flight.get("date")
    if actual is None:
        return {"key": "date_correct", "score": True, "comment": "no flight booked; skipped"}
    return {"key": "date_correct", "score": actual == expected, "comment": f"expected={expected!r}, actual={actual!r}"}


def requirements_correct(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """Structured requirement extraction matches reference expectations, where given.
    Only checks fields actually present in reference_outputs (IMPROVEMENTS.md §9's
    requirements_correct) — e.g. a missing_values case can assert just origin without also
    asserting budget_level."""
    checkable = {"origin", "budget_level"}  # vibes/activities are free-form LLM picks, not asserted exactly
    reqs = outputs.get("requirements") or {}
    mismatches = {
        k: (reqs.get(k), reference_outputs[k])
        for k in checkable
        if k in reference_outputs and reqs.get(k) != reference_outputs[k]
    }
    return {
        "key": "requirements_correct",
        "score": not mismatches,
        "comment": f"mismatches={mismatches}" if mismatches else "ok",
    }


# ---------------------------------------------------------------------------
# Tool-sequence / retry-loop invariants
# ---------------------------------------------------------------------------

EXPECTED_SEQUENCE = ["get_destination_shortlist", "pick_destination", "find_flight"]


def tool_sequence_valid(outputs: dict[str, Any]) -> dict[str, Any]:
    """Normal expected order: shortlist, then (pick, find)+, then book only if a flight
    was found. Doesn't require exactly one pick/find pair — a retry loop repeats that pair."""
    names = [tc.get("tool") for tc in outputs.get("tool_calls") or []]
    if not names:
        return {"key": "tool_sequence_valid", "score": False, "comment": "no tool calls recorded"}
    if names[0] != "get_destination_shortlist":
        return {"key": "tool_sequence_valid", "score": False, "comment": f"must start with get_destination_shortlist, got {names[0]!r}"}
    # After the shortlist, every call must be pick/find/book, and book (if present) must be last.
    rest = names[1:]
    if any(n not in EXPECTED_SEQUENCE + ["book_flight"] for n in rest):
        return {"key": "tool_sequence_valid", "score": False, "comment": f"unexpected tool in sequence: {rest}"}
    if "book_flight" in rest and rest[-1] != "book_flight":
        return {"key": "tool_sequence_valid", "score": False, "comment": f"book_flight must be last: {rest}"}
    return {"key": "tool_sequence_valid", "score": True, "comment": f"sequence={names}"}


def never_book_before_flight(outputs: dict[str, Any]) -> dict[str, Any]:
    """Hard invariant: a book_flight call must be preceded by a find_flight call that
    actually returned a flight (not null)."""
    calls = outputs.get("tool_calls") or []
    seen_successful_flight = False
    for tc in calls:
        if tc.get("tool") == "find_flight":
            output = tc.get("output") or {}
            seen_successful_flight = output.get("flight") is not None
        elif tc.get("tool") == "book_flight":
            if not seen_successful_flight:
                return {"key": "never_book_before_flight", "score": False, "comment": "book_flight called without a prior successful find_flight"}
    return {"key": "never_book_before_flight", "score": True}


def no_duplicate_destination_retry(outputs: dict[str, Any]) -> dict[str, Any]:
    """A destination already known to fail shouldn't be picked again on a later retry."""
    picks = _tool_outputs(outputs, "pick_destination")
    codes = []
    for p in picks:
        dest = (p or {}).get("selected_destination") or {}
        iata = (dest.get("airport_iata_codes") or [None])[0]
        codes.append(iata)
    duplicates = [c for c in codes if c is not None and codes.count(c) > 1]
    return {
        "key": "no_duplicate_destination_retry",
        "score": not duplicates,
        "comment": f"picked codes in order: {codes}",
    }


def retry_behavior_valid(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
    """For a case expected to retry: select, find->none, select another, find->success (or
    exhaust the shortlist and stop cleanly). For a case NOT expected to retry: exactly one
    pick/find pair. Skips (scores True) when the dataset case doesn't make a claim either way
    (see dataset.py's retry-case caveat — we can't assert expect_retry reliably there)."""
    expect_retry = reference_outputs.get("expect_retry")
    if expect_retry is None:
        return {"key": "retry_behavior_valid", "score": True, "comment": "no expectation in this case; skipped"}
    find_calls = _tool_outputs(outputs, "find_flight")
    actually_retried = len(find_calls) > 1
    if expect_retry and not actually_retried:
        return {"key": "retry_behavior_valid", "score": False, "comment": f"expected a retry, only {len(find_calls)} find_flight call(s)"}
    if not expect_retry and actually_retried:
        return {"key": "retry_behavior_valid", "score": False, "comment": f"expected no retry, got {len(find_calls)} find_flight calls"}
    return {"key": "retry_behavior_valid", "score": True, "comment": f"find_flight calls: {len(find_calls)}"}


# ---------------------------------------------------------------------------
# Semantic (LLM-as-judge) — IMPROVEMENTS.md §10
# ---------------------------------------------------------------------------

LITELLM_BASE_URL = __import__("os").environ.get("LITELLM_BASE_URL", "http://litellm.lab/v1")
LITELLM_API_KEY = __import__("os").environ.get("LITELLM_API_KEY", "sk-ai-lab-litellm")
JUDGE_MODEL_ALIAS = __import__("os").environ.get("JUDGE_MODEL_ALIAS", "openai-gpt")


class TripMatchGrade(BaseModel):
    matches: bool = Field(description="Does the outcome reasonably match the traveler's vibe, activity, and budget?")
    reasoning: str = Field(description="One sentence explaining the judgment")


def _judge():
    # Imported lazily so this module can be imported (e.g. by tests) without langchain_openai
    # needing LiteLLM to be reachable at import time.
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=JUDGE_MODEL_ALIAS, temperature=0,
    ).with_structured_output(TripMatchGrade)


def trip_matches_request(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """LLM-as-judge, explicit rubric (§10): does the outcome reasonably fit the traveler's
    stated vibe/activity/budget? Lenient about exact destination choice - several destinations
    can validly fit the same preferences; this grades fit, not whether it matches one 'correct'
    answer."""
    import json as _json
    trip_request = inputs.get("trip_request")
    grade = _judge().invoke([
        (
            "system",
            "You grade whether a travel agent's trip plan fits the traveler's stated vibe, "
            "activity, and budget preferences. Rubric: (1) if a destination was selected, does "
            "it plausibly match the requested vibe/activity? (2) if a budget was requested, is "
            "nothing about the outcome inconsistent with it? (3) ignore whether it's the single "
            "'best' destination - several can validly fit. If no booking was possible (no_flight "
            "case), grade whether the agent's explanation is clear and honest, not whether a "
            "destination was eventually chosen.",
        ),
        (
            "human",
            f"Requested: {_json.dumps(trip_request)}\n\n"
            f"Status: {outputs.get('status')}\n"
            f"Selected destination: {_json.dumps(outputs.get('selected_destination'))}\n"
            f"Agent's final answer:\n{outputs.get('answer', '')}",
        ),
    ])
    return {"key": "trip_matches_request", "score": grade.matches, "comment": grade.reasoning}


DETERMINISTIC_EVALUATORS = [
    contains_booking_code,
    booking_succeeded,
    no_false_booking,
    origin_correct,
    date_correct,
    requirements_correct,
    tool_sequence_valid,
    never_book_before_flight,
    no_duplicate_destination_retry,
    retry_behavior_valid,
]

SEMANTIC_EVALUATORS = [trip_matches_request]

ALL_EVALUATORS = DETERMINISTIC_EVALUATORS + SEMANTIC_EVALUATORS
