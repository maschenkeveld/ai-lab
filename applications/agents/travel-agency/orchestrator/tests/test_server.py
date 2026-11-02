"""The orchestrator over HTTP and A2A, in-process: /plan -> /pending -> /approve, and message/send."""
from contextlib import asynccontextmanager

import httpx
from pydantic_ai.models.test import TestModel
from travel_common.a2a_client import call_agent
from travel_orchestrator import server
from travel_orchestrator.planner import planner, requirements_agent
from travel_orchestrator.store import MemoryStore

from .test_service import ATH_FLIGHT, BOOKABLE, REQUIREMENTS, FakeSpecialists


@asynccontextmanager
async def running(monkeypatch):
    """The orchestrator app (lifespan running) with a scripted model, in-memory store and fake specialists."""
    async def memory_store():
        return MemoryStore()

    monkeypatch.setattr(server, "make_store", memory_store)
    monkeypatch.setattr(server.Specialists, "from_env", classmethod(lambda cls: FakeSpecialists({"ATH": ATH_FLIGHT})))
    with requirements_agent.override(model=TestModel(custom_output_args=REQUIREMENTS)), planner.override(model=BOOKABLE):
        async with server.app.router.lifespan_context(server.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as c:
                yield c


async def test_plan_pending_approve(monkeypatch):
    async with running(monkeypatch) as client:
        plan = (await client.post("/plan", json={"prompt": "Museum trip", "thread_id": "t1"})).json()
        assert plan["status"] == "awaiting_approval" and plan["thread_id"] == "t1"

        pending = (await client.get("/a2a/travel-orchestrator/pending")).json()["pending"]
        assert pending["t1"]["to"] == "ATH"

        done = (await client.post("/a2a/travel-orchestrator/approve", json={"thread_id": "t1", "approved": True})).json()
        assert done["status"] == "booked" and done["booking"]["code"] == "BK123"
        assert (await client.get("/pending")).json() == {"pending": {}}


async def test_conflicts_and_unknown_threads_are_clear_errors(monkeypatch):
    async with running(monkeypatch) as client:
        await client.post("/plan", json={"prompt": "Museum trip", "thread_id": "t1"})
        again = await client.post("/plan", json={"prompt": "Another", "thread_id": "t1"})
        assert again.status_code == 409 and again.json()["error"] == "awaiting_approval"
        unknown = await client.post("/approve", json={"thread_id": "nope", "approved": True})
        assert unknown.status_code == 404


async def test_a2a_message_send_ends_input_required_with_the_pending_booking(monkeypatch):
    async with running(monkeypatch) as client:
        result = await call_agent("http://test/a2a/travel-orchestrator", {"prompt": "Museum trip", "thread_id": "t9"},
                                  http_client=client, allow_input_required=True, timeout=10)
        assert result["status"] == "awaiting_approval" and result["thread_id"] == "t9"
        assert (await client.get("/pending")).json()["pending"]["t9"]["from"] == "AMS"


async def test_agent_card(monkeypatch):
    async with running(monkeypatch) as client:
        card = (await client.get("/a2a/travel-orchestrator/.well-known/agent-card.json")).json()
        assert card["name"] == "travel-orchestrator"
        assert [s["id"] for s in card["skills"]] == ["plan-trip"]
