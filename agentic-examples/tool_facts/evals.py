"""Evals for tool_facts: does a model quote, recommend the cheapest date, and leave the prices alone?

    task evals                              # the default model
    task evals -- --llm gemini              # compare another model

Two datasets run: `honest` uses a real model on three trips; `guard` uses the lying model from main.py and must
still pass, which is the proof that the validator, not the model, owns the prices.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from common import build_model, configure_telemetry, shutdown_telemetry
from main import Trip, agent, fare, lying_model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

Out = dict[str, Any]
CASES = [
    Case(name="three-days", inputs={"origin": "AMS", "destination": "VIE", "dates": ["2026-12-17", "2026-12-18", "2026-12-19"]}),
    Case(name="two-days", inputs={"origin": "AMS", "destination": "MAD", "dates": ["2026-12-20", "2026-12-21"]}),
    Case(name="five-days", inputs={"origin": "BRU", "destination": "LIS", "dates": [f"2026-12-{d}" for d in range(10, 15)]}),
]


@dataclass
class PricesAreFacts(Evaluator[dict, Out, Any]):
    """Every price in the answer is the price the tool computed."""

    def evaluate(self, ctx: EvaluatorContext[dict, Out, Any]) -> bool:
        i = ctx.inputs
        return all(o["price"] == fare(i["origin"], i["destination"], o["date"]) for o in ctx.output["options"])


@dataclass
class RecommendsTheCheapest(Evaluator[dict, Out, Any]):
    """The recommended date is the cheapest one (the instructions ask for that, so a miss is a finding about the model)."""

    def evaluate(self, ctx: EvaluatorContext[dict, Out, Any]) -> bool:
        i = ctx.inputs
        cheapest = min(i["dates"], key=lambda d: fare(i["origin"], i["destination"], d))
        return ctx.output["recommended_date"] == cheapest


def task_with(model):
    async def task(inputs: dict) -> Out:
        trip = Trip(**inputs)
        result = await agent.run("Which day should I fly?", model=model, deps=trip)
        return result.output.model_dump()
    return task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llm", choices=["openai", "gemini", "ollama", "local"], help="which model, by gateway route (default: openai)")
    args = parser.parse_args()
    configure_telemetry("example-tool-facts-evals")

    ok = True
    print("\n### honest: a real model")
    honest = Dataset(name="honest", cases=CASES, evaluators=[PricesAreFacts(), RecommendsTheCheapest()])
    report = honest.evaluate_sync(task_with(build_model(args.llm)), max_concurrency=2)
    report.print(include_input=False, include_output=False)
    ok &= all(a.value for c in report.cases for a in c.assertions.values())

    print("\n### guard: a model that lies about every price (only PricesAreFacts applies)")
    guard = Dataset(name="guard", cases=CASES[:2], evaluators=[PricesAreFacts()])
    report = guard.evaluate_sync(task_with(lying_model([])), max_concurrency=1)
    report.print(include_input=False, include_output=False)
    ok &= all(a.value for c in report.cases for a in c.assertions.values())
    shutdown_telemetry()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
