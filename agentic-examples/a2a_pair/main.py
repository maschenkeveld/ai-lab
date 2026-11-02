"""a2a_pair: a planner agent that asks another agent, over A2A, for what it needs.

The planner has one tool, `ask_scout`, and that tool is an A2A call: it sends a task to the scout process, polls it until it
finishes, and returns the scout's answer. The planner never imports the scout; all it knows is a URL. Run the scout first:

    task serve                      # terminal 1: scout.py on http://127.0.0.1:4500
    task run                        # terminal 2: this file
    task demo                       # or both in one go (starts the scout, runs the planner, stops the scout)
    task test

A2A `message/send` is asynchronous: it returns a *task* in state `submitted`; the caller polls `tasks/get` until it is
`completed` (or `failed`, `input-required`, ...). The lifecycle the planner saw is printed at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid

import httpx
from common import build_model, configure_telemetry, shutdown_telemetry
from fasta2a.client import A2AClient
from fasta2a.schema import Message, Part
from pydantic_ai import Agent

SCOUT_URL = os.getenv("SCOUT_URL", "http://127.0.0.1:4500")
FINAL = {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}
seen_states: list[list[str]] = []  # per question: the task states the planner observed, for the printout


def _task(result):
    """The server may wrap the task as {"task": {...}} (A2A 1.0) or return it bare."""
    return result["task"] if isinstance(result, dict) and "task" in result else result


def _text_of(task) -> str:
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts", []):
            if "text" in part:
                return part["text"]
    for message in reversed(task.get("history") or []):
        if message.get("role") in ("agent", "ROLE_AGENT"):
            for part in message.get("parts", []):
                if "text" in part:
                    return part["text"]
    return ""


async def ask(client: A2AClient, question: str, *, timeout: float = 60.0, poll: float = 0.3) -> str:
    """Send one question to another agent and return its answer: send, poll, read the result."""
    sent = await client.send_message(Message(role="user", parts=[Part(text=question)], message_id=str(uuid.uuid4())))
    task = _task(sent["result"])
    deadline = time.monotonic() + timeout
    states = [task["status"]["state"]]
    seen_states.append(states)
    while task["status"]["state"] not in FINAL:
        if time.monotonic() > deadline:
            raise TimeoutError(f"the scout did not finish within {timeout}s (state: {task['status']['state']})")
        await asyncio.sleep(poll)
        task = _task((await client.get_task(task["id"]))["result"])
        if states[-1] != task["status"]["state"]:
            states.append(task["status"]["state"])
    if task["status"]["state"] != "completed":
        raise RuntimeError(f"the scout ended in state '{task['status']['state']}': {_text_of(task)}")
    return _text_of(task)


planner = Agent(
    name="planner",
    instructions=("You plan a short city trip from Amsterdam. Use ask_scout to learn about candidate cities (ask one question per "
                  "city), then recommend one in two sentences, and name what the scout told you. Do not invent facts."),
)


@planner.tool_plain
async def ask_scout(question: str) -> str:
    """Ask the destination scout agent (a separate process, reached over A2A) a question about a city."""
    async with httpx.AsyncClient(timeout=60) as http:
        return await ask(A2AClient(SCOUT_URL, http_client=http), question)


async def show_agent_card() -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        card = (await http.get(f"{SCOUT_URL}/.well-known/agent-card.json")).json()
    print(f"Scout's agent card: {card['name']}: {card['description']}")
    print(f"  skills: {[s['name'] for s in card.get('skills', [])]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llm", choices=["openai", "gemini", "ollama", "local"], help="which model, by gateway route (default: openai)")
    parser.add_argument("request", nargs="?", default="Should I go to Vienna, Lisbon or Brussels for a winter weekend?")
    args = parser.parse_args()

    configure_telemetry("example-a2a-planner")
    try:
        asyncio.run(show_agent_card())
    except httpx.HTTPError:
        raise SystemExit(f"No scout at {SCOUT_URL}. Start it first: task serve (in another terminal), or use task demo.")
    result = planner.run_sync(args.request, model=build_model(args.llm))
    print(f"\nPlanner: {result.output}")
    print("\nThe task each question became, and the states the planner saw while polling:")
    for i, states in enumerate(seen_states, 1):
        print(f"  question {i}: {' -> '.join(states)}")
    shutdown_telemetry()


if __name__ == "__main__":
    main()
