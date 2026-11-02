import asyncio
import uuid

import psycopg
import pytest
from dbos import DBOS
from fare_watcher import watcher
from fare_watcher.watcher import significant_drop
from travel_common.contracts import FareAssessment


def test_a_drop_needs_both_the_percentage_and_the_amount():
    assert significant_drop(120, 100, 8, 10) == (True, 16.7)
    assert significant_drop(120, 115, 8, 10) == (False, 4.2)    # under 8%
    assert significant_drop(40, 35, 8, 10) == (False, 12.5)     # 12.5% but only 5 EUR
    assert significant_drop(120, 125, 8, 10) == (False, 0.0)    # a rise is never a drop
    assert significant_drop(0, 10, 8, 10) == (False, 0.0)


@pytest.fixture(scope="module")
def runtime(postgres_url):
    db = f"fw_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(f"{postgres_url}/postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    url = f"{postgres_url}/{db}"
    DBOS(config={"name": "fare-watcher-test", "system_database_url": url})
    DBOS.launch()
    yield url
    DBOS.destroy()


class Rig:
    def __init__(self, pool, prices):
        self.prices, self.notified, self.fail_notify = list(prices), [], 0
        self.quotes = 0

        async def quote(row):
            self.quotes += 1
            price = self.prices[min(self.quotes - 1, len(self.prices) - 1)]
            return None if price is None else {"price": price, "priced_at": "t"}

        async def assess(facts):
            return FareAssessment(worth_rebooking=facts["new_price"] < 100, reasoning=f"{facts['drop_pct']}% drop")

        async def notify(url, event):
            if self.fail_notify > 0:
                self.fail_notify -= 1
                raise ConnectionError("coordinator down")
            self.notified.append(event)

        watcher.configure(watcher.Deps(pool=pool, quote=quote, assess=assess, notify=notify))


@pytest.fixture
async def make_rig(runtime):
    pools = []

    async def make(prices):
        pool = await watcher.connect(runtime)
        pools.append(pool)
        return Rig(pool, prices)

    yield make
    for p in pools:
        await p.close()


def watch(trip="trip1", **kw):
    return {"action": "watch", "trip_id": trip, "origin": "AMS", "to_iata": "VIE", "date": "2026-12-18", "booked_price": 120.0,
            "callback_url": "http://coordinator/a2a", "interval_seconds": 0.2, "max_checks": 6, **kw}


async def status_of(rig, watch_id):
    return (await watcher._one("SELECT * FROM watches WHERE id = %s", watch_id))["status"]


async def completed(rig, watch_id, timeout=15):
    for _ in range(int(timeout * 20)):
        if await status_of(rig, watch_id) == "completed":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"watch stayed {await status_of(rig, watch_id)}")


async def test_a_real_drop_is_assessed_and_reported_once(make_rig):
    rig = await make_rig([119, 118, 95, 95, 95, 95])
    state = await watcher.handle(watch())
    await completed(rig, state["watch_id"])
    assert [(e.old_price, e.new_price) for e in rig.notified] == [(120.0, 95.0)]   # once, not on every later check
    assert rig.notified[0].assessment.worth_rebooking is True and rig.notified[0].drop_pct == 20.8


async def test_prices_that_do_not_move_enough_report_nothing(make_rig):
    rig = await make_rig([118, 117, 119, 116])
    state = await watcher.handle(watch(trip="trip2", max_checks=4))
    await completed(rig, state["watch_id"])
    assert rig.notified == []


async def test_a_failed_notification_is_retried_at_the_next_check(make_rig):
    rig = await make_rig([95, 95, 95, 95])
    rig.fail_notify = 1
    state = await watcher.handle(watch(trip="trip3", max_checks=4))
    await completed(rig, state["watch_id"])
    assert len(rig.notified) == 1  # the first delivery failed, so the drop was not marked as notified


async def test_unwatch_stops_the_watch(make_rig):
    rig = await make_rig([120] * 50)
    state = await watcher.handle(watch(trip="trip4", max_checks=50))
    stopped = await watcher.handle({"action": "unwatch", "trip_id": "trip4"})
    assert stopped["status"] == "stopped"
    await asyncio.sleep(0.8)
    assert rig.quotes <= 2 and await status_of(rig, state["watch_id"]) == "stopped"


async def test_a_new_watch_replaces_the_old_one_for_the_same_trip(make_rig):
    rig = await make_rig([120] * 50)
    first = await watcher.handle(watch(trip="trip5", max_checks=50))
    second = await watcher.handle(watch(trip="trip5", booked_price=95.0, max_checks=50))
    assert first["watch_id"] != second["watch_id"]
    assert await status_of(rig, first["watch_id"]) == "stopped" and await status_of(rig, second["watch_id"]) == "watching"
    await watcher.handle({"action": "unwatch", "trip_id": "trip5"})


async def test_an_unquotable_route_is_counted_and_skipped(make_rig):
    rig = await make_rig([None, None, None])
    state = await watcher.handle(watch(trip="trip6", max_checks=3))
    await completed(rig, state["watch_id"])
    assert rig.notified == []


async def test_incomplete_requests_are_rejected(make_rig):
    await make_rig([100])
    with pytest.raises(ValueError, match="needs origin"):
        await watcher.handle({"action": "watch", "trip_id": "x"})
