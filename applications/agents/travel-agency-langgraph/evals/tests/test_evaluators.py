"""Unit tests for the deterministic evaluators in evaluators.py.

These exercise evaluator logic against synthetic `outputs`/`reference_outputs` dicts, not a
live orchestrator — they're a fast, infra-free regression check for the scoring logic itself
(CI has no reachable orchestrator deployment to run the real dataset against; see
IMPROVEMENTS.md §21/§25 and run_eval.py's own module docstring). Running the full dataset
against a live deployment stays a manual/scheduled step, not a PR check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluators import (
    booking_succeeded,
    never_book_before_flight,
    no_duplicate_destination_retry,
    no_false_booking,
    tool_sequence_valid,
)

BOOKED_OUTPUTS = {
    "answer": "Booked! Confirmation ABC123.",
    "status": "booked",
    "requirements": {"origin": "AMS", "budget_level": "high"},
    "selected_destination": {"name": "Lisbon", "airport_iata_codes": ["LIS"]},
    "flight": {"from": "AMS", "to": "LIS", "date": "2026-09-01"},
    "booking": {"code": "ABC123"},
    "tool_calls": [
        {"tool": "get_destination_shortlist", "output": {"destinations": [{"name": "Lisbon", "airport_iata_codes": ["LIS"]}]}},
        {"tool": "pick_destination", "output": {"selected_destination": {"name": "Lisbon", "airport_iata_codes": ["LIS"]}}},
        {"tool": "find_flight", "output": {"flight": {"from": "AMS", "to": "LIS", "date": "2026-09-01"}}},
        {"tool": "book_flight", "output": {"booking": {"code": "ABC123"}}},
    ],
}

NO_FLIGHT_OUTPUTS = {
    "answer": "Sorry, no flights available.",
    "status": "no_flight",
    "booking": None,
    "tool_calls": [
        {"tool": "get_destination_shortlist", "output": {"destinations": []}},
        {"tool": "pick_destination", "output": {"selected_destination": {"name": "Lisbon", "airport_iata_codes": ["LIS"]}}},
        {"tool": "find_flight", "output": {"flight": None}},
        {"tool": "pick_destination", "output": {"selected_destination": {"name": "Rome", "airport_iata_codes": ["FCO"]}}},
        {"tool": "find_flight", "output": {"flight": None}},
    ],
}


def test_booking_succeeded_true_for_a_real_booking():
    assert booking_succeeded(BOOKED_OUTPUTS, {"must_book": True})["score"] is True


def test_booking_succeeded_skips_when_not_expected():
    assert booking_succeeded(NO_FLIGHT_OUTPUTS, {"must_book": False})["score"] is True


def test_no_false_booking_true_for_a_genuine_no_flight_case():
    assert no_false_booking(NO_FLIGHT_OUTPUTS, {"must_book": False})["score"] is True


def test_no_false_booking_catches_a_fabricated_code_in_the_answer():
    fabricated = {**NO_FLIGHT_OUTPUTS, "answer": "Booked under code ZZYYXX just in case!"}
    assert no_false_booking(fabricated, {"must_book": False})["score"] is False


def test_tool_sequence_valid_accepts_a_normal_booking_sequence():
    assert tool_sequence_valid(BOOKED_OUTPUTS)["score"] is True


def test_tool_sequence_valid_rejects_book_flight_in_the_middle():
    bad = {**BOOKED_OUTPUTS, "tool_calls": list(reversed(BOOKED_OUTPUTS["tool_calls"]))}
    assert tool_sequence_valid(bad)["score"] is False


def test_never_book_before_flight_accepts_a_real_success():
    assert never_book_before_flight(BOOKED_OUTPUTS)["score"] is True


def test_never_book_before_flight_catches_booking_without_a_successful_find_flight():
    bad = {
        "tool_calls": [
            {"tool": "find_flight", "output": {"flight": None}},
            {"tool": "book_flight", "output": {"booking": {"code": "X"}}},
        ]
    }
    assert never_book_before_flight(bad)["score"] is False


def test_no_duplicate_destination_retry_accepts_distinct_retries():
    assert no_duplicate_destination_retry(NO_FLIGHT_OUTPUTS)["score"] is True


def test_no_duplicate_destination_retry_catches_picking_the_same_destination_twice():
    bad = dict(NO_FLIGHT_OUTPUTS)
    bad["tool_calls"] = [
        {"tool": "pick_destination", "output": {"selected_destination": {"name": "Lisbon", "airport_iata_codes": ["LIS"]}}},
        {"tool": "find_flight", "output": {"flight": None}},
        {"tool": "pick_destination", "output": {"selected_destination": {"name": "Lisbon", "airport_iata_codes": ["LIS"]}}},
        {"tool": "find_flight", "output": {"flight": None}},
    ]
    assert no_duplicate_destination_retry(bad)["score"] is False
