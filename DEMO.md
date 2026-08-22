# OSS AI Lab — Demo Guide

## Setting the scene

A fully local AI platform running on a Kind cluster, built entirely from open-source components. It demonstrates multi-provider LLM routing, MCP tool exposure, and a multi-agent travel workflow — all on a laptop.

The travel agency runs as **5 separate Python microservices** communicating over A2A JSON-RPC through agentgateway:

```
Client → agentgateway → orchestrator (LangGraph)
                          → agentgateway → destination        → Traefik → REST API
                          → agentgateway → destination-decision
                          → agentgateway → flight-finder      → Traefik → REST API
                          → agentgateway → flight-booker      → Traefik → REST API
```

---

## Before you start

Bring the lab up if it isn't already running:

```bash
cd k8s
task up
```

This takes a few minutes on first run (image pulls). Subsequent runs are fast.

---

## Sanity checks

Run these before demoing. Everything should return a response without errors.

**REST backends via Traefik:**

```bash
curl http://traefik-api-gw.lab/flights/health
curl http://traefik-api-gw.lab/destinations/health
curl http://traefik-api-gw.lab/book-flights/health
```

**LiteLLM — confirm models are loaded:**

```bash
curl -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models
```

Expected: a list with `openai-gpt`, `gemini-pro`, `ollama-llama`.

**All 5 agent cards reachable through agentgateway:**

```bash
curl http://agentgateway.lab/a2a/travel-orchestrator/.well-known/agent-card.json
curl http://agentgateway.lab/a2a/destination/.well-known/agent-card.json
curl http://agentgateway.lab/a2a/destination-decision/.well-known/agent-card.json
curl http://agentgateway.lab/a2a/flight-finder/.well-known/agent-card.json
curl http://agentgateway.lab/a2a/flight-booker/.well-known/agent-card.json
```

Each should return a JSON agent card with `name`, `description`, and `skills`.

**Kafka accessible from Mac terminal:**

```bash
kafka-topics --bootstrap-server kafka-direct.lab:9092 --list
```

**MCP tools reachable:**

```bash
# Connect an MCP client (e.g. Claude Desktop) to these endpoints:
http://agentgateway.lab/mcp-dice-roller
http://agentgateway.lab/mcp-destinations
http://agentgateway.lab/mcp-flights
http://agentgateway.lab/mcp-book-flights
```

---

## Demo 1 — LLM Gateway (LiteLLM)

**What to show:** LiteLLM as an OpenAI-compatible proxy in front of multiple providers. Clients send standard OpenAI requests; LiteLLM routes to the right backend.

**Show the configured models:**

```bash
curl -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models
```

**Send a chat request to OpenAI:**

```bash
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai-gpt",
    "messages": [{"role": "user", "content": "What does an API gateway do? Three words."}]
  }'
```

**Same request to Gemini:**

```bash
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-pro",
    "messages": [{"role": "user", "content": "What does an API gateway do? Three words."}]
  }'
```

**Same request to local Ollama:**

```bash
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ollama-llama",
    "messages": [{"role": "user", "content": "What does an API gateway do? Three words."}]
  }'
```

Same request body, three different providers. The client doesn't need to know which model or provider is behind each alias.

---

## Demo 2 — MCP tools via agentgateway

**What to show:** agentgateway exposes MCP servers as tools that any MCP client can discover and call.

**Connect Claude Desktop (or any MCP client) to:**

```
http://agentgateway.lab/mcp-flights
```

The client will discover and list available tools. You can invoke `search_flights` directly from the MCP client without writing any agent code.

**To verify the tool list from the CLI:**

```bash
curl -X POST http://agentgateway.lab/mcp-flights \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

**All four MCP backends:**

| MCP endpoint | Tools |
| --- | --- |
| `agentgateway.lab/mcp-flights` | `search_flights` |
| `agentgateway.lab/mcp-destinations` | `get_destinations` |
| `agentgateway.lab/mcp-book-flights` | `book_flight`, `list_bookings`, `cancel_booking` |
| `agentgateway.lab/mcp-dice-roller` | `roll_dice` |

The backends are plain REST services — no MCP code in them. agentgateway handles the protocol conversion.

---

## Demo 3 — A2A multi-agent workflow (LangGraph)

**What to show:** five independent Python microservices, each with an A2A endpoint, coordinated by a LangGraph orchestrator through agentgateway.

### Step 1 — Discover the agents

Each service publishes a standard A2A agent card:

```bash
curl http://agentgateway.lab/a2a/travel-orchestrator/.well-known/agent-card.json | jq .
```

The orchestrator describes its `plan-trip` skill and references its public URL through agentgateway — not its internal pod address. agentgateway is the only thing external callers ever see.

### Step 2 — Trigger the workflow

```bash
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/plan \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Plan a high budget museum trip for Maarten from AMS on 2026-07-15"}' \
  | jq .
