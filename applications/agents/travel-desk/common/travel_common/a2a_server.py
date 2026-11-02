"""Serve a Pydantic AI agent over A2A (FastA2A), mounted under agentgateway's route prefix.

FastA2A's Pydantic AI bridge rejects structured (`data`) request parts, and agents here exchange typed
payloads. `DataAwareWorker` hands a `data` part to the agent as JSON text, so a caller can send a
Pydantic model and the agent sees it as its prompt. Results come back as a `data` artifact
(`{"result": {...}}`) whenever the agent has a structured `output_type`.
"""
import asyncio
import contextvars
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial

from fasta2a import FastA2A
from fasta2a.broker import InMemoryBroker
from fasta2a.pydantic_ai import AgentWorker, worker_lifespan
from fasta2a.schema import Message, Part, Skill
from fasta2a.storage import InMemoryStorage
from pydantic_ai import Agent, UserPromptPart
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

log = logging.getLogger(__name__)

# The structured request (the `data` part) of the task currently being run. Agent tools read it with
# `current_request()` instead of taking big payloads as LLM-copied arguments (a shortlist, say).
_request: contextvars.ContextVar[dict] = contextvars.ContextVar("a2a_request")


def current_request() -> dict:
    """The `data` payload of the A2A request being handled; only valid inside a running task."""
    return _request.get()


class DataAwareWorker(AgentWorker):
    async def run_task(self, params):
        task = await self.storage.load_task(params["id"])
        data: dict = {}
        for message in (task or {}).get("history", []):
            for part in message.get("parts", []):
                if "data" in part:
                    data = part["data"]
        _request.set(data)
        try:
            await super().run_task(params)
        except Exception as exc:
            # The bridge marks the task failed and re-raises; keep the cause with the task so the caller can see it.
            note = Message(role="agent", parts=[Part(text=f"{type(exc).__name__}: {exc}")],
                           message_id=str(uuid.uuid4()))
            await self.storage.update_task(params["id"], state="failed", new_messages=[note])
            raise

    def _request_parts_from_a2a(self, parts):
        converted = []
        for part in parts:
            if "data" in part:
                converted.append(UserPromptPart(content=json.dumps(part["data"])))
            else:
                converted.extend(super()._request_parts_from_a2a([part]))
        return converted


def create_a2a_app(
    agent: Agent,
    *,
    name: str,
    description: str,
    prefix: str,
    public_url: str,
    skills: list[Skill],
    version: str = "0.3.0",
    startup_attempts: int = 30,
    startup_delay: float = 2.0,
) -> Starlette:
    """An ASGI app serving `agent` at `<prefix>/` (JSON-RPC) and `<prefix>/.well-known/agent-card.json`.

    `prefix` is the path agentgateway forwards unchanged (e.g. "/a2a/flight-finder"); `public_url` is the
    externally visible URL advertised in the agent card. `GET /health` is for Kubernetes probes.

    Starting the agent connects its MCP toolsets (through agentgateway). If the gateway isn't reachable yet,
    startup is retried (`startup_attempts` x `startup_delay`s) instead of crashing the pod.
    """
    storage = InMemoryStorage()
    broker = InMemoryBroker()
    worker = DataAwareWorker(agent=agent, broker=broker, storage=storage)
    a2a = FastA2A(
        storage=storage,
        broker=broker,
        name=name,
        url=public_url,
        version=version,
        description=description,
        skills=skills,
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        lifespan=partial(worker_lifespan, worker=worker, agent=agent),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # Starlette doesn't run a mounted app's lifespan; this starts the worker that executes tasks.
        async with AsyncExitStack() as stack:
            for attempt in range(1, startup_attempts + 1):
                try:
                    await stack.enter_async_context(a2a.router.lifespan_context(a2a))
                    break
                except Exception as exc:
                    if attempt == startup_attempts:
                        raise
                    log.warning("startup attempt %d/%d failed (%s: %s); retrying in %.0fs",
                                attempt, startup_attempts, type(exc).__name__, exc, startup_delay)
                    await asyncio.sleep(startup_delay)
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": name})

    return Starlette(routes=[Route("/health", health), Mount(prefix, app=a2a)], lifespan=lifespan)
