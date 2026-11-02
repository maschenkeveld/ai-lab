"""fare-watcher: a long-running agent that watches booked fares for days and tells the coordinator about drops.

A2A in:  WatchRequest {"action": "watch", "trip_id", "origin", "to_iata", "date", "booked_price", "callback_url", ...}
                      {"action": "unwatch", "trip_id"}
A2A out: WatchState   {"watch_id", "status": "watching"|"stopped"|"completed", ...}

The watches live in Postgres and run as DBOS workflows, so a restart resumes every watch where it was. Quotes come
from the travel-search MCP server through agentgateway; a significant drop is judged by a model and sent to the
coordinator over A2A (the coordinator then asks a human before rebooking anything).
"""
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from travel_common.otel import setup_otel

setup_otel("fare-watcher")

import uvicorn
from dbos import DBOS
from fasta2a.schema import Skill
from pydantic_ai import Agent
from travel_common.a2a_client import call_agent
from travel_common.contracts import FareAssessment, FareEvent
from travel_common.function_worker import create_function_app
from travel_common.mcp import mcp_toolset, unwrap
from travel_common.model import build_model

from . import watcher

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "fare-watcher")
PREFIX = "/a2a/fare-watcher"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/fare_watcher")

travel_search = mcp_toolset("mcp-travel-search", tool_error_behavior="error")

assessor = Agent(build_model(), name="fare-assessor", output_type=FareAssessment, model_settings={"temperature": 0},
                 instructions="A traveller has a confirmed flight booking and its fare has dropped. Decide whether rebooking "
                              "(book the new fare, cancel the old booking) is worth asking the traveller about. Rebooking costs "
                              "nothing but their attention, so recommend it when the saving is meaningful for the price and "
                              "there is time before departure. Reason in one or two sentences using only the numbers given.")


async def quote(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        async with travel_search:
            raw = unwrap(await travel_search.direct_call_tool(
                "flight_price", {"from_iata": row["origin"], "to_iata": row["to_iata"], "date": row["date"]}))
    except Exception as exc:
        log.warning("quote failed for %s-%s: %s", row["origin"], row["to_iata"], exc)
        return None
    return {"price": float(raw["price"]), "priced_at": raw.get("priced_at", "")}


async def assess(facts: dict[str, Any]) -> FareAssessment:
    try:
        return (await assessor.run(str(facts))).output
    except Exception as exc:  # no model, no problem: a drop that passed the code rule is worth showing a human
        log.warning("assessment model failed (%s: %s); recommending by the rule", type(exc).__name__, exc)
        saving = facts["old_price"] - facts["new_price"]
        return FareAssessment(worth_rebooking=True, reasoning=f"The fare fell {facts['drop_pct']}% ({saving:.2f} {facts['currency']}).")


async def notify(callback_url: str, event: FareEvent) -> None:
    await call_agent(callback_url, event.model_dump(mode="json"), timeout=30)


@asynccontextmanager
async def runtime() -> AsyncIterator[None]:
    pool = await watcher.connect(DATABASE_URL)
    watcher.configure(watcher.Deps(pool=pool, quote=quote, assess=assess, notify=notify))
    DBOS(config={"name": "fare-watcher", "system_database_url": DATABASE_URL, "log_level": "INFO"})
    DBOS.launch()
    try:
        yield
    finally:
        DBOS.destroy()
        await pool.close()


app = create_function_app(
    watcher.handle, name=AGENT_NAME, prefix=PREFIX, public_url=PUBLIC_URL, lifespan=runtime,
    description="Watches a booked fare for days (a durable workflow per booking) and tells the coordinator about meaningful drops.",
    skills=[Skill(id="watch-fare", name="Watch fare", description="Watch a booked route and report meaningful fare drops.",
                  tags=["travel", "fares", "long-running", "durable"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4304")))
