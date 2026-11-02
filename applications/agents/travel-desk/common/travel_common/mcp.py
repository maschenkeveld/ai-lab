"""MCP tool access — always through agentgateway, so every tool call is routed and observable there."""
import os

from pydantic_ai.mcp import MCPToolset

AGENTGATEWAY_URL = "http://agentgateway.lab"


def agentgateway_url() -> str:
    return os.getenv("AGENTGATEWAY_URL", AGENTGATEWAY_URL).rstrip("/")


def mcp_toolset(route: str, *, tool_error_behavior: str = "error") -> MCPToolset:
    """Toolset for the MCP server at agentgateway's `/<route>` (e.g. "mcp-flights").

    A failing MCP tool raises (`tool_error_behavior="error"`) rather than coming back as ordinary
    text the model might mistake for a result. Pass "retry" to let the model see the error and retry.
    """
    return MCPToolset(f"{agentgateway_url()}/{route.lstrip('/')}", tool_error_behavior=tool_error_behavior)  # type: ignore[arg-type]


def unwrap(raw):
    """An MCP tool result as a plain dict: FastMCP wraps non-object results as {"result": ...}."""
    if isinstance(raw, dict) and set(raw) == {"result"}:
        return raw["result"]
    return raw
