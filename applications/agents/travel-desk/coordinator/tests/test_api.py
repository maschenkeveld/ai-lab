"""The REST API and the A2A handler over real HTTP (in-process), on the same rig as the workflow tests."""

import httpx
import pytest
from travel_common.contracts import FareAssessment, FareEvent
from travel_coordinator import api


@pytest.fixture
async def client(make_desk):
    desk = await make_desk()
    app = api.build_app(desk.service, lifespan=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.desk = desk
        yield c


A = "/desk/api"


async def pending_id(client, trip_id):
    return (await client.desk.pending(trip_id))["id"]


async def test_config_exposes_the_policy(client):
    body = (await client.get(f"{A}/config")).json()
    assert [r["approvals"] for r in body["policy"]["rules"]] == [0, 1, 2]
    assert body["demo_mode"] is False


async def test_the_web_app_and_health_are_served(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    assert (await client.get("/desk/")).status_code == 200


async def test_create_trip_approve_and_read_back_the_audit_trail(client):
    created = await client.post(f"{A}/trips", json={"request": "culture in vienna", "requested_by": "alice"})
    assert created.status_code == 202
    trip_id = created.json()["id"]
    approval_id = await pending_id(client, trip_id)

    listed = (await client.get(f"{A}/approvals", params={"status": "pending"})).json()
    assert [a["id"] for a in listed] == [approval_id]

    decided = await client.post(f"{A}/approvals/{approval_id}/decision", json={"approver": "bob", "decision": "approve", "comment": "ok"})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    await client.desk.until(trip_id, "completed")

    snapshot = (await client.get(f"{A}/trips/{trip_id}")).json()
    assert snapshot["trip"]["requested_by"] == "alice" and snapshot["trip"]["booking"]["status"] == "scheduled"
    kinds = [e["type"] for e in snapshot["audit"]]
    assert kinds.index("trip_requested") < kinds.index("approval_approve") < kinds.index("booking_confirm")
    assert next(e for e in snapshot["audit"] if e["type"] == "approval_approve")["actor"] == "bob"


async def test_deciding_twice_is_a_conflict(client):
    trip_id = (await client.post(f"{A}/trips", json={"request": "vienna"})).json()["id"]
    approval_id = await pending_id(client, trip_id)
    body = {"approver": "bob", "decision": "reject"}
    assert (await client.post(f"{A}/approvals/{approval_id}/decision", json=body)).status_code == 200
    again = await client.post(f"{A}/approvals/{approval_id}/decision", json={"approver": "carol", "decision": "approve"})
    assert again.status_code == 409 and "already rejected" in again.json()["detail"]


async def test_invalid_input_is_rejected_before_anything_happens(client):
    assert (await client.post(f"{A}/trips", json={"request": "x"})).status_code == 422
    trip_id = (await client.post(f"{A}/trips", json={"request": "vienna"})).json()["id"]
    approval_id = await pending_id(client, trip_id)
    assert (await client.post(f"{A}/approvals/{approval_id}/decision", json={"approver": "bob", "decision": "maybe"})).status_code == 422
    assert (await client.post(f"{A}/approvals/{approval_id}/decision", json={"approver": "", "decision": "approve"})).status_code == 422
    assert (await client.get(f"{A}/approvals", params={"status": "bogus"})).status_code == 422


async def test_unknown_records_are_404(client):
    assert (await client.get(f"{A}/trips/nope")).status_code == 404
    assert (await client.post(f"{A}/approvals/nope/decision", json={"approver": "bob", "decision": "approve"})).status_code == 404


async def test_cancel_a_waiting_trip(client):
    trip_id = (await client.post(f"{A}/trips", json={"request": "vienna"})).json()["id"]
    approval_id = await pending_id(client, trip_id)
    cancelled = await client.post(f"{A}/trips/{trip_id}/cancel", json={"actor": "alice"})
    assert cancelled.json()["status"] == "cancelled"
    assert client.desk.stub.statuses() == ["cancelled"]
    assert (await client.get(f"{A}/approvals/{approval_id}")).json()["status"] == "cancelled"
    assert (await client.post(f"{A}/trips/{trip_id}/cancel", json={})).status_code == 409


async def test_the_demo_fare_drop_is_off_unless_demo_mode(client):
    assert (await client.post(f"{A}/demo/fare-drop/anything", json={})).status_code == 404


async def test_a2a_handler_creates_trips_and_takes_fare_events(client):
    desk = client.desk
    made = await api.a2a_handler(desk.service, {"type": "create_trip", "request": "culture in vienna", "requested_by": "agent"})
    trip_id = made["trip_id"]
    approval_id = await pending_id(client, trip_id)
    await desk.service.decide(approval_id, "bob", "approve")
    await desk.until(trip_id, "booked")

    event = FareEvent(trip_id=trip_id, watch_id="w", old_price=120.0, new_price=90.0, drop_pct=25.0,
                      assessment=FareAssessment(worth_rebooking=False, reasoning="meh")).model_dump(mode="json")
    assert (await api.a2a_handler(desk.service, event))["accepted"] is True
    assert (await api.a2a_handler(desk.service, {**event, "trip_id": "nope"}))["accepted"] is False
    status = await api.a2a_handler(desk.service, {"type": "get_trip", "trip_id": trip_id})
    assert status["status"] in ("booked", "completed") and status["pending_approvals"] == []
    with pytest.raises(ValueError, match="unknown message type"):
        await api.a2a_handler(desk.service, {"type": "dance"})
    await desk.service.cancel_trip(trip_id, "test")
