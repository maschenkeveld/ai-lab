"""A tiny MCP server with two travel tools. `main.py` starts it over stdio by itself, so there is nothing to run first.

You can also run it as a normal HTTP MCP server and point an agent (or Claude Desktop) at it:

    uv run python server.py --http          # then: task run -- --url http://127.0.0.1:8000/mcp
"""
import hashlib
import sys

from fastmcp import FastMCP

mcp = FastMCP("tiny-travel")

DESTINATIONS = {
    "culture": [("Vienna", "Austria"), ("Rome", "Italy"), ("Prague", "Czechia")],
    "beach": [("Lisbon", "Portugal"), ("Barcelona", "Spain"), ("Split", "Croatia")],
    "food": [("Lyon", "France"), ("Bologna", "Italy"), ("San Sebastian", "Spain")],
}


def list_destinations(vibe: str) -> list[dict]:
    """List destinations that match a vibe: culture, beach or food."""
    return [{"name": n, "country": c} for n, c in DESTINATIONS.get(vibe.lower(), [])]


def flight_price(origin: str, destination: str, date: str) -> dict:
    """Quote a flight in EUR between two cities on a YYYY-MM-DD date. The same route and date always cost the same."""
    digest = hashlib.sha256(f"{origin}{destination}{date}".encode()).digest()
    return {"origin": origin, "destination": destination, "date": date, "price": round(60 + digest[0] / 255 * 140, 2), "currency": "EUR"}


# Registered after definition so the functions stay plain and directly callable (the tests rely on that).
mcp.tool(list_destinations)
mcp.tool(flight_price)


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
    else:
        mcp.run()  # stdio
