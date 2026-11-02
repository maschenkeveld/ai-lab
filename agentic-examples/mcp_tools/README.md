# mcp_tools

**Idea:** MCP (Model Context Protocol) is the open standard for giving agents tools. The server declares its tools; the
agent's `MCPToolset` lists them, hands them to the model, and sends every call back over MCP. The agent code never mentions
`list_destinations` or `flight_price`, so you can change or swap the server without touching the agent.

## Run

```bash
cd agentic-examples/mcp_tools       # its own uv project: the first run creates .venv from uv.lock
task run                            # starts server.py over stdio by itself and asks a question
task run -- "A beach trip from AMS on 2026-12-20, what is the cheapest option?"
task run -- --url http://agentgateway.lab/mcp-travel-search    # the lab's real MCP server, through agentgateway
task serve                          # run server.py as an HTTP MCP server (then: task run -- --url http://127.0.0.1:8000/mcp)
task test                           # offline: the real server in-process, a scripted model
```

The default needs only the lab's LiteLLM for the model; the bundled MCP server runs on your machine. With `--url` the
tools come from the cluster (the lab must be up). The output lists every tool call the model made, so you can see it
explore (it quotes several destinations before choosing).

## Read this

| What | Where |
|---|---|
| `MCPToolset(source)` | `build_agent` in `main.py`. `source` is a script path (stdio), a URL (HTTP) or a FastMCP instance (in-process, used by the tests) |
| Tools as plain functions | `server.py`: `list_destinations`, `flight_price`, registered with `mcp.tool(...)` |
| `tool_calls(messages)` | `main.py`: reads the run's messages to show what the model did |

## Things to try

- Add a third tool to `server.py` (say `weather(city)`), run `task run -- "…and will it be warm?"`, and see the model use it
  with no change to `main.py`.
- Run with `--url http://agentgateway.lab/mcp-travel-search`: different server, different tool names and schemas
  (`from_iata`, `to_iata`), same agent code. The model adapts.
- Point Claude Desktop at `task serve`'s URL and call the same tools by hand.
- Make a tool raise: `MCPToolset(source, tool_error_behavior="retry")` (the default) lets the model see the error and try again;
  `"error"` fails the run instead. The desk uses `"error"` for the booking agent, where a retry could be wrong.
- Remove "Never guess a price: call the tool." from the instructions and see whether a model starts guessing.

## Files

`main.py`, `server.py`, `tests/test_mcp.py`, `common.py` (model and tracing, copied into every example on purpose),
`pyproject.toml` + `uv.lock`, `Taskfile.yml`.
