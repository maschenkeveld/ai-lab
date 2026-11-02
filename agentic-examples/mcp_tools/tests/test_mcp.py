"""The MCP wiring, offline: the real server in-process, a scripted model instead of an LLM."""
import json

from main import Idea, build_agent, tool_calls
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from server import flight_price, mcp


def test_the_server_prices_are_stable():
    first = flight_price("AMS", "Vienna", "2026-12-18")
    assert first == flight_price("AMS", "Vienna", "2026-12-18") and 60 <= first["price"] <= 200


async def test_the_agent_discovers_the_servers_tools_and_calls_them():
    def script(messages, info):
        names = {t.name for t in info.function_tools}
        assert names == {"list_destinations", "flight_price"}   # discovered over MCP, never named in main.py
        returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("flight_price", {"origin": "AMS", "destination": "Vienna", "date": "2026-12-18"})])
        price = json.loads(returns[-1].content)["price"] if isinstance(returns[-1].content, str) else returns[-1].content["price"]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"destination": "Vienna", "price_eur": price, "why": "test"})])

    agent = build_agent(mcp)  # in-process: no subprocess, no network
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("go")
    assert isinstance(result.output, Idea) and result.output.price_eur == flight_price("AMS", "Vienna", "2026-12-18")["price"]
    assert tool_calls(result.all_messages())[0].startswith("flight_price(")
