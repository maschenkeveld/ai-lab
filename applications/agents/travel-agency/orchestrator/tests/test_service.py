"""The orchestrator's whole flow, with a scripted model and fake specialists (no LLM, no cluster)."""
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from travel_common.contracts import (
    BookingResult,
    DecisionResult,
    Destination,
    DestinationResult,
    Flight,
    FlightResult,
)
from travel_orchestrator.planner import Specialists, planner, requirements_agent
from travel_orchestrator.service import (
    AwaitingApproval,
    NotAwaitingApproval,
    TripService,
)
from travel_orchestrator.store import MemoryStore

ATHENS = Destination(name="Athens", country="Greece", airport_iata_codes=["ATH"])
LISBON = Destination(name="Lisbon", country="Portugal", airport_iata_codes=["LIS"])
SHORTLIST = [ATHENS.model_dump(), LISBON.model_dump()]
REQUIREMENTS = {"passenger_name": "Maarten", "origin": "AMS", "date": "2026-07-15", "region": "Europe",
                "budget_level": "high", "vibes": ["culture"], "activities": ["museums"]}
ATH_FLIGHT = Flight(flight_number="KA-AMSATH", price=210.0, currency="EUR")


class FakeSpecialists(Specialists):
    """Scripted specialists; `flights` maps destination -> a Flight or None (no flight)."""

    def __init__(self, flights):
        super().__init__("d", "dd", "ff", "fb")
        self.flights, self.calls = flights, []

    async def shortlist(self, request):
        self.calls.append("shortlist")
        return DestinationResult(destinations=[ATHENS, LISBON])

    async def decide(self, request):
        self.calls.append(("decide", tuple(request.tried_iata_codes)))
        untried = [d for d in request.shortlist if d.airport_iata_codes[0] not in request.tried_iata_codes]
        return DecisionResult(selected_destination=untried[0])

    async def find_flight(self, request):
        self.calls.append(("find_flight", request.to_iata))
        return FlightResult(flight=self.flights.get(request.to_iata))

    async def book(self, request):
        self.calls.append(("book", request.to_iata))
        return BookingResult(booking={"code": "BK123", "to": request.to_iata, "status": "confirmed"})


def call(name, **args):
    return ModelResponse(parts=[ToolCallPart(name, args)])


def scripted(*steps):
    """A model that plays `steps` in order, one per model response so far in the conversation."""
    def model(messages, info):
        return steps[sum(isinstance(m, ModelResponse) for m in messages)]
    return FunctionModel(model)


BOOKABLE = scripted(
    call("get_destination_shortlist", vibes=["culture"], budget_level="high", activities=["museums"], origin="AMS"),
    call("pick_destination", shortlist=SHORTLIST, tried_iata_codes=[]),
    call("find_flight", origin="AMS", to_iata="ATH", date="2026-07-15"),
    call("book_flight", passenger_name="Maarten", from_iata="AMS", to_iata="ATH", date="2026-07-15"),
    ModelResponse(parts=[TextPart("Booked Athens.")]),
)


@pytest.fixture(autouse=True)
def stub_requirements():
    with requirements_agent.override(model=TestModel(custom_output_args=REQUIREMENTS)):
        yield


def service(flights, store=None):
    return TripService(store or MemoryStore(), FakeSpecialists(flights))


async def test_run_pauses_for_approval_before_booking():
    svc = service({"ATH": ATH_FLIGHT})
    with planner.override(model=BOOKABLE):
        out = await svc.run_trip("Plan a museum trip", "t1")
    assert out["status"] == "awaiting_approval"
    assert out["pending_approval"]["from"] == "AMS" and out["pending_approval"]["to"] == "ATH"
    assert out["booking"] is None
    assert [c["tool"] for c in out["tool_calls"]] == ["get_destination_shortlist", "pick_destination", "find_flight"]
    assert ("book", "ATH") not in svc.specialists.calls  # nothing booked yet
    assert list(await svc.pending()) == ["t1"]


async def test_approve_books_the_flight():
    svc = service({"ATH": ATH_FLIGHT})
    with planner.override(model=BOOKABLE):
        await svc.run_trip("Plan a museum trip", "t1")
        out = await svc.resume_trip("t1", approved=True)
    assert out["status"] == "booked"
    assert out["booking"]["code"] == "BK123"
    assert out["answer"] == "Booked Athens."
    assert ("book", "ATH") in svc.specialists.calls
    assert await svc.pending() == {}


async def test_reject_does_not_book():
    svc = service({"ATH": ATH_FLIGHT})
    with planner.override(model=BOOKABLE):
        await svc.run_trip("Plan a museum trip", "t1")
        out = await svc.resume_trip("t1", approved=False)
    assert out["status"] == "rejected"
    assert out["booking"] is None
    assert ("book", "ATH") not in svc.specialists.calls
    assert await svc.pending() == {}


async def test_no_flight_retries_the_next_destination_then_gives_up():
    svc = service({})  # no flights anywhere
    script = scripted(
        call("get_destination_shortlist", vibes=["culture"], budget_level="high", activities=["museums"], origin="AMS"),
        call("pick_destination", shortlist=SHORTLIST, tried_iata_codes=[]),
        call("find_flight", origin="AMS", to_iata="ATH", date="2026-07-15"),
        call("pick_destination", shortlist=SHORTLIST, tried_iata_codes=["ATH"]),
        call("find_flight", origin="AMS", to_iata="LIS", date="2026-07-15"),
        ModelResponse(parts=[TextPart("No flights available.")]),
    )
    with planner.override(model=script):
        out = await svc.run_trip("Plan a museum trip", "t2")
    assert out["status"] == "no_flight"
    assert ("decide", ("ATH",)) in svc.specialists.calls
    assert out["selected_destination"]["name"] == "Lisbon"


async def test_pending_approval_survives_a_restart():
    store = MemoryStore()
    with planner.override(model=BOOKABLE):
        await service({"ATH": ATH_FLIGHT}, store).run_trip("Plan a museum trip", "t1")
        restarted = service({"ATH": ATH_FLIGHT}, store)  # a fresh service over the same store
        assert list(await restarted.pending()) == ["t1"]
        out = await restarted.resume_trip("t1", approved=True)
    assert out["status"] == "booked"


async def test_cannot_start_a_new_request_on_a_paused_thread():
    svc = service({"ATH": ATH_FLIGHT})
    with planner.override(model=BOOKABLE):
        await svc.run_trip("Plan a museum trip", "t1")
        with pytest.raises(AwaitingApproval):
            await svc.run_trip("Another trip", "t1")


async def test_cannot_resume_a_thread_with_nothing_pending():
    with pytest.raises(NotAwaitingApproval):
        await service({}).resume_trip("nope", approved=True)


async def test_messages_round_trip_through_json():
    """What PostgresStore persists: the message history as JSON, reloaded intact."""
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_core import to_jsonable_python

    store = MemoryStore()
    svc = service({"ATH": ATH_FLIGHT}, store)
    with planner.override(model=BOOKABLE):
        await svc.run_trip("Plan a museum trip", "t1")
    messages = (await store.load("t1")).messages
    assert ModelMessagesTypeAdapter.validate_python(to_jsonable_python(messages)) == messages
