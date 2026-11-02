"""MCP adapter for travel search: destinations and flight fares, over the two Go REST services."""
import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .otel import setup_otel

setup_otel("mcp-travel-search")

DESTINATIONS_BASE_URL = os.getenv(
    "DESTINATIONS_BASE_URL",
    "http://rest-destinations.rest-destinations.svc.cluster.local:8000",
)
FLIGHTS_BASE_URL = os.getenv(
    "FLIGHTS_BASE_URL",
    "http://rest-flights.rest-flights.svc.cluster.local:8000",
)

mcp = FastMCP("travel-search")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "mcp-travel-search"})


@mcp.tool
async def list_destinations(
    vibes: Annotated[
        list[str] | None,
        Field(description="Optional vibe filters such as city, culture, food, nature, design, relax, or nightlife."),
    ] = None,
    budget_level: Annotated[
        str | None,
        Field(description="Optional budget filter. Valid values are low, mid, and high."),
    ] = None,
    activities: Annotated[
        list[str] | None,
        Field(description="Optional activity filters such as museums, architecture, food, shopping, music, or walking."),
    ] = None,
    require_all_vibes: Annotated[
        bool,
        Field(description="When true, only return destinations that match every requested vibe."),
    ] = False,
    limit: Annotated[
        int,
        Field(description="Maximum number of destinations to return."),
    ] = 10,
) -> dict[str, Any]:
    """List travel destinations with optional vibe, activity, budget, and result-limit filters."""
    payload = {
        "vibes": vibes or [],
        "budget_level": budget_level or "",
        "activities": activities or [],
        "require_all_vibes": require_all_vibes,
        "limit": limit,
    }
    return await post_json("/v1/destinations", payload)


@mcp.tool
async def shortlist_destinations(
    query: Annotated[
        str,
        Field(description="Natural-language travel preferences to search against destination names, countries, blurbs, vibes, and activities."),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of matching destinations to return."),
    ] = 5,
) -> dict[str, Any]:
    """Search destination content and return a ranked shortlist for a travel request."""
    return await post_json("/v1/shortlist", {"query": query, "limit": limit})


@mcp.tool
async def get_destination(
    destination_id: Annotated[
        str,
        Field(description="Destination identifier, for example paris, london, madrid, or vienna."),
    ],
) -> dict[str, Any]:
    """Fetch one destination by its stable destination identifier."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{DESTINATIONS_BASE_URL.rstrip('/')}/v1/destinations/{destination_id}")
        response.raise_for_status()
        return response.json()


async def post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{DESTINATIONS_BASE_URL.rstrip('/')}{path}", json=payload)
        response.raise_for_status()
        return response.json()


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
