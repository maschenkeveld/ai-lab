"""A throwaway Postgres in Docker for the whole test session (tests that need a database skip without Docker)."""
import shutil
import subprocess
import time
import uuid

import pytest


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="session")
def postgres_url():
    if not _docker_ok():
        pytest.skip("Docker is not available")
    name = f"desk-test-pg-{uuid.uuid4().hex[:6]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=postgres",
                    "-p", "127.0.0.1::5432", "postgres:16-alpine"], check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
                              ).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        time.sleep(1)  # pg_isready can answer during the init restart
        yield f"postgresql://postgres:postgres@127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ---- the rig shared by the workflow and API tests: real DBOS + Postgres, simulated specialists ------------------
import asyncio  # noqa: E402

import psycopg  # noqa: E402
from dbos import DBOS  # noqa: E402
from travel_common.contracts import (  # noqa: E402
    BookingCommand,
    BookingState,
    Destination,
    DestinationRequest,
    DestinationResult,
    FlightOption,
    FlightOptions,
    FlightQuoteRequest,
    TripRequirements,
    WatchRequest,
    WatchState,
)
from travel_coordinator import workflow  # noqa: E402
from travel_coordinator.service import DeskService  # noqa: E402
from travel_coordinator.settings import Agents, Policy  # noqa: E402
from travel_coordinator.store import Store  # noqa: E402

PRICES = {"BRU": 39.0, "VIE": 120.0, "MAD": 250.0}  # auto / one approver / two approvers under the test policy
NAMES = {"BRU": "Brussels", "VIE": "Vienna", "MAD": "Madrid"}


class StubSpecialists:
    def __init__(self):
        self.bookings: dict[str, dict] = {}
        self.keys: dict[str, str] = {}
        self.watches: list[WatchRequest] = []
        self.price_override: dict[str, float] = {}

    async def destinations(self, request: DestinationRequest) -> DestinationResult:
        order = ["VIE", "MAD", "BRU"]  # rank order: the first is what gets proposed
        return DestinationResult(destinations=[Destination(name=NAMES[c], country="x", airport_iata_codes=[c]) for c in order],
                                 reasoning="stub ranking")

    async def flights(self, request: FlightQuoteRequest) -> FlightOptions:
        price = self.price_override.get(request.to_iata, PRICES[request.to_iata])
        return FlightOptions(options=[FlightOption(date=request.dates[0], flight_number=f"KA-{request.origin}{request.to_iata}", price=price)],
                             recommended_date=request.dates[0], reasoning="stub")

    async def booking(self, c: BookingCommand) -> BookingState:
        if c.action == "hold":
            if c.idempotency_key in self.keys:
                return BookingState(**self.bookings[self.keys[c.idempotency_key]], idempotent_replay=True)
            code = uuid.uuid4().hex[:6].upper()
            self.bookings[code] = {"booking_code": code, "status": "held", "origin": c.origin, "to_iata": c.to_iata, "date": c.date}
            self.keys[c.idempotency_key] = code
        else:
            code = c.booking_code
        b = self.bookings[code]
        if c.action == "confirm" and b["status"] == "held":
            b["status"] = "scheduled"
        if c.action == "cancel" and b["status"] in ("held", "scheduled"):
            b["status"] = "cancelled"
        return BookingState(**b)

    async def watch(self, request: WatchRequest) -> WatchState:
        self.watches.append(request)
        return WatchState(watch_id="w", trip_id=request.trip_id, status="watching" if request.action == "watch" else "stopped")

    def statuses(self) -> list[str]:
        return [b["status"] for b in self.bookings.values()]


class StubLlm:
    async def extract(self, text: str) -> TripRequirements:
        return TripRequirements(traveller="Alice", origin="AMS", date="2026-12-18", vibes=["culture"])

    async def summarize(self, facts: dict) -> str:
        return f"Fly to {facts['destination']['name']}."


@pytest.fixture(scope="module")
def runtime(postgres_url):
    db = f"wf_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(f"{postgres_url}/postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    url = f"{postgres_url}/{db}"
    DBOS(config={"name": "coordinator-test", "system_database_url": url})
    DBOS.launch()
    yield url
    DBOS.destroy()


FAST = Policy(auto_approve_below=60, second_approver_from=180, approval_ttl_seconds=30, escalate_after_seconds=15,
              hold_buffer_seconds=5, max_edits=2, monitor_seconds=1.5)


class Desk:
    def __init__(self, store: Store, specialists: StubSpecialists, policy: Policy):
        self.stub = specialists
        workflow.configure(workflow.Deps(store=store, specialists=specialists, llm=StubLlm(), policy=policy, agents=Agents()))
        self.service = DeskService(workflow._deps)
        self.store = store

    async def status(self, trip_id: str) -> str:
        return (await self.store.get_trip(trip_id))["status"]

    async def until(self, trip_id: str, *statuses: str, timeout: float = 30) -> str:
        for _ in range(int(timeout * 20)):
            status = await self.status(trip_id)
            if status in statuses:
                return status
            await asyncio.sleep(0.05)
        raise AssertionError(f"trip stayed '{await self.status(trip_id)}', wanted {statuses}")

    async def pending(self, trip_id: str) -> dict:
        for _ in range(200):
            rows = await self.store.list_approvals(status="pending", trip_id=trip_id)
            if rows:
                return rows[0]
            await asyncio.sleep(0.05)
        raise AssertionError("no pending approval appeared")


@pytest.fixture
async def make_desk(runtime):
    stores = []

    async def make(policy: Policy = FAST) -> Desk:
        store = await Store.connect(runtime)
        stores.append(store)
        return Desk(store, StubSpecialists(), policy)

    yield make
    for s in stores:
        await s.close()


