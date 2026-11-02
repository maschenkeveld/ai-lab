# OSS AI Lab — Demo Guide (local Kind lab)

## Setting the scene

A fully local AI platform running on a Kind cluster, built entirely from open-source components. It demonstrates multi-provider LLM routing, MCP tool exposure, and a multi-agent travel workflow where separate agents talk to each other over A2A — all on a laptop.

The travel agency runs as **5 separate Python agents** (Pydantic AI + FastA2A) communicating over A2A JSON-RPC through agentgateway. Their tools are reached over MCP through the same gateway:

```
Client → agentgateway → orchestrator (Pydantic AI)
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

**Tip for any live demo:** keep `kubectl logs -n travel-agency -l app=orchestrator -f` open in a second terminal, and Grafana → Explore → Tempo in a browser tab. The agents log each request, and one trip is one trace tree.

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

## Demo 3 — A2A multi-agent workflow (Pydantic AI)

**What to show:** five independent Python microservices, each with an A2A endpoint, coordinated by an orchestrator agent through agentgateway. Agents call agents over A2A, and tools over MCP.

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
immediately (the tool is declared `requires_approval=True`, see `book_flight` in `planner.py`).

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
`flight-booker` request appears for a rejected thread_id). Pending approvals are stored in Postgres, so
they survive an orchestrator restart: `kubectl rollout restart deploy/orchestrator -n travel-agency`, then `/pending` again.

For the human-facing side of this rather than raw curl, use the approval CLI:

```bash
cd applications/agents/travel-agency
./approve_cli.py plan "Cultural city trip from Amsterdam, mid budget, museums, 2026-12-18. Book it."
./approve_cli.py pending
./approve_cli.py approve <thread_id>      # or: reject <thread_id>
```

It prints the proposed booking and the exact command to approve or reject it.

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

Call a specialist directly to make it concrete. A2A `message/send` is asynchronous: it returns a task, and you poll `tasks/get` for the result.

```bash
TASK=$(curl -s -X POST http://agentgateway.lab/a2a/flight-finder/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"kind":"message","messageId":"m1","role":"user","parts":[{"kind":"data","data":{"origin":"AMS","to_iata":"VIE","date":"2026-12-18"}}]}}}' \
  | jq -r '.result.task.id')

sleep 6
curl -s -X POST http://agentgateway.lab/a2a/flight-finder/ \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tasks/get\",\"params\":{\"id\":\"$TASK\"}}" \
  | jq '.result.task.status.state, .result.task.artifacts[0].parts[0].data'
```

The request is a typed `data` part (`FlightRequest`) and the result is a typed artifact (`FlightResult`): the contracts live in `common/travel_common/contracts.py` and both sides use the same Pydantic models.

### Step 5 — Show the retry loop (optional)

The orchestrator retries with a different destination if flight-finder returns nothing — but this is **prompt-driven, not graph-driven**. There is no hand-authored graph or conditional edge: the orchestrator is a single Pydantic AI tool-calling agent, and the retry behavior lives in `SYSTEM_PROMPT` in `applications/agents/travel-agency/orchestrator/travel_orchestrator/planner.py`, which tells the model to call `pick_destination` again with the failed IATA code appended to `tried_iata_codes` whenever `find_flight` returns `{"flight": null}`.

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
{namespace="travel-agency"}

# Just the orchestrator
{namespace="travel-agency", app="orchestrator"}

# Errors across the lab
{namespace=~"travel-agency|litellm|agentgateway-system"} |= "error"
```

### Tempo — one trace per trip

Explore → Tempo → search service `travel-orchestrator`. A single `/plan` call is one trace tree across all five agents:
- the orchestrator's agent run, with each LLM turn and each tool call (`get_destination_shortlist`, `pick_destination`, `find_flight`, `book_flight`)
- under each tool call, the A2A hop through agentgateway into the specialist's own agent run
- under that, the specialist's LLM turns and its MCP tool call, and the REST API span behind it
- a retry, if a flight wasn't found on the first pick, as repeated `pick_destination` → `find_flight` calls

The booking approval splits a trip into two traces: the `/plan` call that ends with the pause, and the `/approve` call that resumes it.

### Kafka UI — `http://kafka-ui.lab`

Shows topic list, message counts, and live messages on `llm-usage`.

---

## Useful commands

```bash
# Watch all travel agent pods
kubectl get pods -n travel-agency -w

# Logs from a specific specialist
kubectl logs -n travel-agency -l app=flight-finder -f

# Restart the orchestrator after a code change
kubectl rollout restart deployment/orchestrator -n travel-agency

# Rebuild and redeploy just the agents
cd k8s && task build:agency && task oss:agency-apply

# Check agentgateway routes
kubectl get httproute -A

# Check all pods are healthy
kubectl get pods -A | grep -v Running | grep -v Completed
```
