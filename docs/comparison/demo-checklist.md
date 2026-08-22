# OSS demo checklist

Run these checks after `cd k8s && task up`.

| Surface | Command | Expected result |
| --- | --- | --- |
| REST flights health | `curl http://traefik-api-gw.lab/flights/health` | Healthy response |
| REST destinations health | `curl http://traefik-api-gw.lab/destinations/health` | Healthy response |
| REST booking health | `curl http://traefik-api-gw.lab/book-flights/health` | Healthy response |
| LiteLLM model list | `curl -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models` | Configured LiteLLM models |
| LangGraph agent card | `curl http://agentgateway.lab/a2a/travel-orchestrator/.well-known/agent-card.json` | Agent card JSON |
| LangGraph travel workflow | `curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/plan -H 'content-type: application/json' -d '{"prompt":"Plan a high budget museum trip for Maarten from AMS on 2026-07-15"}'` | Shortlist, selected destination, flight, and booking |
| Direct Kafka | `kafka-topics --bootstrap-server kafka-direct.lab:9092 --list` | Topic list |
| MCP dice tools | connect an MCP client to `http://agentgateway.lab/mcp-dice-roller` | Dice tools are listed |
| MCP destination tools | connect an MCP client to `http://agentgateway.lab/mcp-destinations` | Destination tools are listed |
| MCP flight tools | connect an MCP client to `http://agentgateway.lab/mcp-flights` | Flight tools are listed |
| MCP booking tools | connect an MCP client to `http://agentgateway.lab/mcp-book-flights` | Booking tools are listed |
