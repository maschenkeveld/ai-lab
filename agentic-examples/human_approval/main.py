"""human_approval: an agent that pauses before an irreversible tool until a human decides.

Booking is the one step that cannot be undone, so `book_flight` is declared `requires_approval=True`. When the model
calls it, the run does not execute it: it ends with `DeferredToolRequests` listing what is waiting. Your code decides
(here: you, in the terminal), then resumes the same run with the decisions. An approval runs the tool; a denial
(`ToolDenied`) tells the model it was refused and why, and the tool never runs.

    task run                       # you are asked before anything is booked
    task run -- --approve          # decide automatically: approve
    task run -- --deny "too expensive"   # decide automatically: deny, and watch what the model does
    task test

This example keeps the paused run in memory: if the process dies, the approval is gone. `durable_approval` is the
version that survives that. In the travel desk, approvals are rows in Postgres.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field

from common import build_model, configure_telemetry, shutdown_telemetry
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolDenied,
)
from pydantic_ai.messages import ToolCallPart

FLIGHTS = {"KA-AMSVIE": 118.41, "KA-AMSLIS": 232.86, "KA-AMSBRU": 29.24}


@dataclass
class Ledger:
    """What has actually been booked. The point of the example is that a denied booking never lands here."""
    booked: list[dict] = field(default_factory=list)


agent = Agent(
    output_type=[str, DeferredToolRequests],
    deps_type=Ledger,
    instructions=(
        "You help book flights. Use search_flights to see the options, choose the best fit for the traveller, and call "
        "book_flight for it. If a booking is denied, say so, do not retry the same flight, and offer the next best option in words."
    ),
)


@agent.tool_plain
def search_flights(destination: str) -> list[dict]:
    """Search flights from Amsterdam to a destination city. Free to call, has no side effects."""
    codes = {"vienna": "KA-AMSVIE", "lisbon": "KA-AMSLIS", "brussels": "KA-AMSBRU"}
    code = codes.get(destination.lower())
    return [{"flight_number": code, "price_eur": FLIGHTS[code]}] if code else []


@agent.tool(requires_approval=True)
def book_flight(ctx: RunContext[Ledger], flight_number: str) -> dict:
    """Book a flight. Irreversible: it needs a human's approval before it runs."""
    booking = {"flight_number": flight_number, "price_eur": FLIGHTS[flight_number], "reference": f"REF{len(ctx.deps.booked) + 1:03d}"}
    ctx.deps.booked.append(booking)
    return booking


Decide = Callable[[ToolCallPart], "bool | str"]  # True to approve, or a reason string to deny


def run_with_approvals(prompt: str, decide: Decide, *, model, ledger: Ledger | None = None):
    """Run the agent; whenever it stops for approval, ask `decide` and resume the same run. Returns (result, ledger)."""
    ledger = ledger or Ledger()
    result = agent.run_sync(prompt, model=model, deps=ledger)
    while isinstance(result.output, DeferredToolRequests):
        decisions = DeferredToolResults()
        for call in result.output.approvals:
            verdict = decide(call)
            decisions.approvals[call.tool_call_id] = True if verdict is True else ToolDenied(str(verdict))
        result = agent.run_sync(message_history=result.all_messages(), deferred_tool_results=decisions, model=model, deps=ledger)
    return result, ledger


def ask_the_human(call: ToolCallPart) -> bool | str:
    print(f"\nThe agent wants to run {call.tool_name}({call.args_as_dict()})")
    answer = input("Approve? [y/N, or type a reason to deny] ").strip()
    return True if answer.lower() in ("y", "yes") else (answer if answer and answer.lower() not in ("n", "no") else "The traveller said no.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="approve every request without asking")
    group.add_argument("--deny", metavar="REASON", help="deny every request with this reason")
    parser.add_argument("--model", help="LiteLLM alias (default: LLM_MODEL or openai-gpt)")
    parser.add_argument("request", nargs="?", default="I want a cheap trip to Brussels or Vienna. Book the best option.")
    args = parser.parse_args()

    decide: Decide = (lambda call: True) if args.approve else (lambda call: args.deny) if args.deny else ask_the_human
    configure_telemetry("example-human-approval")
    result, ledger = run_with_approvals(args.request, decide, model=build_model(args.model))
    print(f"\nAgent: {result.output}")
    print(f"\nBooked in the ledger: {ledger.booked or 'nothing'}")
    shutdown_telemetry()


if __name__ == "__main__":
    main()
