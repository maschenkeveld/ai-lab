"""A2A round-trip through the real FastA2A server and `call_agent`, with a stub model (no LLM needed)."""
from contextlib import asynccontextmanager

import httpx
import pytest
from fasta2a.schema import Skill
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from travel_common.a2a_client import A2AError, call_agent
from travel_common.a2a_server import create_a2a_app
from travel_common.contracts import Flight, FlightRequest, FlightResult

PREFIX = "/a2a/flight-finder"
URL = f"http://test{PREFIX}"
SKILL = Skill(id="find-flight", name="Find flight", description="d", tags=["travel"],
              input_modes=["application/json"], output_modes=["application/json"])


@asynccontextmanager
async def served(agent: Agent):
    """The agent behind the real A2A app (worker running), reached through an in-process client."""
    app = create_a2a_app(agent, name="travel-flight-finder", description="d", prefix=PREFIX,
                         public_url=URL, skills=[SKILL])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            yield client


def stub_agent() -> Agent:
    return Agent(TestModel(custom_output_args={"flight": {"flight_number": "KL1234", "price": 129.0}}),
                 output_type=FlightResult)


async def test_typed_round_trip():
    async with served(stub_agent()) as client:
        result = await call_agent(URL, FlightRequest(origin="AMS", to_iata="BCN", date="2026-12-01"),
                                  FlightResult, http_client=client)
    assert result.flight == Flight(flight_number="KL1234", price=129.0)


async def test_agent_card_and_health():
    async with served(stub_agent()) as client:
        card = (await client.get(f"{URL}/.well-known/agent-card.json")).json()
        health = (await client.get("http://test/health")).json()
    assert card["name"] == "travel-flight-finder"
    assert card["supportedInterfaces"][0]["url"] == URL
    assert health["status"] == "ok"


async def test_request_payload_reaches_the_agent():
    prompts = []

    def no_flight(messages, info):
        prompts.append(str(messages[-1].parts[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"flight": None})])

    async with served(Agent(FunctionModel(no_flight), output_type=FlightResult)) as client:
        out = await call_agent(URL, {"origin": "AMS", "to_iata": "BCN", "date": "2026-12-01"},
                               FlightResult, http_client=client)
    assert out.flight is None
    assert '"to_iata": "BCN"' in prompts[0]


async def test_agent_failure_raises():
    def boom(messages, info):
        raise RuntimeError("model down")

    async with served(Agent(FunctionModel(boom), output_type=FlightResult)) as client:
        with pytest.raises(A2AError, match="failed"):
            await call_agent(URL, {"origin": "AMS"}, FlightResult, http_client=client, timeout=5)


async def test_unreachable_agent_raises_http_error():
    with pytest.raises(httpx.HTTPError):
        await call_agent("http://127.0.0.1:9/a2a/x", {"a": 1}, timeout=2)


async def test_startup_retries_until_toolset_connects():
    from pydantic_ai.toolsets import FunctionToolset

    class FlakyToolset(FunctionToolset):
        attempts = 0

        async def __aenter__(self):
            type(self).attempts += 1
            if type(self).attempts < 3:
                raise ConnectionError("gateway not ready")
            return await super().__aenter__()

    agent = Agent(TestModel(custom_output_args={"flight": None}), output_type=FlightResult,
                  toolsets=[FlakyToolset()])
    app = create_a2a_app(agent, name="n", description="d", prefix=PREFIX, public_url=URL, skills=[SKILL],
                         startup_delay=0.01)
    async with app.router.lifespan_context(app):
        pass
    assert FlakyToolset.attempts == 3


async def test_failure_cause_reaches_the_caller():
    def boom(messages, info):
        raise RuntimeError("model down")

    async with served(Agent(FunctionModel(boom), output_type=FlightResult)) as client:
        with pytest.raises(A2AError, match="model down"):
            await call_agent(URL, {"origin": "AMS"}, FlightResult, http_client=client, timeout=5)


async def test_tools_can_read_the_request():
    from travel_common.a2a_server import current_request

    agent = Agent(TestModel(call_tools=["echo_origin"], custom_output_args={"flight": None}), output_type=FlightResult)
    seen = []

    @agent.tool_plain
    def echo_origin() -> str:
        seen.append(current_request())
        return "ok"

    async with served(agent) as client:
        await call_agent(URL, {"origin": "AMS", "to_iata": "BCN", "date": "d"}, FlightResult, http_client=client)
    assert seen == [{"origin": "AMS", "to_iata": "BCN", "date": "d"}]

