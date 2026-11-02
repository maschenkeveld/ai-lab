"""MCP adapter for the flight booking REST API: a booking is held, then confirmed or cancelled."""
import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .otel import setup_otel

setup_otel("mcp-bookings")

BOOK_FLIGHTS_BASE_URL = os.getenv(
    "BOOK_FLIGHTS_BASE_URL",
    "http://rest-book-flights.rest-book-flights.svc.cluster.local:8000",
)

mcp = FastMCP("bookings")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "mcp-bookings"})


def _url(path: str = "") -> str:
    return f"{BOOK_FLIGHTS_BASE_URL.rstrip('/')}/v1/bookings{path}"


async def _send(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()


@mcp.tool
async def hold_booking(
    passenger_name: Annotated[str, Field(description="Passenger name for the booking.")],
    from_iata: Annotated[str, Field(description="Three-letter origin airport IATA code, for example AMS.")],
    to_iata: Annotated[str, Field(description="Three-letter destination airport IATA code, for example VIE.")],
    date: Annotated[str, Field(description="Flight date in YYYY-MM-DD format.")],
    idempotency_key: Annotated[
        str,
        Field(description="A unique key for this booking attempt. Retrying with the same key returns the same hold instead of creating a second one."),
    ],
    hold_seconds: Annotated[int, Field(description="How long the hold lasts before it expires. Default 900.")] = 900,
) -> dict[str, Any]:
    """Reserve a flight without confirming it. The result has status "held" and an expiry; confirm or cancel it afterwards."""
    return await _send("POST", _url(), json={
        "name": passenger_name, "from": from_iata.upper(), "to": to_iata.upper(), "date": date,
        "hold": True, "hold_seconds": hold_seconds, "idempotency_key": idempotency_key,
    })


@mcp.tool
async def confirm_booking(
    booking_code: Annotated[str, Field(description="The booking code returned by hold_booking.")],
) -> dict[str, Any]:
    """Confirm a held booking (status becomes "scheduled"). Fails if the hold expired or was cancelled; confirming twice is harmless."""
    return await _send("POST", _url(f"/{booking_code}/confirm"))


@mcp.tool
async def cancel_booking(
    booking_code: Annotated[str, Field(description="The booking code to cancel.")],
) -> dict[str, Any]:
    """Cancel a held or confirmed booking (status becomes "cancelled"). Cancelling twice is harmless."""
    return await _send("POST", _url(f"/{booking_code}/cancel"))


@mcp.tool
async def get_booking(
    booking_code: Annotated[str, Field(description="The booking code to look up.")],
) -> dict[str, Any]:
    """Fetch one booking with its current status (held, scheduled, cancelled or expired)."""
    return await _send("GET", _url(f"/{booking_code}"))


@mcp.tool
async def list_bookings() -> dict[str, Any]:
    """List all bookings currently stored by the booking service."""
    return await _send("GET", _url())


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )
