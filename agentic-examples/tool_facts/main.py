"""tool_facts: tools produce the facts, and an output validator makes sure a model cannot change them.

The pattern behind the travel desk. A model is good at judgment (which date is best, and why) and unreliable at
copying numbers. So the numbers never pass through the model: a tool computes the fares and stores them in the run's
dependencies, and an output validator overwrites whatever the model returned with those stored facts. The model's
only real contribution is `recommended_date` and `reasoning`.

    task run              # a real model quotes and recommends
    task run -- --liar    # a model that lies about every price, to show the validator putting the facts back
    task test             # the same guarantees, offline
    task evals            # score a model on the same task (-- --model <alias> to compare)

Read `agent`, `quote_dates` and `facts_win` below; that is the whole idea.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field

from common import build_model, configure_telemetry, shutdown_telemetry
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel


class FlightOption(BaseModel):
    date: str
    price: float
    currency: str = "EUR"


class Advice(BaseModel):
    options: list[FlightOption]
    recommended_date: str | None
    reasoning: str


@dataclass
class Trip:
    """What the run is about, and where the tool leaves the facts for the validator to find."""
    origin: str
    destination: str
    dates: list[str]
    quotes: list[FlightOption] = field(default_factory=list)


def fare(origin: str, destination: str, date: str) -> float:
    """A deterministic stand-in for a fare service: the same route and date always cost the same."""
    digest = hashlib.sha256(f"{origin}{destination}{date}".encode()).digest()
    return round(60 + digest[0] / 255 * 140, 2)


agent = Agent(
    output_type=Advice,
    deps_type=Trip,
    instructions=(
        "You recommend a flight date. Call quote_dates once (it takes no arguments, the trip is already known), return "
        "its quotes unchanged as options, set recommended_date to the cheapest quoted date, and explain in one sentence "
        "using the prices. Never invent dates or prices."
    ),
)


@agent.tool
def quote_dates(ctx: RunContext[Trip]) -> list[dict]:
    """Quote every candidate date of the current trip."""
    ctx.deps.quotes = [FlightOption(date=d, price=fare(ctx.deps.origin, ctx.deps.destination, d)) for d in ctx.deps.dates]
    return [q.model_dump() for q in ctx.deps.quotes]


@agent.output_validator
def facts_win(ctx: RunContext[Trip], advice: Advice) -> Advice:
    """Whatever the model says about prices, the tool's quotes are what leaves this function."""
    if not ctx.deps.quotes:
        raise ModelRetry("Call quote_dates before answering.")
    advice.options = list(ctx.deps.quotes)
    if advice.recommended_date not in {q.date for q in ctx.deps.quotes}:
        raise ModelRetry(f"recommended_date must be one of {[q.date for q in ctx.deps.quotes]}.")
    return advice


def lying_model(claimed: list[float]) -> FunctionModel:
    """A stand-in model that quotes honestly, then reports every price as 1.00 EUR, and first claims a date it never quoted."""
    def respond(messages, info):
        returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return" and p.tool_name == "quote_dates"]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("quote_dates", {})])
        content = returns[-1].content
        quotes = json.loads(content) if isinstance(content, str) else content
        dates = [q["date"] for q in quotes]  # lie about the very dates the tool quoted
        answered = any(p.part_kind == "tool-call" and p.tool_name == info.output_tools[0].name for m in messages for p in m.parts)
        options = [{"date": d, "price": 1.0} for d in dates]
        claimed.extend(o["price"] for o in options)
        recommended = dates[0] if answered else "2030-01-01"  # the first try is outside the quotes: the validator retries
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"options": options, "recommended_date": recommended, "reasoning": "Everything is 1 EUR!"})])
    return FunctionModel(respond)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--liar", action="store_true", help="use a model that lies about prices")
    parser.add_argument("--model", help="LiteLLM alias (default: LLM_MODEL or openai-gpt)")
    args = parser.parse_args()

    configure_telemetry("example-tool-facts")
    trip = Trip(origin="AMS", destination="VIE", dates=["2026-12-17", "2026-12-18", "2026-12-19"])
    claimed: list[float] = []
    model = lying_model(claimed) if args.liar else build_model(args.model)
    result = agent.run_sync("Which day should I fly?", model=model, deps=trip)

    print("Quotes from the tool (the facts):")
    for q in trip.quotes:
        print(f"  {q.date}  {q.price:.2f} {q.currency}")
    if args.liar:
        print(f"\nThe lying model claimed prices of {sorted(set(claimed))} and a date outside the quotes first.")
    print("\nWhat the agent returned:")
    for o in result.output.options:
        print(f"  {o.date}  {o.price:.2f} {o.currency}")
    print(f"  recommended: {result.output.recommended_date}\n  reasoning:   {result.output.reasoning}")
    print(f"\n{result.usage}")
    shutdown_telemetry()


if __name__ == "__main__":
    main()
