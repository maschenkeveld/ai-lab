"""scout: an agent served over A2A, as its own process.

A2A (Agent2Agent) is the open protocol for agents calling other agents. A server publishes an *agent card* (who it is
and what it can do) and accepts tasks over JSON-RPC. This file turns a Pydantic AI agent into such a server with FastA2A:

    task serve                                   # terminal 1: the scout, on http://127.0.0.1:4500
    curl http://127.0.0.1:4500/.well-known/agent-card.json | jq .

The scout answers questions about a few cities. The facts come from a tool, not from the model.
"""
from __future__ import annotations

import os
from functools import partial

import uvicorn
from common import build_model, configure_telemetry
from fasta2a import FastA2A
from fasta2a.broker import InMemoryBroker
from fasta2a.pydantic_ai import AgentWorker, worker_lifespan
from fasta2a.schema import Skill
from fasta2a.storage import InMemoryStorage
from pydantic_ai import Agent

PORT = int(os.getenv("SCOUT_PORT", "4500"))
PUBLIC_URL = os.getenv("SCOUT_URL", f"http://127.0.0.1:{PORT}")

FACTS = {
    "vienna": "Vienna: coffee houses, the Kunsthistorisches Museum, opera. Best in winter for Christmas markets. About 2 h by plane from Amsterdam.",
    "lisbon": "Lisbon: tiled streets, trams, the Alfama district, pastel de nata. Mild winters. About 3 h by plane from Amsterdam.",
    "brussels": "Brussels: Grand-Place, comics museums, chocolate, waffles. About 1 h by plane (or 2 h by train) from Amsterdam.",
}


def build_scout(model=None) -> Agent:
    scout = Agent(
        model or build_model(),
        name="scout",
        instructions=("You are a destination scout. Answer the question in two or three sentences, using ONLY what the "
                      "facts_about tool returns. If the tool has nothing, say you do not have information on that city."),
    )

    @scout.tool_plain
    def facts_about(city: str) -> str:
        """Known facts about a city, or an empty string if there are none."""
        return FACTS.get(city.strip().lower(), "")

    return scout


def build_app(scout: Agent) -> FastA2A:
    storage, broker = InMemoryStorage(), InMemoryBroker()
    worker = AgentWorker(agent=scout, broker=broker, storage=storage)
    return FastA2A(
        storage=storage, broker=broker, name="scout", url=PUBLIC_URL, version="0.1.0",
        description="Answers questions about a few European cities, from a tool's facts.",
        skills=[Skill(id="city-facts", name="City facts", description="What a city is like and how far it is from Amsterdam.",
                      tags=["travel"], input_modes=["text/plain"], output_modes=["text/plain"])],
        lifespan=partial(worker_lifespan, worker=worker, agent=scout),
    )


if __name__ == "__main__":
    configure_telemetry("example-a2a-scout")
    uvicorn.run(build_app(build_scout()), host="127.0.0.1", port=PORT)
