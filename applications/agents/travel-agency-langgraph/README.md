# Travel agency LangGraph agents

This is the Python starting point for the OSS agent runtime.

It exposes a small A2A-compatible public surface:

- `/.well-known/agent-card.json`
- `/a2a/jsonrpc`
- `/health`

The first implementation is a minimal FastAPI + LangGraph skeleton. It proves the A2A HTTP shape and gives the next phase a place to add MCP tool calls, LiteLLM-backed planning, and Kafka triggers.

Run locally:

```bash
pip install .
python -m travel_agency_langgraph.server
```
