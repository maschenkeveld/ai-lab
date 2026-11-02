import asyncio
import uuid

import psycopg
import pytest
from travel_coordinator.policy import describe, required_approvals
from travel_coordinator.settings import Policy
from travel_coordinator.store import DecisionConflict, NotFound, Store

POLICY = Policy(auto_approve_below=60, second_approver_from=180)


def test_policy_tiers():
    assert required_approvals(39.18, POLICY) == 0
    assert required_approvals(59.99, POLICY) == 0
    assert required_approvals(60, POLICY) == 1
    assert required_approvals(120.21, POLICY) == 1
    assert required_approvals(179.99, POLICY) == 1
    assert required_approvals(180, POLICY) == 2
    assert required_approvals(540, POLICY) == 2


def test_policy_is_described_for_the_ui():
    rules = describe(POLICY)["rules"]
    assert [r["approvals"] for r in rules] == [0, 1, 2]


@pytest.fixture
async def store(postgres_url):
    db = f"t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(f"{postgres_url}/postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    s = await Store.connect(f"{postgres_url}/{db}")
    yield s
    await s.close()


async def make_approval(store: Store, required: int = 1) -> dict:
    trip_id = f"trip_{uuid.uuid4().hex[:6]}"
    await store.create_trip(trip_id, "alice", "weekend in vienna")
    return await store.create_approval(trip_id=trip_id, kind="booking", required=required,
                                       proposal={"price": 120.2}, summary="s", ttl_seconds=600, escalate_after_seconds=300)


async def test_audit_log_is_append_only(store):
    await store.audit("alice", "something", trip_id="t1")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await store._run("UPDATE audit SET actor = 'mallory'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await store._run("DELETE FROM audit")
    assert len(await store.audit_log("t1")) == 1


async def test_single_approver_approves(store):
    approval = await make_approval(store, required=1)
    after, message = await store.decide(approval["id"], "bob", "approve", "looks fine")
    assert after["status"] == "approved"
    assert message == {"decision": "approved", "by": ["bob"]}
    assert after["resolved_at"] is not None


async def test_two_approvers_must_be_different_people(store):
    approval = await make_approval(store, required=2)
    after, message = await store.decide(approval["id"], "bob", "approve")
    assert after["status"] == "pending" and message is None
    with pytest.raises(DecisionConflict, match="already decided"):
        await store.decide(approval["id"], "Bob", "approve")  # same person, different case
    after, message = await store.decide(approval["id"], "carol", "approve")
    assert after["status"] == "approved"
    assert message["by"] == ["bob", "carol"]


async def test_reject_resolves_immediately_even_with_two_required(store):
    approval = await make_approval(store, required=2)
    after, message = await store.decide(approval["id"], "bob", "reject", "too expensive")
    assert after["status"] == "rejected"
    assert message == {"decision": "rejected", "by": "bob", "comment": "too expensive"}


async def test_edit_supersedes_the_approval_and_carries_the_change(store):
    approval = await make_approval(store)
    after, message = await store.decide(approval["id"], "bob", "edit", "cheaper please", edit={"destination": "Madrid"})
    assert after["status"] == "superseded"
    assert message["edit"] == {"destination": "Madrid"}


async def test_a_resolved_approval_cannot_be_decided_again(store):
    approval = await make_approval(store)
    await store.decide(approval["id"], "bob", "approve")
    with pytest.raises(DecisionConflict, match="already approved"):
        await store.decide(approval["id"], "carol", "reject")


async def test_concurrent_decisions_resolve_exactly_once(store):
    approval = await make_approval(store, required=1)
    results = await asyncio.gather(*(store.decide(approval["id"], f"user{i}", "approve") for i in range(6)),
                                   return_exceptions=True)
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1
    assert all(isinstance(r, DecisionConflict) for r in results if isinstance(r, Exception))


async def test_expiry_loses_to_a_decision_that_got_there_first(store):
    approval = await make_approval(store)
    await store.decide(approval["id"], "bob", "approve")
    assert await store.expire_approval(approval["id"]) is False
    assert (await store.get_approval(approval["id"]))["status"] == "approved"


async def test_expiry_and_escalation(store):
    approval = await make_approval(store)
    await store.escalate_approval(approval["id"])
    assert (await store.get_approval(approval["id"]))["escalated"] is True
    assert await store.expire_approval(approval["id"]) is True
    types = [a["type"] for a in await store.audit_log(approval["trip_id"])]
    assert "approval_escalated" in types and "approval_expired" in types


async def test_unknown_ids(store):
    with pytest.raises(NotFound):
        await store.get_trip("nope")
    with pytest.raises(NotFound):
        await store.decide("nope", "bob", "approve")
