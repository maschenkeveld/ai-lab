"""booking-agent: holds, confirms and cancels bookings. It has no LLM, on purpose.

A2A in:  BookingCommand {"action": "hold"|"confirm"|"cancel"|"get", ...}
A2A out: BookingState   {"booking_code", "status": "held"|"scheduled"|"cancelled"|"expired", ...}

Moving money is the one place a model adds risk and no value, so this agent is plain code behind the same A2A
interface as the others. It reaches the booking system through the bookings MCP server (via agentgateway). The
commands are idempotent (a hold carries the caller's idempotency key; confirm and cancel are harmless to repeat),
so callers can retry safely. Confirming a hold that has expired or been cancelled returns that state, not an error.
"""
import logging
import os
from typing import Any

from travel_common.otel import setup_otel

setup_otel("booking-agent")

import uvicorn
from fasta2a.schema import Skill
from travel_common.contracts import BookingCommand, BookingState
from travel_common.function_worker import create_function_app
from travel_common.mcp import mcp_toolset, unwrap

log = logging.getLogger(__name__)

AGENT_NAME = os.getenv("AGENT_NAME", "booking-agent")
PREFIX = "/a2a/booking-agent"
PUBLIC_URL = os.getenv("PUBLIC_AGENT_BASE_URL", f"http://agentgateway.lab{PREFIX}")

bookings = mcp_toolset("mcp-bookings", tool_error_behavior="error")


def to_state(raw: dict[str, Any]) -> BookingState:
    """A booking from the REST service (via MCP) as the agent's typed state."""
    b = unwrap(raw)
    b = b.get("booking", b)
    return BookingState(booking_code=b["booking_code"], status=b["status"], flight_number=b.get("flight_number", ""),
                        origin=b.get("from", ""), to_iata=b.get("to", ""), date=b.get("date", ""),
                        hold_expires_at_ts=b.get("hold_expires_at_ts", 0), idempotent_replay=b.get("idempotent_replay", False))


async def execute(command: BookingCommand) -> BookingState:
    async with bookings:
        call = bookings.direct_call_tool
        if command.action == "hold":
            return to_state(await call("hold_booking", {
                "passenger_name": command.traveller, "from_iata": command.origin, "to_iata": command.to_iata,
                "date": command.date, "idempotency_key": command.idempotency_key, "hold_seconds": command.hold_seconds}))
        if command.action == "confirm":
            try:
                return to_state(await call("confirm_booking", {"booking_code": command.booking_code}))
            except Exception as exc:
                if "409" not in str(exc):
                    raise
                # Expired or cancelled: report what it is now so the caller can decide, instead of failing the task.
                return to_state(await call("get_booking", {"booking_code": command.booking_code}))
        if command.action == "cancel":
            return to_state(await call("cancel_booking", {"booking_code": command.booking_code}))
        return to_state(await call("get_booking", {"booking_code": command.booking_code}))


async def handle(data: dict[str, Any]) -> dict[str, Any]:
    command = BookingCommand.model_validate(data)
    if command.action == "hold" and not (command.idempotency_key and command.traveller and command.origin and command.to_iata and command.date):
        raise ValueError("a hold needs traveller, origin, to_iata, date and an idempotency_key")
    if command.action != "hold" and not command.booking_code:
        raise ValueError(f"{command.action} needs a booking_code")
    state = await execute(command)
    log.info("booking %s %s -> %s", command.action, state.booking_code, state.status)
    return state.model_dump(mode="json")


app = create_function_app(
    handle, name=AGENT_NAME, prefix=PREFIX, public_url=PUBLIC_URL,
    description="Holds, confirms and cancels flight bookings through the bookings MCP server. Deterministic: no LLM.",
    skills=[Skill(id="manage-booking", name="Manage booking", description="Hold, confirm, cancel or look up a booking.",
                  tags=["travel", "booking", "mcp", "saga"], input_modes=["application/json"], output_modes=["application/json"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4303")))
