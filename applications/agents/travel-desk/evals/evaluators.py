"""Evaluators for the desk. Each is a pure function of one run, so a failure points at one behaviour."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_evals.evaluators import Evaluator, EvaluatorContext


@dataclass
class ExtractionMatches(Evaluator[str, dict[str, Any], Any]):
    """The extracted requirements contain what the traveller said. Scalars must be equal; lists must contain the
    expected items (extra vibes are fine, missing ones are not). Reports each field and the share that matched."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> dict[str, bool | float]:
        got, want = ctx.output, ctx.expected_output or {}
        if got.get("_blocked_by_gateway"):
            # The gateway's guardrail rejected the prompt before any model saw it. For a case built to be hostile that is a
            # correct outcome (the traveller's request was never read, so nothing wrong was extracted); for any other it is a failure.
            allowed = bool((ctx.metadata or {}).get("may_be_blocked"))
            return {"blocked_by_gateway": True, "all_fields": allowed, "field_score": 1.0 if allowed else 0.0}
        results: dict[str, bool | float] = {}
        for key, expected in want.items():
            actual = got.get(key)
            if isinstance(expected, list):
                results[f"field_{key}"] = set(expected) <= set(actual or [])
            else:
                results[f"field_{key}"] = actual == expected
        fields = [v for k, v in results.items() if k.startswith("field_")]
        results["all_fields"] = all(fields)
        results["field_score"] = sum(fields) / len(fields) if fields else 1.0
        return results


@dataclass
class OnlyAllowedValues(Evaluator[str, dict[str, Any], Any]):
    """Vibes and activities stay inside the vocabularies the downstream services accept (anything else is a 400)."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> bool:
        if ctx.output.get("_blocked_by_gateway"):
            return True  # nothing was extracted, so nothing can be out of vocabulary
        vibes = {"culture", "city", "food", "design", "nature", "relax", "nightlife", "beach"}
        activities = {"museums", "architecture", "shopping", "music", "walking"}
        return (set(ctx.output.get("vibes", [])) <= vibes and set(ctx.output.get("activities", [])) <= activities
                and ctx.output.get("budget_level") in {"low", "mid", "high"})


# ---- the live end-to-end suite: the output is {"trip": ..., "approvals": [...], "audit": [...], "decision": ...} ----


@dataclass
class PolicyRespected(Evaluator[str, dict[str, Any], Any]):
    """The number of approvals the desk demanded is what the policy says for the price."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> bool:
        policy = ctx.output["policy"]
        for approval in ctx.output["approvals"]:
            price = approval["proposal"]["price"]
            expected = 0 if price < policy["below"] else 2 if price >= policy["second"] else 1
            if approval["required"] != expected:
                return False
        return True


@dataclass
class ProposalMatchesRequest(Evaluator[str, dict[str, Any], Any]):
    """The proposal flies from the requested origin to a destination that is not the origin."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> bool:
        proposal = ctx.output["trip"].get("proposal") or {}
        return bool(proposal) and proposal["origin"] == ctx.metadata["origin"] and proposal["to_iata"] != proposal["origin"]


@dataclass
class OutcomeMatchesDecision(Evaluator[str, dict[str, Any], Any]):
    """Approved -> a confirmed booking. Rejected -> no booking left: the hold was released at the supplier."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> bool:
        trip = ctx.output["trip"]
        booking = trip.get("booking") or {}
        if ctx.metadata["decision"] == "approve":
            return trip["status"] == "booked" and booking.get("status") == "scheduled"
        return trip["status"] == "rejected" and booking.get("status") == "cancelled"


@dataclass
class AuditTrailComplete(Evaluator[str, dict[str, Any], Any]):
    """Every step that mattered left a record, in order, and every human decision names who made it."""

    def evaluate(self, ctx: EvaluatorContext[str, dict[str, Any], Any]) -> bool:
        audit = ctx.output["audit"]
        kinds = [e["type"] for e in audit]
        needed = ["trip_requested", "requirements_extracted", "research_done", "booking_hold"]
        if not all(k in kinds for k in needed) or kinds.index("trip_requested") != 0:
            return False
        human = [e for e in audit if e["type"].startswith("approval_") and e["type"] not in ("approval_requested", "approval_escalated", "approval_expired")]
        # every human decision names a real person (from their token), and never the person who requested the trip
        requester = ctx.output["trip"]["requested_by"]
        return all(e["actor"] not in ("system", "", requester) for e in human)
