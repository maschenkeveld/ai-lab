"""The fare-watcher's durable core: one DBOS workflow per watched booking.

A watch wakes up every `interval_seconds` (a durable sleep: it survives restarts and costs nothing while asleep),
quotes the booked route again and compares the fare with the baseline. Whether a drop is large enough to bother
anyone is a code rule (`significant_drop`); only a drop that passes it is shown to the model, whose job is
judgment: is rebooking worth the traveller's attention? The verdict goes to the coordinator as an A2A message.
"""
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from dbos import DBOS, SetWorkflowID
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from travel_common.contracts import FareAssessment, FareEvent, WatchRequest, WatchState

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id              text PRIMARY KEY,
    trip_id         text NOT NULL,
    status          text NOT NULL DEFAULT 'watching',
    origin          text NOT NULL,
    to_iata         text NOT NULL,
    date            text NOT NULL,
    currency        text NOT NULL DEFAULT 'EUR',
    baseline_price  double precision NOT NULL,
    last_price      double precision NOT NULL DEFAULT 0,
    notified_price  double precision,
    checks          integer NOT NULL DEFAULT 0,
    max_checks      integer NOT NULL,
    interval_seconds double precision NOT NULL,
    min_drop_pct    double precision NOT NULL,
    min_drop_eur    double precision NOT NULL,
    callback_url    text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS watches_trip ON watches (trip_id, status);
"""


def significant_drop(reference: float, price: float, min_pct: float, min_eur: float) -> tuple[bool, float]:
    """Is `price` far enough below `reference` to report? Needs both the percentage and the absolute drop."""
    if reference <= 0 or price >= reference:
        return False, 0.0
    drop = reference - price
    pct = round(drop / reference * 100, 1)
    return (pct >= min_pct and drop >= min_eur), pct


Quote = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
Assess = Callable[[dict[str, Any]], Awaitable[FareAssessment]]
Notify = Callable[[str, FareEvent], Awaitable[None]]


@dataclass
class Deps:
    pool: AsyncConnectionPool
    quote: Quote       # watch row -> {"price": float, "priced_at": str} or None if the route cannot be quoted
    assess: Assess     # facts -> FareAssessment
    notify: Notify     # (callback_url, event) -> None; raises if the coordinator cannot be reached


_deps: Deps | None = None


def configure(deps: Deps) -> None:
    global _deps
    _deps = deps


def d() -> Deps:
    assert _deps is not None, "fare-watcher dependencies are not configured"
    return _deps


async def connect(url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False, kwargs={"row_factory": dict_row, "autocommit": True})
    await pool.open(wait=True, timeout=60)
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    return pool


async def _one(sql: str, *args: Any) -> dict | None:
    async with d().pool.connection() as conn:
        return await (await conn.execute(sql, args)).fetchone()


async def _run(sql: str, *args: Any) -> int:
    async with d().pool.connection() as conn:
        return (await conn.execute(sql, args)).rowcount


def state_of(row: dict) -> WatchState:
    return WatchState(watch_id=row["id"], trip_id=row["trip_id"], status=row["status"], baseline_price=row["baseline_price"],
                      last_price=row["last_price"], checks=row["checks"])


# ---- steps ---------------------------------------------------------------------------------------------------


@DBOS.step()
async def s_load(watch_id: str) -> dict:
    row = await _one("SELECT * FROM watches WHERE id = %s", watch_id)
    assert row is not None, f"no watch {watch_id}"
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)
async def s_quote(watch: dict) -> dict | None:
    return await d().quote(watch)


@DBOS.step()
async def s_record(watch_id: str, price: float | None) -> None:
    if price is None:
        await _run("UPDATE watches SET checks = checks + 1, updated_at = now() WHERE id = %s", watch_id)
    else:
        await _run("UPDATE watches SET checks = checks + 1, last_price = %s, updated_at = now() WHERE id = %s", price, watch_id)


@DBOS.step(retries_allowed=True, max_attempts=2, interval_seconds=1.0)
async def s_assess(facts: dict) -> dict:
    return (await d().assess(facts)).model_dump()


@DBOS.step()
async def s_notify(watch: dict, event: dict) -> bool:
    """Tell the coordinator. Only a delivered notification counts as notified, so a failure is retried at the next check."""
    try:
        await d().notify(watch["callback_url"], FareEvent.model_validate(event))
    except Exception as exc:
        log.warning("could not notify %s for trip %s: %s", watch["callback_url"], watch["trip_id"], exc)
        return False
    await _run("UPDATE watches SET notified_price = %s WHERE id = %s", event["new_price"], watch["id"])
    return True


@DBOS.step()
async def s_finish(watch_id: str, status: str) -> None:
    await _run("UPDATE watches SET status = %s, updated_at = now() WHERE id = %s AND status = 'watching'", status, watch_id)


# ---- the workflow --------------------------------------------------------------------------------------------


@DBOS.workflow()
async def watch_workflow(watch_id: str) -> str:
    while True:
        watch = await s_load(watch_id)
        if watch["status"] != "watching":
            return watch["status"]
        if watch["checks"] >= watch["max_checks"]:
            await s_finish(watch_id, "completed")
            return "completed"
        await DBOS.sleep_async(watch["interval_seconds"])
        watch = await s_load(watch_id)  # it may have been stopped while we slept
        if watch["status"] != "watching":
            return watch["status"]

        quote = await s_quote(watch)
        await s_record(watch_id, quote["price"] if quote else None)
        if quote is None:
            continue
        reference = watch["notified_price"] if watch["notified_price"] is not None else watch["baseline_price"]
        significant, pct = significant_drop(reference, quote["price"], watch["min_drop_pct"], watch["min_drop_eur"])
        if not significant:
            continue
        assessment = await s_assess({"old_price": watch["baseline_price"], "new_price": quote["price"], "drop_pct": pct,
                                     "currency": watch["currency"], "route": f"{watch['origin']}-{watch['to_iata']}",
                                     "date": watch["date"], "checks_so_far": watch["checks"] + 1})
        await s_notify(watch, FareEvent(trip_id=watch["trip_id"], watch_id=watch_id, old_price=watch["baseline_price"],
                                        new_price=quote["price"], currency=watch["currency"], drop_pct=pct,
                                        priced_at=quote.get("priced_at", ""),
                                        assessment=FareAssessment.model_validate(assessment)).model_dump(mode="json"))


# ---- A2A handler ---------------------------------------------------------------------------------------------


async def handle(data: dict[str, Any]) -> dict[str, Any]:
    request = WatchRequest.model_validate(data)
    if request.action == "unwatch":
        await _run("UPDATE watches SET status = 'stopped', updated_at = now() WHERE trip_id = %s AND status = 'watching'", request.trip_id)
        row = await _one("SELECT * FROM watches WHERE trip_id = %s ORDER BY created_at DESC LIMIT 1", request.trip_id)
        return state_of(row).model_dump(mode="json") if row else WatchState(watch_id="", trip_id=request.trip_id, status="stopped").model_dump(mode="json")

    if not (request.origin and request.to_iata and request.date and request.booked_price > 0 and request.callback_url):
        raise ValueError("a watch needs origin, to_iata, date, booked_price and callback_url")
    # One active watch per trip: a new baseline (after a rebooking) replaces the old watch.
    await _run("UPDATE watches SET status = 'stopped', updated_at = now() WHERE trip_id = %s AND status = 'watching'", request.trip_id)
    watch_id = f"w_{request.trip_id}_{uuid.uuid4().hex[:6]}"
    await _run("INSERT INTO watches (id, trip_id, origin, to_iata, date, currency, baseline_price, max_checks, interval_seconds, "
               "min_drop_pct, min_drop_eur, callback_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
               watch_id, request.trip_id, request.origin, request.to_iata, request.date, request.currency, request.booked_price,
               request.max_checks, request.interval_seconds, request.min_drop_pct, request.min_drop_eur, request.callback_url)
    with SetWorkflowID(watch_id):
        await DBOS.start_workflow_async(watch_workflow, watch_id)
    return state_of(await _one("SELECT * FROM watches WHERE id = %s", watch_id)).model_dump(mode="json")
