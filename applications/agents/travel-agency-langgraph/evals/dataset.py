"""Trip requests for evaluating the travel-orchestrator agent end-to-end.

Each example's `inputs.trip_request` is serialized to JSON and sent as free text to the
orchestrator's /a2a endpoint — the orchestrator's own `extract_requirements` LLM call
parses it into the real `TripRequirements` schema (passenger_name, origin, date,
budget_level, vibes: list[str], activities: list[str]; see graph.py). Field names below
intentionally match that schema (not an older, looser shape this dataset used to use)
so a reader can see the real contract directly from the dataset.

A `trip_request` can also just be a plain string (see the "ambiguous" category) — the
orchestrator's extraction step is an LLM call over free text either way; a dict is just a
convenient, readable way to express a request precisely.

## Categories (IMPROVEMENTS.md §7)

- happy_path: normal requests that should book successfully.
- missing_values: one or more fields omitted; verifies the stated defaults kick in
  (passenger_name→"Maarten", origin→"AMS", date→"2026-07-15", budget_level→"high",
  vibes→["culture","city"], activities→["museums"]; see TripRequirements in graph.py).
- no_flight: engineered to make *every* candidate destination fail at find_flight, so the
  agent must exhaust the shortlist and report no booking rather than inventing one.
- ambiguous: unstructured free-text requests.
- retry: see the important caveat below — included for completeness, not confidence.

## Why there's no genuine "first destination fails, second succeeds" case

IMPROVEMENTS.md §7 asks for a retry scenario (destination A → no flight → destination B →
flight found). After reading the actual backend code, this isn't realistically
constructable with the current demo data, for two independent reasons:

1. `rest-destinations`' and `rest-flights`' airport data are kept in perfect sync (same 33
   IATA codes on both sides) — every destination the shortlist can return already has a
   working flight route, so find_flight structurally can't fail for a normal candidate.
2. The `destination` specialist's `get_destinations` (server.py) *pre-filters out* any
   destination whose own airports include the traveler's origin, before the shortlist is
   even built — so the one obvious way to force a single candidate to fail (origin ==
   destination's airport, triggering rest-flights' "from and to must be different" error)
   never reaches pick_destination/find_flight at all; that candidate is just absent from
   the shortlist, not present-and-failing.

So under the current architecture, find_flight only fails in a way that affects *every*
candidate uniformly (an origin rest-flights doesn't recognize - see NO_FLIGHT_CASE below),
or via a genuine backend outage - never a single-destination failure. Constructing a real
"fails once, succeeds on retry" case would mean mocking the flights backend for one
specific destination, which wouldn't be an honest integration-level eval case. This is a
real architectural property worth a design discussion, not a dataset gap to paper over
(see IMPROVEMENTS.md §26) - noted here instead of faking a case that can't actually happen
end-to-end. The `retry_behavior_valid`/`no_duplicate_destination_retry` evaluators
(evaluators.py) still exist and are exercised by DESTINATION_DECISION_DETERMINISTIC mode
at the unit level; they just can't be driven through a realistic dataset case today.

## Deterministic vs stochastic (§11)

Which destination gets picked first is normally a dice roll (destination-decision's
server.py). Evals that care about *which* destination is tried (the no_flight case here
doesn't — every candidate fails identically, so pick order is irrelevant) should point
ORCHESTRATOR_URL at a deployment with destination-decision's DICE_ROLL_DETERMINISTIC=true.

## "food" belongs in vibes, not activities

Confirmed live: `activities: ["food"]` always produces an empty shortlist, because the
destinations backend's activities field never literally contains "food" (it's modeled as a
*vibe* - several mid-budget destinations have `vibes: [..., "food"]`) and the shortlist
filter requires an exact string match. `TripRequirements`'s own field description used to
list "food" as a valid activity (fixed in graph.py); examples here that want a food-focused
trip put "food" in `vibes` instead.
"""

from typing import Any

Example = dict[str, Any]

# ---------------------------------------------------------------------------
# happy_path — normal requests that should book successfully
# ---------------------------------------------------------------------------
HAPPY_PATH: list[Example] = [
    {
        "inputs": {
            "trip_request": {
                "passenger_name": "Alice", "origin": "AMS", "date": "2026-09-01",
                "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
            }
        },
        "reference_outputs": {"must_book": True, "origin": "AMS", "expect_retry": False},
        "metadata": {"category": "happy_path"},
    },
    {
        "inputs": {
            "trip_request": {
                "passenger_name": "Bob", "origin": "CDG", "date": "2026-10-15",
                "activities": ["walking"], "vibes": ["nature"], "budget_level": "high",
            }
        },
        "reference_outputs": {"must_book": True, "origin": "CDG", "expect_retry": False},
        "metadata": {"category": "happy_path"},
    },
    {
        "inputs": {
            "trip_request": {
                "passenger_name": "Priya", "origin": "DUB", "date": "2026-11-03",
                "activities": ["walking"], "vibes": ["nightlife", "food"], "budget_level": "mid",
            }
        },
        "reference_outputs": {"must_book": True, "origin": "DUB", "expect_retry": False},
        "metadata": {"category": "happy_path"},
    },
    {
        "inputs": {
            "trip_request": {
                "passenger_name": "Jonas", "origin": "MAD", "date": "2026-08-10",
                "activities": ["architecture"], "vibes": ["design"], "budget_level": "high",
            }
        },
        "reference_outputs": {"must_book": True, "origin": "MAD", "expect_retry": False},
        "metadata": {"category": "happy_path"},
    },
]

