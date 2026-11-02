"""The guarantees of tool_facts, offline (no model, no network)."""
import pytest
from main import Trip, agent, fare, lying_model
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel


def trip() -> Trip:
    return Trip(origin="AMS", destination="VIE", dates=["2026-12-18", "2026-12-19"])


def test_the_fare_is_deterministic_and_plausible():
    assert fare("AMS", "VIE", "2026-12-18") == fare("AMS", "VIE", "2026-12-18")
    assert 60 <= fare("AMS", "VIE", "2026-12-18") <= 200


def test_a_lying_model_cannot_change_a_price():
    t, claimed = trip(), []
    with agent.override(model=lying_model(claimed)):
        out = agent.run_sync("go", deps=t).output
    assert claimed and set(claimed) == {1.0}                         # it did lie
    assert [o.price for o in out.options] == [fare("AMS", "VIE", d) for d in t.dates]   # the facts won
    assert out.recommended_date == "2026-12-18"                       # the out-of-range first claim was retried


def test_answering_without_quoting_is_refused():
    def never_quotes(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"options": [], "recommended_date": "2026-12-18", "reasoning": "x"})])

    with agent.override(model=FunctionModel(never_quotes)):
        with pytest.raises(UnexpectedModelBehavior):
            agent.run_sync("go", deps=trip())
