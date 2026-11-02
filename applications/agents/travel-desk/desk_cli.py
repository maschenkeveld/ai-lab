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

The desk signs people in with Keycloak. `--as <user>` says who you are: the first time, the client signs in as that user
(sandbox password "demo", or DESK_PASSWORD; `./desk_cli.py login <user>` prompts for one) and caches the token in
~/.cache/travel-desk/. The server decides what you may do from your roles, so `approve --as alice` is refused: alice only
requests trips. Demo users: alice (requests), bob and carol (approve), dave (both; may not approve his own trips).

    ./desk_cli.py login bob               # sign in explicitly (prompts for a password)
    ./desk_cli.py whoami --as bob         # who the server thinks you are, and your roles

DESK_URL overrides the base URL (default http://agentgateway.lab/desk).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.getenv("DESK_URL", "http://agentgateway.lab/desk").rstrip("/")
CACHE = Path(os.getenv("DESK_TOKEN_DIR", Path.home() / ".cache" / "travel-desk"))
SANDBOX_PASSWORD = os.getenv("DESK_PASSWORD", "demo")
ACTIVE = {"user": None}  # whose token `call` sends; set from --as, or by the demo as it changes roles
_config: dict = {}


def config() -> dict:
    if not _config:
        try:
            with urllib.request.urlopen(f"{BASE}/api/config", timeout=30) as response:
                _config.update(json.load(response))
        except urllib.error.URLError as exc:
            sys.exit(f"cannot reach {BASE}: {exc.reason}")
    return _config


def _token_request(form: dict) -> dict:
    endpoint = config()["auth"]["token_endpoint"]
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.load(exc).get("error_description", "")
        except Exception:
            pass
        sys.exit(f"sign-in failed ({exc.code}): {detail or 'check the user name and password'}")


def _save(user: str, tokens: dict) -> dict:
    session = {"access": tokens["access_token"], "refresh": tokens.get("refresh_token"), "exp": time.time() + tokens["expires_in"]}
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{user}.json").write_text(json.dumps(session))
    return session


def login(user: str, password: str) -> dict:
    return _save(user, _token_request({"grant_type": "password", "client_id": "travel-desk-cli", "username": user, "password": password}))


def access_token(user: str) -> str:
    """A valid access token for `user`: from the cache, refreshed if it is about to expire, else a fresh sign-in."""
    path = CACHE / f"{user}.json"
    session = json.loads(path.read_text()) if path.exists() else None
    if session and session["exp"] - 30 > time.time():
        return session["access"]
    if session and session.get("refresh"):
        try:
            return _save(user, _token_request({"grant_type": "refresh_token", "client_id": "travel-desk-cli", "refresh_token": session["refresh"]}))["access"]
        except SystemExit:
            pass
    return login(user, SANDBOX_PASSWORD)["access"]


def call(method: str, path: str, body: dict | None = None, user: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    who = user or ACTIVE["user"]
    if config()["auth"]["enabled"]:
        if not who:
            sys.exit("the desk needs you to sign in: add --as <user> (alice, bob, carol, dave) or run ./desk_cli.py login <user>")
        headers["Authorization"] = f"Bearer {access_token(who)}"
    request = urllib.request.Request(f"{BASE}/api/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", "")
        except Exception:
            detail = ""
        if exc.code == 401 and who:
            (CACHE / f"{who}.json").unlink(missing_ok=True)
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


def wait_for(trip_id: str, *statuses: str, timeout: float = 240, quiet: bool = False, user: str | None = None) -> dict:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        snapshot = call("GET", f"trips/{trip_id}", user=user)
        status = snapshot["trip"]["status"]
        if status != last and not quiet:
            print(f"  … {status}")
            last = status
        if status in statuses:
            return snapshot
        time.sleep(1.5)
    sys.exit(f"timed out waiting for {statuses}; the trip is '{last}'")


def pending_for(trip_id: str, timeout: float = 120, user: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = [a for a in call("GET", "approvals?status=pending", user=user) if a["trip_id"] == trip_id]
        if rows:
            return rows[0]
        time.sleep(1.5)
    sys.exit("no approval appeared")


def demo() -> None:
    step = lambda text: print(f"\n=== {text}")  # noqa: E731
    step("1. alice, a requester, asks for a trip in plain language")
    request = "A culture weekend from Amsterdam around 18 December, mid budget, a day or two either way, museums please."
    trip = call("POST", "trips", {"request": request}, user="alice")
    print(f"  trip {trip['id']} requested by {trip['requested_by']}: “{request}”")

    step("2. The coordinator researches over A2A, holds a flight, and waits for a human (or policy)")
    snapshot = wait_for(trip["id"], "awaiting_approval", "booked", "completed", "failed", user="alice")
    if snapshot["trip"]["status"] == "failed":
        print_trip(snapshot)
        sys.exit("the trip failed")
    if snapshot["trip"]["status"] == "awaiting_approval":
        approval = pending_for(trip["id"], user="bob")
        print(f"  needs {approval['required']} approval(s): {approval['summary']}")
        step("3. alice tries to approve her own booking, then the approvers decide")
        refused = call_quiet("POST", f"approvals/{approval['id']}/decision", {"decision": "approve"}, user="alice")
        print(f"  alice: refused ({refused})")
        for who in ("bob", "carol")[: approval["required"]]:
            call("POST", f"approvals/{approval['id']}/decision", {"decision": "approve", "comment": "scripted demo"}, user=who)
            print(f"  {who}: approved")
    else:
        step("3. The price was under the auto-approve line: booked without a human")
    snapshot = wait_for(trip["id"], "booked", "failed", user="alice")
    print(f"  booked {snapshot['trip']['booking']['booking_code']} at {money(snapshot['trip']['price'])}")

    step("4. The fare drops 25%; the fare-watcher notices, a model judges it, the coordinator asks again")
    call("POST", f"demo/fare-drop/{trip['id']}", {"pct": -25}, user="bob")
    print("  (the watcher checks every 30s; waiting)")
    approval = pending_for(trip["id"], timeout=180, user="bob")
    print(f"  {approval['kind']}: {approval['summary']}")
    for who in ("bob", "carol")[: approval["required"]]:
        call("POST", f"approvals/{approval['id']}/decision", {"decision": "approve", "comment": "take the lower fare"}, user=who)
    wait_for(trip["id"], "rebooking", "booked", quiet=True, user="alice")
    time.sleep(3)
    step("5. The trip, with its full audit trail (every line names the person, from their token)")
    print_trip(call("GET", f"trips/{trip['id']}", user="alice"))
    call("POST", f"trips/{trip['id']}/cancel", {}, user="alice")
    print("\n(demo trip cancelled to stop its fare watch)")


def call_quiet(method: str, path: str, body: dict | None = None, user: str | None = None) -> str:
    """Make a call that is expected to be refused and return the reason, instead of exiting."""
    try:
        call(method, path, body, user=user)
    except SystemExit as exc:
        return str(exc)
    return "NOT refused"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["login", "whoami", "new", "trips", "show", "inbox", "approve", "reject", "edit", "cancel", "drop", "demo"])
    parser.add_argument("target", nargs="?", help="request text, trip id, or approval id")
    parser.add_argument("--as", dest="who", default=os.getenv("DESK_USER") or None, help="sign in as this user (alice, bob, carol, dave)")
    parser.add_argument("--comment", default="")
    parser.add_argument("--destination")
    parser.add_argument("--date")
    parser.add_argument("--pct", type=float, default=-25)
    args = parser.parse_args()
    needs_target = {"login", "new", "show", "approve", "reject", "edit", "cancel", "drop"}
    if args.command in needs_target and not args.target:
        parser.error(f"{args.command} needs an argument")

    ACTIVE["user"] = args.who
    if args.command == "login":
        password = os.getenv("DESK_PASSWORD") or getpass.getpass(f"password for {args.target}: ")
        login(args.target, password)
        print(f"signed in as {args.target}; use it with --as {args.target} (or DESK_USER={args.target})")
    elif args.command == "whoami":
        me = call("GET", "me")
        print(f"{me['username'] or '(authentication is off)'}  roles: {', '.join(me['roles']) or '-'}  approver: {me['is_approver']}")
    elif args.command == "new":
        trip = call("POST", "trips", {"request": args.target})
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
        out = call("POST", f"approvals/{args.target}/decision", {"decision": args.command, "comment": args.comment})
        print(f"{out['id']} is now {out['status']}")
    elif args.command == "edit":
        edit = {k: v for k, v in (("destination", args.destination), ("date", args.date)) if v}
        if not edit:
            parser.error("edit needs --destination and/or --date")
        out = call("POST", f"approvals/{args.target}/decision", {"decision": "edit", "comment": args.comment, "edit": edit})
        print(f"{out['id']} is now {out['status']}; a new proposal is being researched")
    elif args.command == "cancel":
        print(call("POST", f"trips/{args.target}/cancel", {})["status"])
    elif args.command == "drop":
        print(call("POST", f"demo/fare-drop/{args.target}", {"pct": args.pct}))
    elif args.command == "demo":
        demo()


if __name__ == "__main__":
    main()
