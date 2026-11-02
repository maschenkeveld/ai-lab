"""durable_approval: a workflow that waits for a human, and survives being killed while it waits.

This is `human_approval` made restart-proof, with DBOS. The trip is a *workflow*: plan (ask a model), hold a flight, wait
for approval, then confirm or release the hold. Each effect is a *step*, and DBOS checkpoints every step's result in a
database (SQLite here, Postgres in the travel desk). Waiting for the human is a durable `recv`. If the process dies while it
waits, the next start resumes the same workflow: the steps that already ran are not run again, and the wait continues.

Try it in three terminals (or one, killing it with Ctrl-C):

    task run                         # plans, holds a flight, then waits for approval ... Ctrl-C it now
    task resume                      # restarts: it does NOT call the model or hold again, it just keeps waiting
    task approve -- <workflow id>    # (any terminal) approve it; `-- <id> --deny "reason"` to deny
    task status -- <workflow id>     # what happened, step by step

    task test                        # offline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from common import build_model, configure_telemetry, shutdown_telemetry
from dbos import DBOS, DBOSClient, SetWorkflowID
from pydantic import BaseModel
from pydantic_ai import Agent

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("DURABLE_DIR", HERE))
DB_URL = f"sqlite:///{STATE_DIR / 'durable.sqlite'}"
HOLDS = STATE_DIR / "holds.jsonl"  # the stand-in booking system: an append-only record of what was done
DESTINATIONS = {"Vienna": 118.41, "Lisbon": 232.86, "Brussels": 29.24}


class Plan(BaseModel):
    destination: str
    reason: str


planner = Agent(
    output_type=Plan,
    instructions=f"Pick one destination for the traveller from {list(DESTINATIONS)} and give a one-sentence reason.",
)


def planning_model():
    """The model the plan step uses. A plain function (not `planner.override`) so tests can swap it: DBOS runs steps in
    their own threads, and Pydantic AI's `override` does not cross threads."""
    return build_model()


def approval_ttl() -> float:
    return float(os.getenv("APPROVAL_TTL_SECONDS", "600"))


def booking_event(kind: str, **detail) -> None:
    with HOLDS.open("a") as f:
        f.write(json.dumps({"kind": kind, **detail}) + "\n")


# ---- steps: each runs once per workflow, even across crashes and restarts ---------------------------------------


@DBOS.step()
def plan_trip(request: str) -> dict:
    print("  [step plan]    asking the model ...", flush=True)
    return planner.run_sync(request, model=planning_model()).output.model_dump()


@DBOS.step()
def hold_flight(destination: str) -> dict:
    code = f"HOLD-{uuid.uuid4().hex[:6].upper()}"
    print(f"  [step hold]    reserving a flight to {destination}: {code}", flush=True)
    booking_event("hold", code=code, destination=destination, price=DESTINATIONS[destination])
    return {"code": code, "price": DESTINATIONS[destination]}


@DBOS.step()
def confirm_flight(code: str) -> None:
    print(f"  [step confirm] confirming {code}", flush=True)
    booking_event("confirm", code=code)


@DBOS.step()
def release_flight(code: str, why: str) -> None:
    print(f"  [step release] releasing {code} ({why})", flush=True)
    booking_event("release", code=code, why=why)


# ---- the workflow ------------------------------------------------------------------------------------------------


@DBOS.workflow()
def trip_workflow(request: str) -> dict:
    plan = plan_trip(request)
    hold = hold_flight(plan["destination"])
    DBOS.set_event("status", "awaiting_approval")
    print(f"  [workflow]     {plan['destination']} for {hold['price']} EUR is held as {hold['code']}; waiting for approval", flush=True)

    decision = DBOS.recv("approval", timeout_seconds=approval_ttl())  # durable: survives a restart
    if decision is None:
        release_flight(hold["code"], "nobody decided in time")
        DBOS.set_event("status", "expired")
        return {"outcome": "expired", "code": hold["code"]}
    if decision == "approve":
        confirm_flight(hold["code"])
        DBOS.set_event("status", "booked")
        return {"outcome": "booked", "code": hold["code"], "destination": plan["destination"]}
    release_flight(hold["code"], decision)
    DBOS.set_event("status", "denied")
    return {"outcome": "denied", "code": hold["code"], "reason": decision}


# ---- the command line --------------------------------------------------------------------------------------------


def launch() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DBOS(config={"name": "durable-approval", "system_database_url": DB_URL, "log_level": "WARNING"})
    DBOS.launch()  # this is also what resumes any workflow that was interrupted


def cmd_run(args) -> None:
    launch()
    configure_telemetry("example-durable-approval")
    wid = args.id or f"trip-{uuid.uuid4().hex[:6]}"
    print(f"Workflow {wid}. Approve it from another terminal:  task approve -- {wid}")
    with SetWorkflowID(wid):
        handle = DBOS.start_workflow(trip_workflow, args.request)
    print(f"Result: {handle.get_result()}")
    shutdown_telemetry()


def cmd_resume(_) -> None:
    launch()
    client = DBOSClient(system_database_url=DB_URL)
    pending = [w.workflow_id for w in client.list_workflows(status=["PENDING", "ENQUEUED"])]
    if not pending:
        sys.exit("Nothing to resume: no workflow is waiting.")
    print(f"Resumed {pending}. The steps that already ran are not run again.")
    for wid in pending:
        print(f"Result of {wid}: {DBOS.retrieve_workflow(wid).get_result()}")


def cmd_approve(args) -> None:
    client = DBOSClient(system_database_url=DB_URL)
    client.send(args.id, f"{args.deny}" if args.deny else "approve", topic="approval")
    print(f"Sent {'a denial' if args.deny else 'approval'} to {args.id}.")


def cmd_status(args) -> None:
    client = DBOSClient(system_database_url=DB_URL)
    handle = client.retrieve_workflow(args.id)
    print(f"{args.id}: {handle.get_status().status}, trip status: {client.get_event(args.id, 'status', timeout_seconds=0)}")
    print("Steps recorded by DBOS (each result is stored, so a restart skips them):")
    for step in client.list_workflow_steps(args.id):
        print(f"  {step['function_name']}")
    if HOLDS.exists():
        print("The booking system's record:")
        print("  " + HOLDS.read_text().strip().replace("\n", "\n  "))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start a trip and wait for it")
    run.add_argument("request", nargs="?", default="A weekend in a city with great food and culture, mid budget.")
    run.add_argument("--id", help="workflow id (default: random)")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("resume", help="restart after a crash and finish what was waiting").set_defaults(fn=cmd_resume)
    approve = sub.add_parser("approve", help="approve (or deny) a waiting trip")
    approve.add_argument("id")
    approve.add_argument("--deny", metavar="REASON", help="deny with this reason instead of approving")
    approve.set_defaults(fn=cmd_approve)
    status = sub.add_parser("status", help="show a workflow's steps and the booking record")
    status.add_argument("id")
    status.set_defaults(fn=cmd_status)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