# ---------------------------------------------------------------------------
# missing_values — one or more fields omitted; defaults should apply
# ---------------------------------------------------------------------------
MISSING_VALUES: list[Example] = [
    {
        # omits origin -> should default to AMS
        "inputs": {"trip_request": {
            "passenger_name": "Sofia", "date": "2026-12-20",
            "activities": ["museums"], "vibes": ["culture"], "budget_level": "mid",
        }},
        "reference_outputs": {"must_book": True, "origin": "AMS", "expect_retry": False},
        "metadata": {"category": "missing_values", "omitted": ["origin"]},
    },
    {
        # omits budget_level -> should default to "high"
        "inputs": {"trip_request": {
            "passenger_name": "Mei", "origin": "WAW", "date": "2027-01-05",
            "activities": ["walking"], "vibes": ["relax"],
        }},
        "reference_outputs": {"must_book": True, "origin": "WAW", "expect_retry": False},
        "metadata": {"category": "missing_values", "omitted": ["budget_level"]},
    },
    {
        # omits activities and vibes -> should default to ["museums"] / ["culture", "city"]
        "inputs": {"trip_request": {
            "passenger_name": "Erik", "origin": "BER", "date": "2026-09-20", "budget_level": "mid",
        }},
        "reference_outputs": {"must_book": True, "origin": "BER", "expect_retry": False},
        "metadata": {"category": "missing_values", "omitted": ["activities", "vibes"]},
    },
    {
        # omits date -> should default to 2026-07-15
        "inputs": {"trip_request": {
            "passenger_name": "Nora", "origin": "ARN",
            "activities": ["walking"], "vibes": ["nightlife", "food"], "budget_level": "mid",
        }},
        "reference_outputs": {"must_book": True, "origin": "ARN", "expect_retry": False},
        "metadata": {"category": "missing_values", "omitted": ["date"]},
    },
    {
        # omits everything except passenger_name -> every default should apply at once
        "inputs": {"trip_request": {"passenger_name": "Tomas"}},
        "reference_outputs": {"must_book": True, "origin": "AMS", "expect_retry": False},
        "metadata": {"category": "missing_values", "omitted": ["origin", "date", "budget_level", "activities", "vibes"]},
    },
]

# ---------------------------------------------------------------------------
# no_flight — "XYZ" isn't a real IATA code in rest-flights' airport table, so
# *every* shortlisted candidate fails at find_flight identically (see module
# docstring for why this is the only reliably-constructable failure mode).
# Expect: shortlist considered, every candidate tried, no booking, clear explanation.
# ---------------------------------------------------------------------------
NO_FLIGHT: list[Example] = [
    {
        "inputs": {"trip_request": {
            "passenger_name": "Casper", "origin": "XYZ", "date": "2026-09-01",
            "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
        }},
        "reference_outputs": {"must_book": False, "expect_retry": True},
        "metadata": {"category": "no_flight"},
    },
]

# ---------------------------------------------------------------------------
# retry — see module docstring: not reliably triggerable end-to-end with the
# current backend data. Kept as a single best-effort case so the dataset
# doesn't silently skip the scenario, but reference_outputs intentionally
# does NOT assert expect_retry=True - see metadata.caveat.
# ---------------------------------------------------------------------------
RETRY: list[Example] = [
    {
        "inputs": {"trip_request": {
            "passenger_name": "Liv", "origin": "OSL", "date": "2026-10-01",
            "activities": ["museums"], "vibes": ["culture"], "budget_level": "high",
        }},
        "reference_outputs": {"must_book": True},  # only asserting the end state is sane either way
        "metadata": {
            "category": "retry",
            "caveat": "not guaranteed to actually exercise a retry - see dataset.py module docstring",
        },
    },
]

# ---------------------------------------------------------------------------
# ambiguous — unstructured free text instead of a trip_request dict
# ---------------------------------------------------------------------------
AMBIGUOUS: list[Example] = [
    {
        "inputs": {"trip_request": (
            "Hi! I'm flying out of Dublin and want somewhere with a good food scene, "
            "nothing too fancy on the budget. Sometime around November."
        )},
        "reference_outputs": {"must_book": True, "origin": "DUB", "expect_retry": False},
        "metadata": {"category": "ambiguous"},
    },
    {
        "inputs": {"trip_request": (
            "Surprise me with somewhere cultural, I don't mind spending a bit more."
        )},
        "reference_outputs": {"must_book": True, "expect_retry": False},
        "metadata": {"category": "ambiguous"},
    },
]

# ---------------------------------------------------------------------------
# thread_state — same thread_id continues a conversation; a different one
# must not inherit prior state. Handled outside the main evaluate() run (see
# run_eval.py's check_thread_state) since LangSmith's evaluate() scores
# independent examples, not ordered conversation turns - sequencing two
# dataset rows under concurrent evaluation would be racy. Kept here only as
# documentation of the two thread_ids used by that check.
# ---------------------------------------------------------------------------
THREAD_STATE_SAME_ID = "eval-thread-continuation"
THREAD_STATE_OTHER_ID = "eval-thread-isolation"

EXAMPLES: list[Example] = HAPPY_PATH + MISSING_VALUES + NO_FLIGHT + RETRY + AMBIGUOUS

DATASET_NAME = "travel-orchestrator-trip-requests"
DATASET_DESCRIPTION = (
    "Trip-planning requests covering happy-path, missing/default values, a guaranteed "
    "no-flight case, and ambiguous free text, for evaluating the travel-orchestrator "
    "LangGraph agent end-to-end. See module docstring for why a genuine single-destination "
    "retry case isn't included."
)
