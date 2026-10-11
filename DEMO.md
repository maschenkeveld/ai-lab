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

**Tip for any live demo:** every agent service logs its own activity to stdout as it
happens — requirements extracted, each tool call and result, final status — not just the
final JSON response. Keep `kubectl logs -n travel-agency-langgraph -l app=travel-orchestrator-langgraph -f`
open in a second terminal while you run the curl commands below and the agent's reasoning
shows up live. Set `VERBOSE=false` on a deployment to silence this.

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

The client will discover and list available tools. You can invoke `flight_price` directly from the MCP client without writing any agent code.

**To verify the tool list from the CLI:**

```bash
curl -X POST http://agentgateway.lab/mcp-flights \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

**All four MCP backends:**

| MCP endpoint | Tools |
| --- | --- |
| `agentgateway.lab/mcp-flights` | `list_airports`, `flight_price` |
| `agentgateway.lab/mcp-destinations` | `list_destinations`, `shortlist_destinations`, `get_destination` |
| `agentgateway.lab/mcp-book-flights` | `create_booking`, `list_bookings` |
| `agentgateway.lab/mcp-dice-roller` | `roll-2`, `roll-6`, `roll-20` |

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
  -d '{"prompt": "Plan a high budget museum trip for Maarten from AMS on 2026-07-15", "thread_id": "demo-1"}' \
  | jq .
```

The response includes:
- `shortlist` — 5 candidate destinations
- `selected_destination` — the one chosen
- `flight` — found flight details
- `status` — `"awaiting_approval"`, not a confirmed booking yet (see Step 3)
- `pending_approval` — the proposed booking, waiting on a human decision
- `answer` — explains that it's waiting for approval

### Step 3 — Approve (or reject) the booking

**What to show:** booking a flight is the one irreversible, money-committing step in this
workflow, so `book_flight` pauses and waits for a human decision instead of booking
immediately (LangGraph's `interrupt()` — see `book_flight` in `graph.py` for how).

```bash
# See what's waiting for a decision
curl http://agentgateway.lab/a2a/travel-orchestrator/pending | jq .

# Approve it — completes the booking
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-1", "approved": true}' | jq .

# Or reject it — confirms nothing was booked
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-1", "approved": false}' | jq .
```

The approved response now includes `booking` (a real confirmation code) and `status: "booked"`.
The rejected response shows `status: "rejected"` and `booking: null` — and the flight-booker
specialist is never called at all in that case (confirm this live in Grafana Loki: no
`travel-flight-booker` log line appears for a rejected thread_id).

For the human-facing side of this rather than raw curl, use the approval CLI:

```bash
cd applications/agents/travel-agency-langgraph/orchestrator
ORCHESTRATOR_URL=http://agentgateway.lab uv run python approve_cli.py
```

It polls `/pending`, shows each proposed booking, and prompts `Approve? [y/n]` — this is the
better thing to show live in a demo than the raw curl calls above.

### Step 4 — Show what happened under the hood

The orchestrator made **4 sequential A2A calls** through agentgateway, with a human decision
in between the last two:

```
orchestrator → agentgateway/a2a/destination         → got shortlist of 5
orchestrator → agentgateway/a2a/destination-decision → picked one
orchestrator → agentgateway/a2a/flight-finder        → found flight
                                                       ⏸ paused — awaiting human approval
                                                       (your decision via /approve, Step 3)
orchestrator → agentgateway/a2a/flight-booker        → confirmed booking (only if approved)
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

### Step 5 — Show the retry loop (optional)

The orchestrator retries with a different destination if flight-finder returns nothing — but this is **prompt-driven, not graph-driven**. There's no hand-authored `StateGraph`, no `route_after_flight`, no conditional edges: the orchestrator is a single LangChain tool-calling agent (`langchain.agents.create_agent`), and the retry behavior lives entirely in `SYSTEM_PROMPT` in `graph.py`, which instructs the model to call `pick_destination` again with the failed IATA code appended to `tried_iata_codes` whenever `find_flight` returns `{"flight": null}`. Open `applications/agents/travel-agency-langgraph/orchestrator/travel_orchestrator/graph.py` and read `SYSTEM_PROMPT` and the `find_flight` tool to see this.

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

Each `/plan` call appears as a single nested trace spanning all five services:
- `extract_trip_requirements` — the LCEL chain that turns free text into structured requirements
- the orchestrator's agent loop: LLM turns plus tool calls (`get_destination_shortlist`, `pick_destination`, `find_flight`, `book_flight`)
- each specialist's `@traceable` function (`get_destinations`, `pick_destination`, `find_flight`, `book_flight`), nested under the matching tool call via trace-header propagation across the A2A hop
- inputs, outputs, and latency for every step above
- the retry loop, if a flight wasn't found on the first pick, visible as repeated `pick_destination` → `find_flight` tool calls with a growing `tried_iata_codes` list — not a different graph path
- the `book_flight` tool call itself shows a gap in the trace timeline where the run was paused awaiting human approval (Demo 3, Step 3) — the trace resumes once the `/approve` decision comes in

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
