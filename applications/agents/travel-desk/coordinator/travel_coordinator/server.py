"""trip-coordinator: the desk's durable workflow, its approval model, REST API, web UI and A2A endpoint."""
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from travel_common.otel import setup_otel

setup_otel("trip-coordinator")

import uvicorn
from dbos import DBOS
from fasta2a.schema import Skill
from fastapi import FastAPI
from travel_common.function_worker import make_function_a2a

from . import workflow
from .agents import Llm, Specialists
from .api import a2a_handler, build_app
from .auth import Authenticator
from .service import DeskService
from .settings import Agents, Policy, database_url
from .store import Store

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "trip-coordinator")
PREFIX = "/a2a/trip-coordinator"
agents = Agents()

holder: dict[str, DeskService] = {}


async def handle(data: dict) -> dict:
    return await a2a_handler(holder["service"], data)


a2a, worker = make_function_a2a(
    handle, name=AGENT_NAME, public_url=agents.self_url,
    description="Coordinates a trip end to end: researches and holds a flight, gets human approval, books it, then watches the fare.",
    skills=[Skill(id="plan-trip", name="Plan trip", description="Create a trip from a plain-language request.",
                  tags=["travel", "workflow", "human-in-the-loop"], input_modes=["application/json"], output_modes=["application/json"],
                  examples=['{"type": "create_trip", "request": "A culture weekend from Amsterdam in mid December"}'])],
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        store = await Store.connect(database_url())
        stack.push_async_callback(store.close)
        deps = workflow.Deps(store=store, specialists=Specialists(agents), llm=Llm(), policy=Policy(), agents=agents)
        workflow.configure(deps)
        holder["service"] = DeskService(deps)
        DBOS(config={"name": "coordinator", "system_database_url": database_url(), "log_level": "INFO"})
        DBOS.launch()  # also resumes every workflow that was in flight when the last pod stopped
        stack.callback(DBOS.destroy)
        await stack.enter_async_context(a2a.task_manager)
        await stack.enter_async_context(worker.run())
        yield


class _LazyService:
    """The API is built before the database exists; it reaches the real service through the holder."""

    def __getattr__(self, name):
        return getattr(holder["service"], name)


app = build_app(_LazyService(), lifespan, Authenticator.from_env())  # type: ignore[arg-type]
app.mount(PREFIX, a2a)  # last, so the explicit routes above win

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4300")))
