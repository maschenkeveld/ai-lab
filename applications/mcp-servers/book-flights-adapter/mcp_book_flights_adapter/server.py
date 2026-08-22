import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field


BOOK_FLIGHTS_BASE_URL = os.getenv(
    "BOOK_FLIGHTS_BASE_URL",
    "http://rest-book-flights.rest-book-flights.svc.cluster.local:8000",
)

mcp = FastMCP("book-flights")


@mcp.tool
async def create_booking(
    passenger_name: Annotated[
        str,
        Field(description="Passenger name for the booking."),
    ],
    from_iata: Annotated[
        str,
        Field(description="Three-letter origin airport IATA code, for example AMS."),
    ],
    to_iata: Annotated[
        str,
        Field(description="Three-letter destination airport IATA code, for example CDG or LHR."),
    ],
    date: Annotated[
        str,
        Field(description="Flight date in YYYY-MM-DD format."),
    ],
) -> dict[str, Any]:
    """Create a flight booking and return the booking code, flight number, passenger, route, date, and status."""
    payload = {
        "name": passenger_name,
        "from": from_iata.upper(),
        "to": to_iata.upper(),
        "date": date,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{BOOK_FLIGHTS_BASE_URL.rstrip('/')}/v1/bookings", json=payload)
        response.raise_for_status()
        return response.json()


@mcp.tool
async def list_bookings() -> dict[str, Any]:
    """List all bookings currently stored by the booking service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{BOOK_FLIGHTS_BASE_URL.rstrip('/')}/v1/bookings")
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )
