import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field

from .otel import setup_otel

setup_otel("mcp-destinations-adapter")

DESTINATIONS_BASE_URL = os.getenv(
    "DESTINATIONS_BASE_URL",
    "http://rest-destinations.rest-destinations.svc.cluster.local:8000",
)

mcp = FastMCP("destinations")


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


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )
