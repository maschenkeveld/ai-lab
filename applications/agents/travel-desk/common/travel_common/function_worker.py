"""Serve plain code (no LLM) as an A2A agent.

Some agents here should not involve a model at all: the booking agent moves money, the coordinator and the
fare-watcher are durable workflows. `FunctionWorker` runs an async handler for each A2A task: the request is the
`data` part of the user message, the result goes back as a `data` artifact (`{"result": {...}}`), and an
exception ends the task `failed` with the cause attached, exactly like `DataAwareWorker` does for LLM agents.
"""
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fasta2a import FastA2A
from fasta2a.broker import InMemoryBroker
from fasta2a.schema import Artifact, Message, Part, Skill, TaskIdParams, TaskSendParams
from fasta2a.storage import InMemoryStorage
from fasta2a.worker import Worker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def agent_text(text: str) -> Message:
    return Message(role="agent", parts=[Part(text=text)], message_id=str(uuid.uuid4()))


def request_data(task: Any) -> dict[str, Any]:
    """The `data` payload of the last user message of a task."""
    data: dict[str, Any] = {}
    for message in task.get("history", []):
        if message.get("role") in ("user", "ROLE_USER"):
            for part in message.get("parts", []):
                if "data" in part:
                    data = part["data"]
    return data


@dataclass
class FunctionWorker(Worker[list]):
    handler: Handler | None = None

    async def run_task(self, params: TaskSendParams) -> None:
        task = await self.storage.load_task(params["id"])
        if task is None:
            raise ValueError(f"Task {params['id']} not found")
        task_id, context_id = task["id"], task["context_id"]
        await self.storage.update_task(task_id, state="working")
        await self.publish_status(task_id, context_id, "working")
        try:
            assert self.handler is not None, "handler is not set"
            result = await self.handler(request_data(task))
        except Exception as exc:
            log.exception("task %s failed", task_id)
            await self.storage.update_task(task_id, state="failed",
                                           new_messages=[agent_text(f"{type(exc).__name__}: {exc}")])
            return
        artifact = Artifact(artifact_id=str(uuid.uuid4()), name="result", parts=[Part(data={"result": result})])
        await self.storage.update_task(task_id, state="completed", new_artifacts=[artifact])

    async def cancel_task(self, params: TaskIdParams) -> None:
        pass

    def build_message_history(self, history: list[Message]) -> list:
        return []

    def build_artifacts(self, result: Any) -> list[Artifact]:
        return []


def make_function_a2a(handler: Handler, *, name: str, description: str, public_url: str,
                      skills: list[Skill], version: str = "0.3.0") -> tuple[FastA2A, FunctionWorker]:
    """The FastA2A app and its worker. Run them with `a2a.task_manager` and `worker.run()` (see create_function_app)."""
    storage, broker = InMemoryStorage(), InMemoryBroker()
    worker = FunctionWorker(broker=broker, storage=storage, handler=handler)
    a2a = FastA2A(storage=storage, broker=broker, name=name, url=public_url, version=version,
                  description=description, skills=skills,
                  default_input_modes=["application/json"], default_output_modes=["application/json"])
    return a2a, worker


def create_function_app(
    handler: Handler,
    *,
    name: str,
    description: str,
    prefix: str,
    public_url: str,
    skills: list[Skill],
    lifespan: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> Starlette:
    """An ASGI app serving `handler` as an A2A agent at `<prefix>/`, with `/health` for Kubernetes probes.

    `lifespan` is an optional context manager entered around the worker (connect a database, launch a workflow engine).
    """
    a2a, worker = make_function_a2a(handler, name=name, description=description, public_url=public_url, skills=skills)

    @asynccontextmanager
    async def app_lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan())
            await stack.enter_async_context(a2a.task_manager)
            await stack.enter_async_context(worker.run())
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": name})

    return Starlette(routes=[Route("/health", health), Mount(prefix, app=a2a)], lifespan=app_lifespan)
