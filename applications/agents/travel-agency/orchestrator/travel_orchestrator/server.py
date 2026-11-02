"""Orchestrator service: the trip-planning HTTP API plus its A2A endpoint.

    POST /plan, /plan/stream      plan (and, once a human approves, book) a trip
    GET  /pending                 bookings waiting for a human decision
    POST /approve                 approve or reject one
    /a2a/travel-orchestrator/     the same orchestrator as an A2A agent (agent card, message/send, tasks/get)

Every path also works under `/a2a/travel-orchestrator/...`, which is the prefix agentgateway forwards.
"""
import os

from travel_common.otel import setup_otel

setup_otel("travel-orchestrator")

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fasta2a import FastA2A
from fasta2a.broker import InMemoryBroker
from fasta2a.schema import Skill
from fasta2a.storage import InMemoryStorage
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from .planner import Specialists
from .service import AwaitingApproval, NotAwaitingApproval, TripService
from .store import make_store
from .worker import TripWorker

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "travel-orchestrator")
PREFIX = "/a2a/travel-orchestrator"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

storage, broker = InMemoryStorage(), InMemoryBroker()
worker = TripWorker(broker=broker, storage=storage)
a2a = FastA2A(
    storage=storage, broker=broker, name=AGENT_NAME, url=PUBLIC_URL, version="0.3.0",
    description="Plans and books a trip: shortlists destinations, picks one, finds a flight and books it "
                "(after human approval) by calling specialist agents over A2A.",
    skills=[Skill(id="plan-trip", name="Plan trip", tags=["travel", "planning", "booking"],
                  description="Shortlist destinations, choose one, find a flight, and book it using specialist agents.",
                  examples=["Plan a high budget museum trip for Maarten from AMS on 2026-07-15"],
                  input_modes=["text/plain", "application/json"], output_modes=["application/json"])],
    default_input_modes=["text/plain", "application/json"], default_output_modes=["application/json"],
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    store = await make_store()
    worker.service = TripService(store, Specialists.from_env())
    try:
        async with a2a.task_manager, worker.run():
            yield
    finally:
        await store.close()


app = FastAPI(title=AGENT_NAME, lifespan=lifespan)


def service() -> TripService:
    assert worker.service is not None, "orchestrator is still starting"
    return worker.service


@app.exception_handler(AwaitingApproval)
async def _awaiting(_, exc: AwaitingApproval):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "awaiting_approval", "thread_id": str(exc),
                         "detail": "A booking on this thread is waiting for a decision; POST /approve first."}, 409)


@app.exception_handler(NotAwaitingApproval)
async def _not_awaiting(_, exc: NotAwaitingApproval):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "not_awaiting_approval", "thread_id": str(exc),
                         "detail": "No booking is waiting for a decision on this thread; see GET /pending."}, 404)


@app.exception_handler(ModelHTTPError)
@app.exception_handler(UnexpectedModelBehavior)
async def _model_error(_, exc: Exception):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "model_unavailable", "detail": f"{type(exc).__name__}: {exc}"}, 502)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": AGENT_NAME}


def _prompt(payload: dict[str, Any]) -> str:
    return str(payload.get("prompt") or payload.get("text") or "")


@app.post("/plan")
@app.post(f"{PREFIX}/plan")
async def plan_trip(payload: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(payload.get("thread_id") or uuid.uuid4())
    return {"thread_id": thread_id, **await service().run_trip(_prompt(payload), thread_id)}


@app.post("/plan/stream")
@app.post(f"{PREFIX}/plan/stream")
async def plan_trip_stream(payload: dict[str, Any]) -> StreamingResponse:
    import json
    thread_id = str(payload.get("thread_id") or uuid.uuid4())

    async def events() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'event': 'start', 'thread_id': thread_id})}\n\n"
        try:
            async for event in service().stream_trip(_prompt(payload), thread_id):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except (AwaitingApproval, ModelHTTPError, UnexpectedModelBehavior) as exc:
            yield f"data: {json.dumps({'event': 'error', 'error': type(exc).__name__, 'detail': str(exc)})}\n\n"
        yield f"data: {json.dumps({'event': 'end'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


class ApprovalRequest(BaseModel):
    thread_id: str
    approved: bool


@app.get("/pending")
@app.get(f"{PREFIX}/pending")
async def pending_approvals() -> dict[str, Any]:
    """Runs paused awaiting a human booking decision, keyed by thread_id. This is a query on the thread
    store, so the list survives a restart of this service."""
    return {"pending": await service().pending()}


@app.post("/approve")
@app.post(f"{PREFIX}/approve")
async def approve(payload: ApprovalRequest) -> dict[str, Any]:
    """Resume a paused run with a human's approve/reject decision for its pending booking."""
    return {"thread_id": payload.thread_id, **await service().resume_trip(payload.thread_id, payload.approved)}


# Mounted last, so the explicit routes above win over the A2A app's catch-all under the same prefix.
app.mount(PREFIX, a2a)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4200")))
