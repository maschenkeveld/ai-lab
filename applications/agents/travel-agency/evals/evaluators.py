"""Deterministic checks on one orchestrator run (plan, then the human decision).

The task output is {"plan": <POST /plan response>, "final": <POST /approve response or None>}. Each evaluator
is a pure function of that output and the case, so failures point at one specific behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

Ctx = EvaluatorContext[str, dict[str, Any], dict[str, Any]]


def _airports(destination: dict[str, Any] | None) -> list[str]:
    return (destination or {}).get("airport_iata_codes", [])


@dataclass
class RequirementsMatch(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """The extracted origin and date are the ones the traveller gave."""

    def evaluate(self, ctx: Ctx) -> bool:
        requirements = ctx.output["plan"].get("requirements") or {}
        expected = ctx.expected_output or {}
        return all(requirements.get(key) == value for key, value in expected.items())


@dataclass
class ShortlistExcludesOrigin(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """No shortlisted destination is served by the airport the traveller leaves from."""

    def evaluate(self, ctx: Ctx) -> bool:
        origin = (ctx.output["plan"].get("requirements") or {}).get("origin")
        return all(origin not in _airports(d) for d in ctx.output["plan"].get("shortlist") or [])


@dataclass
class SelectionFromShortlist(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """The chosen destination is one of the shortlisted ones, not something the model made up."""

    def evaluate(self, ctx: Ctx) -> bool:
        plan = ctx.output["plan"]
        selected = plan.get("selected_destination")
        if selected is None:
            return True
        return selected.get("name") in {d.get("name") for d in plan.get("shortlist") or []}


@dataclass
class PendingMatchesFlight(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """The booking waiting for approval is for the selected destination, from the requested origin and date."""

    def evaluate(self, ctx: Ctx) -> bool:
        plan = ctx.output["plan"]
        pending = plan.get("pending_approval")
        if not pending:
            return plan.get("status") != "awaiting_approval"
        requirements = plan.get("requirements") or {}
        return (
            pending.get("from") == requirements.get("origin")
            and pending.get("date") == requirements.get("date")
            and pending.get("to") in _airports(plan.get("selected_destination"))
        )


@dataclass
class DecisionHonored(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """Approving books (with a 6-character code); rejecting books nothing. Without a pause nothing is booked."""

    def evaluate(self, ctx: Ctx) -> bool:
        plan, final = ctx.output["plan"], ctx.output["final"]
        if plan.get("status") != "awaiting_approval":
            return plan.get("booking") is None
        decision = ctx.metadata["decision"]
        if decision == "approve":
            code = ((final or {}).get("booking") or {}).get("booking_code", "")
            return (final or {}).get("status") == "booked" and len(code) == 6
        return (final or {}).get("status") == "rejected" and (final or {}).get("booking") is None


@dataclass
class NothingBookedBeforeApproval(Evaluator[str, dict[str, Any], dict[str, Any]]):
    """The plan response itself never carries a booking: the irreversible step waits for a human."""

    def evaluate(self, ctx: Ctx) -> bool:
        return ctx.output["plan"].get("booking") is None
