"""Run, pause and resume trip-planning conversations.

A run ends one of two ways: the planner answers (`completed`), or it reaches `book_flight` and the run ends
with a deferred tool request (`awaiting_approval`). The thread's message history is saved either way, so a
later `approve`/`reject` resumes the very same conversation from the stored state.
"""
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import (
    AgentRunResult,
    AgentRunResultEvent,
    DeferredToolRequests,
    DeferredToolResults,
    ToolDenied,
)
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    TextPartDelta,
    ToolReturnPart,
)

from .models import TripRequirements
from .planner import Specialists, planner, requirements_agent
from .store import AWAITING_APPROVAL, Store, ThreadState

log = logging.getLogger(__name__)

TOOLS = ("get_destination_shortlist", "pick_destination", "find_flight", "book_flight")


class NotAwaitingApproval(Exception):
    """There is no booking waiting for a decision on this thread."""


class AwaitingApproval(Exception):
    """The thread is paused on a booking; decide it before sending a new request."""


class TripService:
    def __init__(self, store: Store, specialists: Specialists) -> None:
        self.store = store
        self.specialists = specialists

    # -- entry points -------------------------------------------------------------------------------

    async def run_trip(self, prompt: str, thread_id: str) -> dict[str, Any]:
        """Extract requirements, run the planner until it answers or pauses for approval."""
        state = await self._begin(prompt, thread_id)
        user_prompt = json.dumps({"trip_request": prompt, "requirements": state.requirements})
        result = await planner.run(user_prompt, deps=self.specialists, message_history=state.messages)
        return await self._finish(state, result)

    async def resume_trip(self, thread_id: str, approved: bool) -> dict[str, Any]:
        """Resume a run paused at `book_flight` with a human's decision."""
        state, results = await self._decision(thread_id, approved)
        result = await planner.run(None, deps=self.specialists, message_history=state.messages,
                                   deferred_tool_results=results)
        return await self._finish(state, result, rejected=not approved)

    async def stream_trip(self, prompt: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
        """The same run as `run_trip`, as a stream of events (tokens, tool start/end), ending with the summary."""
        state = await self._begin(prompt, thread_id)
        user_prompt = json.dumps({"trip_request": prompt, "requirements": state.requirements})
        async for event in planner.run_stream_events(user_prompt, deps=self.specialists, message_history=state.messages):
            if isinstance(event, AgentRunResultEvent):
                summary = await self._finish(state, event.result)
                yield {"event": "awaiting_approval" if summary["status"] == AWAITING_APPROVAL else "result", **summary}
            elif (out := serialize_event(event)) is not None:
                yield out

    async def pending(self) -> dict[str, dict[str, Any]]:
        return await self.store.pending()

    # -- internals ----------------------------------------------------------------------------------

    async def _begin(self, prompt: str, thread_id: str) -> ThreadState:
        state = await self.store.load(thread_id) or ThreadState(thread_id)
        if state.status == AWAITING_APPROVAL:
            raise AwaitingApproval(thread_id)
        state.requirements = (await requirements_agent.run(prompt.strip())).output.model_dump()
        log.info("[%s] requirements: %s", thread_id, state.requirements)
        return state

    async def _decision(self, thread_id: str, approved: bool) -> tuple[ThreadState, DeferredToolResults]:
        state = await self.store.load(thread_id)
        if state is None or state.status != AWAITING_APPROVAL or not state.pending:
            raise NotAwaitingApproval(thread_id)
        call_id = state.pending["tool_call_id"]
        decision = True if approved else ToolDenied("The human reviewer rejected this booking; it was not made.")
        return state, DeferredToolResults(approvals={call_id: decision})

    async def _finish(self, state: ThreadState, result: AgentRunResult, rejected: bool = False) -> dict[str, Any]:
        state.messages = result.all_messages()
        if isinstance(result.output, DeferredToolRequests):
            state.status, state.pending = AWAITING_APPROVAL, pending_from(result.output)
            summary = summarize_pending(state)
        else:
            state.status, state.pending = "completed", None
            summary = summarize_result(state, str(result.output), rejected)
        await self.store.save(state)
        log.info("[%s] status=%s", state.thread_id, summary["status"])
        return summary


# -- shaping results ----------------------------------------------------------------------------------

def pending_from(requests: DeferredToolRequests) -> dict[str, Any]:
    """The booking waiting for a human, in the shape `GET /pending` and approve_cli.py show."""
    call = requests.approvals[0]
    args = call.args_as_dict()
    return {
        "action": "approve_booking",
        "tool_call_id": call.tool_call_id,
        "passenger_name": args.get("passenger_name"),
        "from": args.get("from_iata"),
        "to": args.get("to_iata"),
        "date": args.get("date"),
    }


def scan_tool_calls(messages: list[ModelMessage]) -> dict[str, Any]:
    """Each tool's last result and the whole tool-call sequence, read back out of the message history.

    The history is the agent's state, so everything a caller needs to know about a run — including the eval
    harness — is derivable from it, rather than each tool pushing results somewhere else.
    """
    found: dict[str, Any] = {"shortlist": None, "selected_destination": None, "flight": None, "booking": None}
    rejected = False
    tool_calls: list[dict[str, Any]] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if not isinstance(part, ToolReturnPart) or part.tool_name not in TOOLS:
                continue
            payload = part.content if isinstance(part.content, dict) else None
            tool_calls.append({"tool": part.tool_name, "output": payload})
            if part.tool_name == "book_flight" and getattr(part, "outcome", "success") == "denied":
                rejected = True
            if payload is None:
                continue
            if part.tool_name == "get_destination_shortlist":
                found["shortlist"] = payload.get("destinations")
            elif part.tool_name == "pick_destination":
                found["selected_destination"] = payload.get("selected_destination")
            elif part.tool_name == "find_flight":
                found["flight"] = payload.get("flight")
            elif part.tool_name == "book_flight":
                found["booking"] = payload.get("booking")
    return {**found, "rejected": rejected, "tool_calls": tool_calls}


def summarize_result(state: ThreadState, answer: str, rejected: bool = False) -> dict[str, Any]:
    scan = scan_tool_calls(state.messages)
    if scan["booking"] is not None:
        status = "booked"
    elif rejected or scan["rejected"]:
        status = "rejected"
    elif scan["shortlist"] is not None:
        status = "no_flight"
    else:
        status = "failed"
    return {
        "answer": answer, "requirements": state.requirements, "shortlist": scan["shortlist"],
        "selected_destination": scan["selected_destination"], "flight": scan["flight"], "booking": scan["booking"],
        "status": status, "tool_calls": scan["tool_calls"], "pending_approval": None,
    }


def summarize_pending(state: ThreadState) -> dict[str, Any]:
    scan = scan_tool_calls(state.messages)
    return {
        "answer": "Awaiting human approval before booking — see pending_approval for details.",
        "requirements": state.requirements, "shortlist": scan["shortlist"],
        "selected_destination": scan["selected_destination"], "flight": scan["flight"], "booking": None,
        "status": AWAITING_APPROVAL, "tool_calls": scan["tool_calls"], "pending_approval": state.pending,
    }


def serialize_event(event: Any) -> dict[str, Any] | None:
    """A stream event as JSON for SSE, using the event names the previous orchestrator emitted."""
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return {"event": "on_chat_model_stream", "name": "travel-orchestrator", "content": event.delta.content_delta}
    if isinstance(event, FunctionToolCallEvent):
        return {"event": "on_tool_start", "name": event.part.tool_name, "input": event.part.args_as_dict()}
    if isinstance(event, FunctionToolResultEvent):
        return {"event": "on_tool_end", "name": event.result.tool_name, "output": event.result.content}
    return None


def new_thread_id() -> str:
    return str(uuid.uuid4())


__all__ = ["AwaitingApproval", "NotAwaitingApproval", "TripRequirements", "TripService", "new_thread_id"]
