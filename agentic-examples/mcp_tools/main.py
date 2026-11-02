"""mcp_tools: an agent that discovers and calls MCP tools.

MCP (Model Context Protocol) is the open standard for giving agents tools. The server says what tools it has; the agent's
`MCPToolset` lists them and hands them to the model, and every call goes back over MCP. The agent code never mentions
`list_destinations` or `flight_price`: change the server and the agent sees the new tools.

    task run                                              # starts server.py over stdio and asks a question
    task run -- --url http://agentgateway.lab/mcp-travel-search    # the lab's real MCP server, through agentgateway
    task test

In the lab, the travel desk's agents reach `mcp-travel-search` and `mcp-bookings` exactly like this, through agentgateway.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import build_model, configure_telemetry, shutdown_telemetry
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset

HERE = Path(__file__).resolve().parent


class Idea(BaseModel):
    destination: str
    price_eur: float
    why: str


def build_agent(source) -> Agent:
    """`source` is anything MCPToolset accepts: a script path (stdio), a URL (HTTP), or a FastMCP instance (in-process)."""
    return Agent(
        output_type=Idea,
        toolsets=[MCPToolset(source)],
        instructions="Use the tools to find a destination that fits the vibe, then quote the flight. Never guess a price: call the tool.",
    )


def tool_calls(messages) -> list[str]:
    return [f"{p.tool_name}({p.args})" for m in messages for p in m.parts if p.part_kind == "tool-call" and p.tool_name != "final_result"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="an MCP server URL instead of the bundled stdio server")
    parser.add_argument("--model", help="LiteLLM alias (default: LLM_MODEL or openai-gpt)")
    parser.add_argument("question", nargs="?", default="I want a culture trip from AMS on 2026-12-18. What do you suggest, and what is the flight?")
    args = parser.parse_args()

    configure_telemetry("example-mcp-tools")
    agent = build_agent(args.url or str(HERE / "server.py"))
    result = agent.run_sync(args.question, model=build_model(args.model))

    print("Tool calls the model made over MCP:")
    for call in tool_calls(result.all_messages()):
        print(f"  {call}")
    print(f"\nAnswer: {result.output}")
    shutdown_telemetry()


if __name__ == "__main__":
    main()
