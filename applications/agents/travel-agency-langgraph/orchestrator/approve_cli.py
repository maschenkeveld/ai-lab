#!/usr/bin/env python3
"""Human-in-the-loop approval CLI for the travel-orchestrator agent.

Polls the orchestrator's /pending endpoint for bookings awaiting a human decision (IMPROVEMENTS.md's
human-in-the-loop requirement: book_flight pauses here until a human approves or rejects it),
shows each one's details, and prompts for a decision — then calls /approve to resume that run.

Usage:
    uv run python approve_cli.py
    ORCHESTRATOR_URL=http://agentgateway.lab uv run python approve_cli.py   # local kind lab
    uv run python approve_cli.py --once   # check once and exit instead of polling
"""

import argparse
import os
import sys
import time

import httpx

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://agentgateway.shared.pve-home.schenkeveld.io")
POLL_INTERVAL_SECONDS = 5

_seen: set[str] = set()


def fetch_pending() -> dict[str, dict]:
    response = httpx.get(f"{ORCHESTRATOR_URL}/a2a/travel-orchestrator/pending", timeout=10.0)
    response.raise_for_status()
    return response.json()["pending"]


def describe(thread_id: str, booking: dict) -> str:
    return (
        f"\nPending booking — thread {thread_id}\n"
        f"  Passenger: {booking.get('passenger_name')}\n"
        f"  Route:     {booking.get('from')} -> {booking.get('to')}\n"
        f"  Date:      {booking.get('date')}\n"
    )


def prompt_decision(thread_id: str) -> bool:
    while True:
        choice = input(f"Approve booking for thread {thread_id}? [y/n]: ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Please answer y or n.")


def submit_decision(thread_id: str, approved: bool) -> dict:
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/a2a/travel-orchestrator/approve",
        json={"thread_id": thread_id, "approved": approved},
        timeout=90.0,
    )
    response.raise_for_status()
    return response.json()


def handle_pending(pending: dict[str, dict]) -> None:
    for thread_id, booking in pending.items():
        if thread_id in _seen:
            continue
        _seen.add(thread_id)
        print(describe(thread_id, booking))
        approved = prompt_decision(thread_id)
        result = submit_decision(thread_id, approved)
        if approved:
            print(f"-> Approved. Status: {result.get('status')}, booking: {result.get('booking')}\n")
        else:
            print(f"-> Rejected. Status: {result.get('status')}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="Check for pending approvals once and exit, instead of polling.")
    args = parser.parse_args()

    print(f"Watching {ORCHESTRATOR_URL} for bookings awaiting approval...")
    try:
        while True:
            try:
                pending = fetch_pending()
            except httpx.HTTPError as e:
                print(f"Could not reach orchestrator: {e}", file=sys.stderr)
                pending = {}
            handle_pending(pending)
            if args.once:
                if not pending:
                    print("No pending approvals.")
                break
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
