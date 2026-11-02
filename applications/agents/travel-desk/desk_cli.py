#!/usr/bin/env python3
"""Drive the travel desk from a terminal. Standard library only; talks to the same REST API as the web app.

    ./desk_cli.py new "A culture weekend from Amsterdam around 18 December, mid budget"
    ./desk_cli.py trips
    ./desk_cli.py show <trip_id>              # proposal, approvals, audit trail
    ./desk_cli.py inbox                       # everything waiting for a human
    ./desk_cli.py approve <approval_id> --as bob [--comment "..."]
    ./desk_cli.py reject  <approval_id> --as bob --comment "too expensive"
    ./desk_cli.py edit    <approval_id> --as bob --destination Madrid [--date 2026-12-20]
    ./desk_cli.py cancel  <trip_id> --as bob
    ./desk_cli.py drop    <trip_id> [--pct -25]  # demo: push the booked fare down so the fare-watcher notices
    ./desk_cli.py demo                        # a scripted end-to-end run: plan, approve, fare drop, rebook

DESK_URL overrides the base URL (default http://agentgateway.lab/desk).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("DESK_URL", "http://agentgateway.lab/desk").rstrip("/")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}/api/{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", "")
        except Exception:
            detail = ""
        sys.exit(f"{exc.code} {method} {path}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {BASE}: {exc.reason}")


def money(value, currency="EUR") -> str:
    return "-" if value is None else f"{value:.2f} {currency}"


def print_trip(snapshot: dict) -> None:
    trip, approvals, audit = snapshot["trip"], snapshot["approvals"], snapshot["audit"]
    p = trip.get("proposal") or {}
    print(f"{trip['id']}  [{trip['status']}]  requested by {trip['requested_by']}")
    print(f"  “{trip['request_text']}”")
    if trip["summary"]:
        print(f"  {trip['summary']}")
    if p:
        print(f"  {p['origin']} -> {p['to_iata']} ({p['destination']['name']}) on {p['flight']['date']}, {p['flight']['flight_number']}, "
              f"{money(trip['price'], trip['currency'])}" + (f"  booking {trip['booking']['booking_code']} ({trip['booking']['status']})" if trip.get("booking") else ""))
        if p.get("alternatives"):
            print("  alternatives: " + ", ".join(f"{a['destination']} {money(a['price'])}" for a in p["alternatives"]))
    for a in approvals:
        got = sum(1 for d in a["decisions"] if d["decision"] == "approve")
        print(f"  approval {a['id']} [{a['status']}] {a['kind']}: {got}/{a['required']} approvals" + ("  ESCALATED" if a["escalated"] and a["status"] == "pending" else ""))
    print("  audit:")
    for e in audit:
        detail = " ".join(f"{k}={v}" for k, v in e["detail"].items() if k not in ("request", "requirements"))
        print(f"    {e['ts'][11:19]}  {e['actor']:<14} {e['type']:<22} {detail}")


def wait_for(trip_id: str, *statuses: str, timeout: float = 240, quiet: bool = False) -> dict:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        snapshot = call("GET", f"trips/{trip_id}")
        status = snapshot["trip"]["status"]
        if status != last and not quiet:
            print(f"  … {status}")
            last = status
        if status in statuses:
            return snapshot
        time.sleep(1.5)
    sys.exit(f"timed out waiting for {statuses}; the trip is '{last}'")


def pending_for(trip_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [a for a in call("GET", "approvals?status=pending") if a["trip_id"] == trip_id]
        if rows:
            return rows[0]
        time.sleep(1.5)
    sys.exit("no approval appeared")


def demo() -> None:
    step = lambda text: print(f"\n=== {text}")  # noqa: E731
    step("1. A traveller asks for a trip in plain language")
    request = "A culture weekend from Amsterdam around 18 December, mid budget, a day or two either way, museums please."
    trip = call("POST", "trips", {"request": request, "requested_by": "alice"})
    print(f"  trip {trip['id']}: “{request}”")

    step("2. The coordinator researches over A2A, holds a flight, and waits for a human (or policy)")
    snapshot = wait_for(trip["id"], "awaiting_approval", "booked", "completed", "failed")
    if snapshot["trip"]["status"] == "failed":
        print_trip(snapshot)
        sys.exit("the trip failed")
    if snapshot["trip"]["status"] == "awaiting_approval":
        approval = pending_for(trip["id"])
        print(f"  needs {approval['required']} approval(s): {approval['summary']}")
        for who in ("bob", "carol")[: approval["required"]]:
            step(f"3. {who} approves")
            call("POST", f"approvals/{approval['id']}/decision", {"approver": who, "decision": "approve", "comment": "scripted demo"})
    else:
        step("3. The price was under the auto-approve line: booked without a human")
    snapshot = wait_for(trip["id"], "booked", "failed")
    print(f"  booked {snapshot['trip']['booking']['booking_code']} at {money(snapshot['trip']['price'])}")

    step("4. The fare drops 25%; the fare-watcher notices, a model judges it, the coordinator asks again")
    call("POST", f"demo/fare-drop/{trip['id']}", {"pct": -25})
    print("  (the watcher checks every 30s; waiting)")
    approval = pending_for(trip["id"], timeout=180)
    print(f"  {approval['kind']}: {approval['summary']}")
    for who in ("bob", "carol")[: approval["required"]]:
        call("POST", f"approvals/{approval['id']}/decision", {"approver": who, "decision": "approve", "comment": "take the lower fare"})
    wait_for(trip["id"], "rebooking", "booked", quiet=True)
    time.sleep(3)
    step("5. The trip, with its full audit trail")
    print_trip(call("GET", f"trips/{trip['id']}"))
    call("POST", f"trips/{trip['id']}/cancel", {"actor": "demo"})
    print("\n(demo trip cancelled to stop its fare watch)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["new", "trips", "show", "inbox", "approve", "reject", "edit", "cancel", "drop", "demo"])
    parser.add_argument("target", nargs="?", help="request text, trip id, or approval id")
    parser.add_argument("--as", dest="who", default=os.getenv("USER", "anonymous"))
    parser.add_argument("--comment", default="")
    parser.add_argument("--destination")
    parser.add_argument("--date")
    parser.add_argument("--pct", type=float, default=-25)
    args = parser.parse_args()
    needs_target = {"new", "show", "approve", "reject", "edit", "cancel", "drop"}
    if args.command in needs_target and not args.target:
        parser.error(f"{args.command} needs an argument")

    if args.command == "new":
        trip = call("POST", "trips", {"request": args.target, "requested_by": args.who})
        print(f"{trip['id']} created; follow it with: ./desk_cli.py show {trip['id']}")
    elif args.command == "trips":
        for t in call("GET", "trips"):
            print(f"{t['id']}  {t['status']:<18} {(t.get('proposal') or {}).get('destination', {}).get('name', '-'):<12} {t['request_text'][:60]}")
    elif args.command == "show":
        print_trip(call("GET", f"trips/{args.target}"))
    elif args.command == "inbox":
        rows = call("GET", "approvals?status=pending")
        if not rows:
            print("Nothing is waiting for approval.")
        for a in rows:
            got = sum(1 for d in a["decisions"] if d["decision"] == "approve")
            print(f"{a['id']}  trip {a['trip_id']}  {a['kind']}  {got}/{a['required']}  {money(a['proposal']['price'])}"
                  + ("  ESCALATED" if a["escalated"] else "") + f"\n    {a['summary']}")
    elif args.command in ("approve", "reject"):
        out = call("POST", f"approvals/{args.target}/decision", {"approver": args.who, "decision": args.command, "comment": args.comment})
        print(f"{out['id']} is now {out['status']}")
    elif args.command == "edit":
        edit = {k: v for k, v in (("destination", args.destination), ("date", args.date)) if v}
        if not edit:
            parser.error("edit needs --destination and/or --date")
        out = call("POST", f"approvals/{args.target}/decision", {"approver": args.who, "decision": "edit", "comment": args.comment, "edit": edit})
        print(f"{out['id']} is now {out['status']}; a new proposal is being researched")
    elif args.command == "cancel":
        print(call("POST", f"trips/{args.target}/cancel", {"actor": args.who})["status"])
    elif args.command == "drop":
        print(call("POST", f"demo/fare-drop/{args.target}", {"pct": args.pct}))
    elif args.command == "demo":
        demo()


if __name__ == "__main__":
    main()
