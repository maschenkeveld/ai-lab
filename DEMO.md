# OSS AI Lab: Demo Guide (local Kind lab)

## Setting the scene

A fully local AI platform running on a Kind cluster, built entirely from open-source components. It shows multi-provider LLM routing (including a model running on your own Mac), MCP tool exposure, and the **travel desk**: separate agents that talk to each other over A2A, with a durable workflow, human approval, and a web app, all on a laptop.

The travel desk is **five Python agents** (Pydantic AI + FastA2A) talking A2A through agentgateway, with their tools reached over MCP through the same gateway:

```
Browser / client → agentgateway
   /desk/                    → the web app and REST API
   /a2a/trip-coordinator     → coordinator (durable workflow, approvals, Postgres)
        → agentgateway → destination-scout → MCP travel-search → REST
        → agentgateway → flight-scout      → MCP travel-search → REST
        → agentgateway → booking-agent     → MCP bookings      → REST   (no LLM: moves the money)
        → agentgateway → fare-watcher      → MCP travel-search → REST   (watches fares for days)
```

---

## Before you start

Bring the lab up if it isn't already running:

```bash
cd k8s
task up
```

This takes a few minutes on first run (image pulls). Subsequent runs are fast.

**Tip for any live demo:** keep the web app (`http://agentgateway.lab/desk/`) on one screen and `kubectl -n travel-desk logs -f deploy/coordinator` on another, with Grafana → Explore → Tempo in a browser tab.

---

## Sanity checks

Run these before demoing. Everything should return a response without errors.

**REST backends via Traefik:**

```bash
curl http://traefik-api-gw.lab/flights/health
curl http://traefik-api-gw.lab/destinations/health
curl http://traefik-api-gw.lab/book-flights/health
```

**LiteLLM: confirm models are loaded:**

```bash
curl -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models
```

Expected: `openai-gpt`, `gemini-pro`, `ollama-llama`, `local-vllm`, `embedding-openai`.

**All 5 agent cards reachable through agentgateway:**

```bash
for A in trip-coordinator destination-scout flight-scout booking-agent fare-watcher; do
  curl -s http://agentgateway.lab/a2a/$A/.well-known/agent-card.json | jq -r '.name'
done
```

**The web app:** open `http://agentgateway.lab/desk/`.

**Kafka accessible from Mac terminal:**

```bash
kafka-topics --bootstrap-server kafka-direct.lab:9092 --list
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

**Same request to the model running on your Mac** (after `task inference:run`, see Demo 5):

```bash
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{"model": "local-vllm", "messages": [{"role": "user", "content": "What does an API gateway do? Three words."}]}'
```

---

## Demo 2: MCP tools via agentgateway

**What to show:** agentgateway exposes MCP servers as tools that any MCP client can discover and call.

**Connect Claude Desktop (or any MCP client) to:**

```
http://agentgateway.lab/mcp-travel-search
http://agentgateway.lab/mcp-bookings
```

The client discovers the tools. You can call `flight_price` directly without writing any agent code.

**To verify the tool lists from the CLI** (MCP is stateful: initialize, then send the session id it returns):

```bash
for ENDPOINT in mcp-travel-search mcp-bookings; do
  SID=$(curl -s -D - -o /dev/null -X POST http://agentgateway.lab/$ENDPOINT \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}' \
    | grep -i '^mcp-session-id' | awk '{print $2}' | tr -d '\r')
  echo "$ENDPOINT:"
  curl -s -X POST http://agentgateway.lab/$ENDPOINT \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | sed -n 's/^data: *//p' | jq -r '.result.tools[].name'
done
```

| MCP endpoint | Tools |
| --- | --- |
| `agentgateway.lab/mcp-travel-search` | `list_destinations`, `shortlist_destinations`, `get_destination`, `list_airports`, `flight_price` |
| `agentgateway.lab/mcp-bookings` | `hold_booking`, `confirm_booking`, `cancel_booking`, `get_booking`, `list_bookings` |

The backends are plain REST services with no MCP code in them. agentgateway routes the protocol; the FastMCP adapters translate to REST.

---

## Demo 3: The travel desk (A2A, durable workflow, human in the loop)

**What to show:** agents that call each other over A2A, a workflow that lasts as long as the people involved take, a booking that is held, approved under a policy, and confirmed, and a system that survives a restart in the middle.

### Step 1: Plan a trip

Open `http://agentgateway.lab/desk/`. Type your name top right (that is who you approve as). Paste a request, for example:

> A culture weekend from Amsterdam around 18 December, mid budget, a day or two either way, museums please.

Press **Plan it** and watch the trip's status: *researching*, then *holding*, then *awaiting approval*. In those ~25 seconds the coordinator extracted the request with a model, asked `destination-scout` and `flight-scout` over A2A (which called MCP tools), and **held** a flight through `booking-agent`.

Show the agent cards while it works: `curl http://agentgateway.lab/a2a/booking-agent/.well-known/agent-card.json | jq .` The booking agent's card says it is deterministic. It has no model, by design.

### Step 2: The proposal and the policy

The page shows the proposal (destination, flight, date, price, booking code in state *held*, alternatives) and a briefing a model wrote for the approver. Point at the policy box:

