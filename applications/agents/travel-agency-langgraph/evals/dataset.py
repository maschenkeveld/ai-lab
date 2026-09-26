"""Representative trip requests for evaluating the travel-orchestrator agent.

Each example's `inputs` matches the JSON payload the orchestrator's /a2a
endpoint expects (see travel_orchestrator/graph.py's extract_requirements).
"""

EXAMPLES = [
    {
        # Amsterdam/Paris/London/... — culture+museums+high, from AMS
        "inputs": {
            "trip_request": {
                "name": "Alice", "from": "AMS", "date": "2026-09-01",
                "activity": "museums", "vibe": "culture", "budget": "high",
            }
        },
    },
    {
        # Oslo — nature+hiking+high, from CDG
        "inputs": {
            "trip_request": {
                "name": "Bob", "from": "CDG", "date": "2026-10-15",
                "activity": "hiking", "vibe": "nature", "budget": "high",
            }
        },
    },
    {
        # Barcelona — nightlife+tapas+mid, from DUB
        "inputs": {
            "trip_request": {
                "name": "Priya", "from": "DUB", "date": "2026-11-03",
                "activity": "tapas", "vibe": "nightlife", "budget": "mid",
            }
        },
    },
    {
        # Rome — culture+ancient-sites+mid, from BER
        "inputs": {
            "trip_request": {
                "name": "Sofia", "from": "BER", "date": "2026-12-20",
                "activity": "ancient-sites", "vibe": "culture", "budget": "mid",
            }
        },
    },
    {
        # Copenhagen — design+architecture+high, from MAD
        "inputs": {
            "trip_request": {
                "name": "Jonas", "from": "MAD", "date": "2026-08-10",
                "activity": "architecture", "vibe": "design", "budget": "high",
            }
        },
    },
    {
        # Prague — relax+walking+mid, from WAW
        "inputs": {
            "trip_request": {
                "name": "Mei", "from": "WAW", "date": "2027-01-05",
                "activity": "walking", "vibe": "relax", "budget": "mid",
            }
        },
    },
]

DATASET_NAME = "travel-orchestrator-trip-requests"
DATASET_DESCRIPTION = (
    "Representative trip-planning requests spanning different origins, "
    "budgets, vibes, and activities, for evaluating the travel-orchestrator "
    "LangGraph agent end-to-end."
)
