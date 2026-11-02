"""The A2A round trip, offline: the real scout server in-process, a stub model instead of an LLM."""
from contextlib import asynccontextmanager

import httpx
import pytest
from fasta2a.client import A2AClient
from main import ask
from pydantic_ai.models.test import TestModel
from scout import FACTS, build_app, build_scout


@asynccontextmanager
async def served(model):
    app = build_app(build_scout(model))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://scout") as http:
            yield http


async def test_the_agent_card_describes_the_scout():
    async with served(TestModel(custom_output_text="x")) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert card["name"] == "scout" and card["skills"][0]["id"] == "city-facts"


async def test_ask_sends_a_task_and_returns_the_answer():
    async with served(TestModel(call_tools=["facts_about"], custom_output_text="Vienna has coffee houses.")) as http:
        answer = await ask(A2AClient("http://scout", http_client=http), "Tell me about Vienna")
    assert answer == "Vienna has coffee houses."


async def test_a_failing_scout_raises_with_its_state():
    from pydantic_ai.models.function import FunctionModel

    def boom(messages, info):
        raise RuntimeError("model down")

    async with served(FunctionModel(boom)) as http:
        with pytest.raises(RuntimeError, match="failed"):
            await ask(A2AClient("http://scout", http_client=http), "anything", timeout=5)


def test_the_scouts_facts_come_from_the_tool_not_the_model():
    assert "Vienna" in FACTS["vienna"] and "Lisbon" in FACTS["lisbon"]
