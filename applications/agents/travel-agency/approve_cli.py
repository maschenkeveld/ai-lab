#!/usr/bin/env python3
"""Drive the travel orchestrator by hand: plan a trip, list paused bookings, approve or reject one.

    ./approve_cli.py plan "Cultural city trip from Amsterdam, mid budget, museums, 2026-12-18. Book it."
    ./approve_cli.py pending
    ./approve_cli.py approve <thread_id>
    ./approve_cli.py reject <thread_id>

Talks to the orchestrator through agentgateway (ORCHESTRATOR_URL to override). Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("ORCHESTRATOR_URL", "http://agentgateway.lab/a2a/travel-orchestrator").rstrip("/")


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code} from {BASE}{path}: {exc.read().decode(errors='replace')}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {BASE}: {exc.reason}")


def show(result: dict) -> None:
    print(result.get("answer", ""))
    if pending := result.get("pending_approval"):
        print(f"\nPending approval, thread {result['thread_id']}:")
        print(json.dumps(pending, indent=2))
        print(f"\n  ./approve_cli.py approve {result['thread_id']}\n  ./approve_cli.py reject {result['thread_id']}")
    if booking := result.get("booking"):
        print(f"\nBooking {booking.get('booking_code')}: {booking.get('from')} -> {booking.get('to')} on {booking.get('date')}")


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[1] not in {"plan", "pending", "approve", "reject"}:
        sys.exit(__doc__)
    command = argv[1]
    if command == "plan":
        if len(argv) < 3:
            sys.exit('usage: approve_cli.py plan "<trip request>" [thread_id]')
        thread_id = argv[3] if len(argv) > 3 else str(uuid.uuid4())
        show(call("POST", "/plan", {"prompt": argv[2], "thread_id": thread_id}))
    elif command == "pending":
        pending = call("GET", "/pending")["pending"]
        if not pending:
            print("Nothing is waiting for approval.")
        for thread_id, details in pending.items():
            print(f"{thread_id}: {details.get('from')} -> {details.get('to')} on {details.get('date')} for {details.get('passenger_name')}")
    else:
        if len(argv) < 3:
            sys.exit(f"usage: approve_cli.py {command} <thread_id>")
        show(call("POST", "/approve", {"thread_id": argv[2], "approved": command == "approve"}))


if __name__ == "__main__":
    main(sys.argv)
