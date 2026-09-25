import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field

from .otel import setup_otel

setup_otel("mcp-flights-adapter")

FLIGHTS_BASE_URL = os.getenv(
    "FLIGHTS_BASE_URL",
    "http://rest-flights.rest-flights.svc.cluster.local:8000",
)

mcp = FastMCP("flights")


@mcp.tool
async def list_airports() -> dict[str, Any]:
    """List all airports supported by the flight pricing service, including IATA code, city, country, and coordinates."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{FLIGHTS_BASE_URL.rstrip('/')}/v1/airports")
        response.raise_for_status()
        return response.json()


@mcp.tool
async def flight_price(
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
        Field(description="Travel date in YYYY-MM-DD format."),
    ],
) -> dict[str, Any]:
    """Calculate a booking-ready flight quote with flight number, route, date, price, distance, and currency."""
    payload = {
        "from": from_iata.upper(),
        "to": to_iata.upper(),
        "date": date,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{FLIGHTS_BASE_URL.rstrip('/')}/v1/price", json=payload)
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )
