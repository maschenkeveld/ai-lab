"""The trip workflow end to end, with a real DBOS on a real Postgres and stub specialists.

The stubs simulate the booking system faithfully (holds, confirm, cancel, expiry) so the tests can assert the
saga's invariants: a rejected, expired or edited proposal always ends with its hold cancelled, and a rebooking
never leaves the traveller without a seat. The rig (stubs, Desk, fixtures) lives in conftest.py.
"""
import asyncio
from dataclasses import replace

import pytest
from conftest import FAST
from travel_common.contracts import FareAssessment, FareEvent, FlightOptions
from travel_coordinator.store import DecisionConflict


async def test_cheap_trip_is_booked_without_a_human(make_desk):
    desk = await make_desk()
    desk.stub.price_override.update({"VIE": 39.0})  # the first-ranked destination is under the auto-approve line
    trip = await desk.service.create_trip("a quick trip")
    assert await desk.until(trip["id"], "completed") == "completed"
    assert desk.stub.statuses() == ["scheduled"]
    types = [a["type"] for a in await desk.store.audit_log(trip["id"])]
    assert "auto_approved" in types and await desk.store.list_approvals(trip_id=trip["id"]) == []


async def test_one_approver_approves_and_the_trip_is_booked_and_watched(make_desk):
    desk = await make_desk()
    trip = await desk.service.create_trip("culture in vienna")
    approval = await desk.pending(trip["id"])
    assert approval["required"] == 1 and approval["proposal"]["destination"]["name"] == "Vienna"
    assert desk.stub.statuses() == ["held"]
    await desk.service.decide(approval["id"], "bob", "approve", "fine")
    assert await desk.until(trip["id"], "booked", "completed") in ("booked", "completed")
    assert await desk.until(trip["id"], "completed") == "completed"
    assert desk.stub.statuses() == ["scheduled"]
    assert [w.action for w in desk.stub.watches] == ["watch", "unwatch"]


async def test_two_approvers_are_needed_for_an_expensive_trip(make_desk):
    desk = await make_desk()
    desk.stub.price_override["VIE"] = 250.0
    trip = await desk.service.create_trip("expensive vienna")
    approval = await desk.pending(trip["id"])
    assert approval["required"] == 2
    await desk.service.decide(approval["id"], "bob", "approve")
    await asyncio.sleep(0.5)
    assert await desk.status(trip["id"]) == "awaiting_approval" and desk.stub.statuses() == ["held"]
    with pytest.raises(DecisionConflict):
        await desk.service.decide(approval["id"], "bob", "approve")
    await desk.service.decide(approval["id"], "carol", "approve")
    assert await desk.until(trip["id"], "completed") == "completed"
    assert desk.stub.statuses() == ["scheduled"]


async def test_rejection_releases_the_hold(make_desk):
    desk = await make_desk()
    trip = await desk.service.create_trip("vienna")
    approval = await desk.pending(trip["id"])
    await desk.service.decide(approval["id"], "bob", "reject", "too pricey")
    assert await desk.until(trip["id"], "rejected") == "rejected"
    assert desk.stub.statuses() == ["cancelled"]
    row = await desk.store.get_trip(trip["id"])
    assert "too pricey" in row["summary"]
    assert row["booking"]["status"] == "cancelled"  # the trip row mirrors the supplier


async def test_edit_swaps_the_destination_and_asks_again(make_desk):
    desk = await make_desk()
    trip = await desk.service.create_trip("vienna")
    first = await desk.pending(trip["id"])
    await desk.service.decide(first["id"], "bob", "edit", "somewhere cheaper", edit={"destination": "Brussels"})
    for _ in range(200):  # the edited proposal is auto-approved (39 EUR), so the trip goes straight through
        if await desk.status(trip["id"]) == "completed":
            break
        await asyncio.sleep(0.05)
    trip_row = await desk.store.get_trip(trip["id"])
    assert trip_row["status"] == "completed" and trip_row["proposal"]["destination"]["name"] == "Brussels"
    assert sorted(desk.stub.statuses()) == ["cancelled", "scheduled"]  # the first hold was released
    assert (await desk.store.get_approval(first["id"]))["status"] == "superseded"


async def test_unanswered_approval_escalates_then_expires_and_releases_the_hold(make_desk):
    desk = await make_desk(replace(FAST, approval_ttl_seconds=3, escalate_after_seconds=1))
    trip = await desk.service.create_trip("vienna")
    approval = await desk.pending(trip["id"])
    assert await desk.until(trip["id"], "expired", timeout=15) == "expired"
    row = await desk.store.get_approval(approval["id"])
    assert row["status"] == "expired" and row["escalated"] is True
    assert desk.stub.statuses() == ["cancelled"]
    assert (await desk.store.get_trip(trip["id"]))["booking"]["status"] == "cancelled"


async def test_no_matching_flight_fails_cleanly_without_a_hold(make_desk):
    desk = await make_desk()

    async def nothing(request):
        return FlightOptions(options=[])

    desk.stub.flights = nothing
    trip = await desk.service.create_trip("vienna")
    assert await desk.until(trip["id"], "failed") == "failed"
    assert desk.stub.bookings == {}


async def test_fare_drop_rebooks_with_approval_and_never_leaves_the_traveller_without_a_seat(make_desk):
    desk = await make_desk(replace(FAST, monitor_seconds=20))
    trip = await desk.service.create_trip("vienna")
    first = await desk.pending(trip["id"])
    await desk.service.decide(first["id"], "bob", "approve")
    await desk.until(trip["id"], "booked")
    original = (await desk.store.get_trip(trip["id"]))["booking"]["booking_code"]

    event = FareEvent(trip_id=trip["id"], watch_id="w", old_price=120.0, new_price=95.0, drop_pct=20.8,
                      assessment=FareAssessment(worth_rebooking=True, reasoning="25 EUR saved at no cost"))
    assert (await desk.service.fare_event(event))["accepted"] is True
    rebook = await desk.pending(trip["id"])
    assert rebook["kind"] == "rebook" and rebook["proposal"]["saving"] == 25.0
    await desk.service.decide(rebook["id"], "bob", "approve")

    for _ in range(200):
        row = await desk.store.get_trip(trip["id"])
        if row["status"] == "booked" and row["booking"]["booking_code"] != original:
            break
        await asyncio.sleep(0.05)
    assert row["booking"]["booking_code"] != original and row["price"] == 95.0
    states = {code: b["status"] for code, b in desk.stub.bookings.items()}
    assert states[original] == "cancelled" and states[row["booking"]["booking_code"]] == "scheduled"
    assert row["booking"]["status"] == "scheduled"  # the trip points at the new, live booking
    await desk.service.cancel_trip(trip["id"], "test")


async def test_a_declined_rebook_keeps_the_original_booking(make_desk):
    desk = await make_desk(replace(FAST, monitor_seconds=20))
    trip = await desk.service.create_trip("vienna")
    await desk.service.decide((await desk.pending(trip["id"]))["id"], "bob", "approve")
    await desk.until(trip["id"], "booked")
    event = FareEvent(trip_id=trip["id"], watch_id="w", old_price=120.0, new_price=95.0, drop_pct=20.8,
                      assessment=FareAssessment(worth_rebooking=True, reasoning="cheaper"))
    await desk.service.fare_event(event)
    rebook = await desk.pending(trip["id"])
    await desk.service.decide(rebook["id"], "bob", "reject")
    await desk.until(trip["id"], "booked")
    assert desk.stub.statuses() == ["scheduled"]
    await desk.service.cancel_trip(trip["id"], "test")


async def test_a_fare_drop_that_is_not_worth_it_is_ignored(make_desk):
    desk = await make_desk(replace(FAST, monitor_seconds=20))
    trip = await desk.service.create_trip("vienna")
    await desk.service.decide((await desk.pending(trip["id"]))["id"], "bob", "approve")
    await desk.until(trip["id"], "booked")
    await desk.service.fare_event(FareEvent(trip_id=trip["id"], watch_id="w", old_price=120.0, new_price=110.0, drop_pct=8.3,
                                            assessment=FareAssessment(worth_rebooking=False, reasoning="too small")))
    await asyncio.sleep(1)
    assert await desk.store.list_approvals(status="pending", trip_id=trip["id"]) == []
    assert "fare_drop_ignored" in [a["type"] for a in await desk.store.audit_log(trip["id"])]
    await desk.service.cancel_trip(trip["id"], "test")


async def test_cancelling_a_trip_releases_the_hold_and_closes_the_approval(make_desk):
    desk = await make_desk()
    trip = await desk.service.create_trip("vienna")
    approval = await desk.pending(trip["id"])
    await desk.service.cancel_trip(trip["id"], "alice")
    assert await desk.status(trip["id"]) == "cancelled"
    assert desk.stub.statuses() == ["cancelled"]
    assert (await desk.store.get_approval(approval["id"]))["status"] == "cancelled"
    with pytest.raises(DecisionConflict):
        await desk.service.decide(approval["id"], "bob", "approve")