| Flight price | Who must approve |
|---|---|
| under 60 EUR | nobody, booked automatically |
| 60 to 180 EUR | one approver |
| 180 EUR and up | two different approvers |

The policy is code, not a prompt: a model cannot talk the desk into skipping an approval.

### Step 3: Decide

Press **Approve** (try a second trip and press **Reject** to see the hold released, or **Edit…** to pick another destination, which cancels the hold and researches again). For a two-approver trip, approve as one name, then try the same name again: the desk refuses ("has already decided"), even with different capitalisation. Change the name at the top right and approve again.

The booking is **confirmed**; its status in the proposal card goes from *held* to *scheduled*.

### Step 4: The fare drops

The `fare-watcher` is a long-running agent: a workflow per booking that wakes every 30 seconds and re-quotes the fare. Press **Simulate a fare drop** (or `./desk_cli.py drop <trip_id>`). Within 30 seconds the page shows a new approval, "Rebook at a lower fare", with the saving. A model judged that the drop was worth asking about; the coordinator asked a human.

Approve it. The audit log shows the saga's order: hold the new booking, confirm it, only then cancel the old one. The traveller is never without a seat.

### Step 5: Kill the coordinator mid-approval

Plan another trip and wait until it is awaiting approval. Then:

```bash
kubectl -n travel-desk delete pod -l app=coordinator
```

Wait for the new pod (`kubectl -n travel-desk get pods -w`). Reload the page: the approval is still there. Approve it and the booking completes. The workflow lives in Postgres; the pod is disposable.

### Step 6: The audit trail

Open any trip and read the audit log: every step, who did it (a person, `policy`, `booking-agent`, `fare-watcher`), in order. Then try to rewrite history:

```bash
kubectl -n travel-desk exec desk-postgres-0 -- psql -U postgres -d coordinator \
  -c "UPDATE audit SET actor='mallory'"
# ERROR:  the audit log is append-only
```

### Step 7: Under the hood

```bash
cd applications/agents/travel-desk
./desk_cli.py inbox                 # the approval queue, from the terminal
./desk_cli.py show <trip_id>        # proposal, approvals, audit
./desk_cli.py demo                  # the whole flow, scripted
```

In Grafana → Explore → Tempo, search service `trip-coordinator`: the research is one trace tree from the coordinator through both scouts, their MCP tool calls, and the REST servers. In the gateway logs (`kubectl -n agentgateway-system logs deploy/agentgateway-proxy`) you can see every A2A hop and MCP call pass through.

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

---

## Demo 5: A local model

**What to show:** the same agents on a model that never leaves your laptop.

```bash
cd k8s
task inference:run                 # terminal 1: vLLM-Metal serves Qwen2.5-14B from .env
task inference:check               # terminal 2: model list plus a generation smoke test
task secrets:litellm && kubectl -n litellm rollout restart deploy/litellm
task desk:model -- local-vllm      # every desk agent now uses it
```

Plan a trip in the web app. It takes a little longer than with a hosted model (about 85 seconds to the first approval, against about 35), and the result has the same shape, because the desk keeps facts and policy in code and only asks the model for judgment. Compare the models on the hard part, reading a free-text request:

```bash
task eval -- --suite extraction --model openai-gpt
task eval -- --suite extraction --model local-vllm
```

Put it back with `task desk:model -- openai-gpt`.

---

## Observability

### Grafana: `http://grafana.lab:3000`

Default credentials: `admin / admin`

| Source | What you see |
| --- | --- |
| **Explore → Loki** | Logs from all pods |
| **Explore → Tempo** | Distributed traces |
| **Explore → Prometheus** | Traefik request metrics, LiteLLM metrics |

**Useful Loki queries:**

```logql
# All travel desk logs
{namespace="travel-desk"}

# Just the coordinator
{namespace="travel-desk", app="coordinator"}

# Errors across the lab
{namespace=~"travel-desk|litellm|agentgateway-system"} |= "error"
```

### Tempo: one trace tree per piece of research

Explore → Tempo → search service `trip-coordinator`. The research for a trip is one trace tree:
- the coordinator's request, then an A2A hop through agentgateway into each scout's own agent run
- under each, the scout's LLM turns and its MCP tool call, and the REST API span behind it
- the booking agent's calls to the bookings MCP server and the booking REST server

An approval is a separate trace: it starts when a human decides. The workflow's steps and the full history are in the audit log (Demo 3, Step 6).

### Kafka UI — `http://kafka-ui.lab`

Shows topic list, message counts, and live messages on `llm-usage`.

---

## Useful commands

```bash
# Watch all desk pods
kubectl get pods -n travel-desk -w

# Logs from one agent
kubectl logs -n travel-desk -l app=fare-watcher -f

# Restart the coordinator after a code change
cd k8s && task build:desk-coordinator && task oss:desk-apply

# Rebuild and redeploy the whole desk
cd k8s && task build:desk && task oss:desk-apply

# Run the evals against the live lab
cd k8s && task eval

# Check agentgateway routes
kubectl get httproute -A

# Check all pods are healthy
kubectl get pods -A | grep -v Running | grep -v Completed
```