```

The response includes:
- `shortlist` — 5 candidate destinations
- `selected_destination` — the one chosen
- `flight` — found flight details
- `booking` — confirmed booking code
- `answer` — human-readable summary

### Step 3 — Show what happened under the hood

The orchestrator made **4 sequential A2A calls** through agentgateway:

```
orchestrator → agentgateway/a2a/destination         → got shortlist of 5
orchestrator → agentgateway/a2a/destination-decision → picked one
orchestrator → agentgateway/a2a/flight-finder        → found flight
orchestrator → agentgateway/a2a/flight-booker        → confirmed booking
```

Call a specialist directly to make it concrete:

```bash
curl -X POST http://agentgateway.lab/a2a/destination/a2a/jsonrpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": "1", "method": "tasks/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "{\"vibes\":[\"culture\",\"city\"],\"budget_level\":\"high\",\"activities\":[\"museums\"],\"limit\":5,\"origin\":\"AMS\"}"}]
      }
    }
  }' | jq '.result.parts[0].text | fromjson'
```

### Step 4 — Show the retry loop (optional)

The orchestrator retries with a different destination if flight-finder returns nothing. This is a conditional edge in the LangGraph graph — not a linear pipeline. Open `applications/agents/travel-agency-langgraph/orchestrator/travel_orchestrator/graph.py` and walk through `route_after_flight` and the `add_conditional_edges` call in `build_graph()`.

---

## Demo 4 — LLM Analytics

LiteLLM emits usage events to Kafka. The ingestor picks them up, generates embeddings, and stores them in Postgres. The analyzer clusters similar prompts and generates plugin recommendations. The React UI shows the results.

**Open the UI:**

```
http://analytics.lab
```

**Send a batch of similar prompts to build up cluster data:**

```bash
for q in \
  "What is 2+2?" \
  "What color is the sky?" \
  "What is the capital of France?" \
  "What year was Python created?" \
  "Is water wet?"; do
  curl -s http://litellm.lab/v1/chat/completions \
    -H "Authorization: Bearer sk-ai-lab-litellm" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"openai-gpt\",\"messages\":[{\"role\":\"user\",\"content\":\"$q\"}]}" > /dev/null
done
```

**Force the analyzer to run immediately:**

```bash
kubectl create job --from=cronjob/llm-analyzer manual-run-$(date +%s) -n llm-analytics
```

Then reload `http://analytics.lab` to see clusters and recommendations.

---

## Observability

### Grafana — `http://grafana.lab:3000`

Default credentials: `admin / admin`

| Source | What you see |
| --- | --- |
| **Explore → Loki** | Logs from all pods |
| **Explore → Tempo** | Distributed traces |
| **Explore → Prometheus** | Traefik request metrics, LiteLLM metrics |

**Useful Loki queries:**

```logql
# All travel agent logs
{namespace="travel-agency-langgraph"}

# Just the orchestrator
{namespace="travel-agency-langgraph", app="travel-orchestrator-langgraph"}

# Errors across the lab
{namespace=~"travel-agency-langgraph|litellm|agentgateway-system"} |= "error"
```

### LangSmith (if API key is configured)

Go to [smith.langchain.com](https://smith.langchain.com) → project `ai-lab-oss`.

Each call to `graph.invoke()` appears as a trace with:
- One row per node execution (`parse_requirements`, `call_destination`, etc.)
- Inputs and outputs for each node
- Total latency breakdown
- The retry loop visible as repeated `call_destination_decision` → `call_flight_finder` rows if a flight wasn't found on the first pick

### Kafka UI — `http://kafka-ui.lab`

Shows topic list, message counts, and live messages on `llm-usage`.

---

## Useful commands

```bash
# Watch all travel agent pods
kubectl get pods -n travel-agency-langgraph -w

# Logs from a specific specialist
kubectl logs -n travel-agency-langgraph -l app=travel-flight-finder -f

# Restart the orchestrator after a code change
kubectl rollout restart deployment/travel-orchestrator-langgraph -n travel-agency-langgraph

# Rebuild and redeploy just the LangGraph services
cd k8s && task oss:langgraph-apply

# Check agentgateway routes
kubectl get httproute -A

# Check all pods are healthy
kubectl get pods -A | grep -v Running | grep -v Completed
```
