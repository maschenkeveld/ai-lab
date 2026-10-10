# Travel agency LangGraph agents

Five Python microservices implementing the travel-agency multi-agent workflow, talking to each
other over A2A JSON-RPC through agentgateway. See [AGENTS.md](../../../AGENTS.md) for the full
file layout and Taskfile commands, and
[docs/langchain-langgraph-langsmith.md](../../../docs/langchain-langgraph-langsmith.md) for a deep
dive on the LangChain/LangGraph/LangSmith wiring.

| Service | Port | What it does |
|---|---|---|
| `orchestrator/` | 4200 | A LangChain v1 tool-calling agent (`create_agent`, running on the LangGraph runtime) that extracts trip requirements, then calls the four specialists below as tools — retrying with a growing exclusion list until a flight is found and booked. Conversation state persists per `thread_id` via an `AsyncPostgresSaver` checkpointer. |
| `specialists/destination/` | 4201 | Shortlists destinations via the `list_destinations` MCP tool, then re-ranks them by embedding similarity to the trip request. |
| `specialists/destination-decision/` | 4202 | Picks one destination from the shortlist via the dice-roller MCP tool (`roll-20`), excluding already-tried candidates. |
| `specialists/flight-finder/` | 4203 | Looks up a flight via the `flight_price` MCP tool. |
| `specialists/flight-booker/` | 4204 | Books the flight via the `create_booking` MCP tool. |

Only the orchestrator builds a LangGraph graph. Each specialist is a plain FastAPI service that
calls MCP tools through `langchain-mcp-adapters`' `MultiServerMCPClient` — none of them build a
graph of their own. Prefer **LangChain/LangGraph orchestrator + A2A specialist services** over
calling all five "LangGraph agents".

Each service exposes:

- `/.well-known/agent-card.json`
- `/a2a/jsonrpc`
- `/health`

The orchestrator additionally exposes `/plan` and `/plan/stream`.

LangSmith tracing (`@traceable`, plus `langsmith.run_helpers.tracing_context` to propagate trace
headers across each A2A hop) is wired into all five services, so a `/plan` call produces one
nested trace tree rather than five disconnected ones — see
[docs/langchain-langgraph-langsmith.md](../../../docs/langchain-langgraph-langsmith.md) for the
mechanics.

Run a service locally (each is its own `uv`-managed package — this example is the orchestrator):

```bash
cd orchestrator
uv sync
uv run python -m travel_orchestrator.server
```

Offline evaluation of the orchestrator (a LangSmith dataset plus deterministic and LLM-judge
evaluators) lives in `evals/` — see `evals/run_eval.py`'s docstring for usage.
