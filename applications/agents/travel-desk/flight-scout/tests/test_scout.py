"""The scout's guarantee: prices come from the quote tool, never from the model."""
import pytest
from flight_scout import server
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from travel_common import a2a_server
from travel_common.contracts import FlightOptions, FlightQuoteRequest

REQUEST = FlightQuoteRequest(origin="AMS", to_iata="VIE", dates=["2026-12-17", "2026-12-18"], preferences="weekends")
PRICES = {"2026-12-17": 140.0, "2026-12-18": 120.0}


class FakeMcp:
    calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def direct_call_tool(self, name, args):
        self.calls.append(args)
        if args["date"] not in PRICES:
            raise RuntimeError("unknown date")
        return {"flight_number": "KA-AMSVIE", "price": PRICES[args["date"]], "currency": "EUR", "priced_at": "t"}


@pytest.fixture(autouse=True)
def fake_mcp(monkeypatch):
    monkeypatch.setattr(server, "travel_search", FakeMcp())
    a2a_server._request.set(REQUEST.model_dump())


def model_that(answer: dict):
    """A model that calls quote_dates, then answers with `answer` (which may lie about the prices)."""
    def fn(messages, info):
        if not any(p.part_kind == "tool-return" for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("quote_dates", {})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])
    return FunctionModel(fn)


async def test_quotes_every_date_and_the_model_recommends():
    answer = {"options": [], "recommended_date": "2026-12-18", "reasoning": "cheapest, and a Friday"}
    with server.agent.override(model=model_that(answer)):
        out = (await server.agent.run("go")).output
    assert [o.date for o in out.options] == ["2026-12-17", "2026-12-18"]
    assert out.recommended_date == "2026-12-18"


async def test_a_model_cannot_change_prices():
    lie = {"options": [{"date": "2026-12-18", "flight_number": "KA-FAKE", "price": 1.0}], "recommended_date": "2026-12-18", "reasoning": "x"}
    with server.agent.override(model=model_that(lie)):
        out = (await server.agent.run("go")).output
    assert {o.date: o.price for o in out.options} == PRICES
    assert all(o.flight_number == "KA-AMSVIE" for o in out.options)


async def test_a_recommendation_outside_the_quotes_is_retried():
    seen = []

    def fn(messages, info):
        if not any(p.part_kind == "tool-return" for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("quote_dates", {})])
        answered = sum(1 for m in messages for p in m.parts if p.part_kind == "tool-call" and p.tool_name == info.output_tools[0].name)
        retried = answered >= 1
        seen.append(retried)
        date = "2026-12-18" if retried else "2030-01-01"
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"options": [], "recommended_date": date, "reasoning": "r"})])

    with server.agent.override(model=FunctionModel(fn)):
        out = (await server.agent.run("go")).output
    assert seen == [False, True] and out.recommended_date == "2026-12-18"


async def test_a_date_with_no_quote_is_skipped_not_fatal():
    a2a_server._request.set(FlightQuoteRequest(origin="AMS", to_iata="VIE", dates=["2026-12-18", "1999-01-01"]).model_dump())
    with server.agent.override(model=model_that({"options": [], "recommended_date": "2026-12-18", "reasoning": "r"})):
        out = (await server.agent.run("go")).output
    assert [o.date for o in out.options] == ["2026-12-18"]


async def test_no_quotes_means_no_recommendation():
    a2a_server._request.set(FlightQuoteRequest(origin="AMS", to_iata="VIE", dates=["1999-01-01"]).model_dump())
    with server.agent.override(model=model_that({"options": [], "recommended_date": "2026-12-18", "reasoning": "r"})):
        out = (await server.agent.run("go")).output
    assert isinstance(out, FlightOptions) and out.options == [] and out.recommended_date is None
