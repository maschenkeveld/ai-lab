<p align="center">
  <img src="ai-lab.png" alt="Kong AI Lab OSS" width="600"/>
</p>

# AI Lab — OSS Stack

A local open-source AI platform lab running on a Kind cluster named `ai-lab-oss`.

**Production-shaped, laptop-scale** — same travel-agency workflow as the Kong AI Lab, but built entirely on open-source components. No Kong, no Konnect. Useful for side-by-side comparisons and OSS-only customer conversations.

**Fully reproducible** — Kind on a laptop, Taskfile, and Helm for config, scriptable end-to-end.

---

## Contents

- [Architecture](#architecture)
- [What's in the lab](#whats-in-the-lab)
  - [Traefik — HTTP edge routing](#traefik-http-edge-routing)
  - [LiteLLM — LLM gateway](#litellm--llm-gateway)
  - [agentgateway — MCP and A2A](#agentgateway--mcp-and-a2a)
  - [LangGraph agents](#langgraph-agents)
  - [MCP servers](#mcp-servers)
  - [REST APIs](#rest-apis)
  - [LLM Analytics](#llm-analytics)
  - [Supporting services](#supporting-services)
- [How it's put together](#how-its-put-together)
- [Repository structure](#repository-structure)
- [Setup](#setup)
- [Hostnames](#hostnames)
- [Demos](#demos)
  - [LLM routing](#llm-routing-demo)
  - [MCP tools](#mcp-demo)
  - [A2A agents](#a2a-demo)
  - [LLM Analytics](#llm-analytics-demo)
- [Observability](#observability)
- [Capability comparison with Kong AI Lab](#capability-comparison-with-kong-ai-lab)
- [Known gaps](#known-gaps)

---

## Architecture

```
Client
  │
  ├── traefik-api-gw.lab ──► Traefik OSS (Kubernetes Gateway API)
  │     └── /flights, /destinations, /book-flights ──► Go REST servers
  │
  ├── litellm.lab ──► LiteLLM Proxy
  │     └── /v1/chat/completions ──► OpenAI / Gemini / Ollama
  │
  └── agentgateway.lab ──► agentgateway
        ├── /a2a/travel-orchestrator ──► LangGraph orchestrator (port 4200)
        │     ├── /a2a/destination ──► LangGraph destination agent (port 4201)
        │     ├── /a2a/destination-decision ──► LangGraph decision agent (port 4202)
        │     ├── /a2a/flight-finder ──► LangGraph flight-finder (port 4203)
        │     └── /a2a/flight-booker ──► LangGraph flight-booker (port 4204)
        ├── /mcp-dice-roller ──► Go MCP server
        ├── /mcp-destinations ──► Python FastMCP adapter → REST destinations
        ├── /mcp-flights ──► Python FastMCP adapter → REST flights
        └── /mcp-book-flights ──► Python FastMCP adapter → REST book-flights
```

---

## What's in the lab

### Traefik — HTTP edge routing

Traefik OSS with Kubernetes Gateway API (`GatewayClass: traefik`). Routes all REST API traffic on `traefik-api-gw.lab`. Routes are plain `HTTPRoute` resources in `k8s/oss/routes/rest-apis.yaml`.

No auth enforcement — routes are open. Traefik does URL-prefix rewriting so `/flights` → `/` on the backend.

### LiteLLM — LLM gateway

OpenAI-compatible API proxy at `litellm.lab`. Three models configured:

| Model alias | Backend | API key |
|---|---|---|
| `openai-gpt` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `gemini-pro` | `gemini-2.5-pro` | `GEMINI_API_KEY` |
| `ollama-llama` | `llama3.2` via Ollama | None (in-cluster) |

Auth via master key (`sk-ai-lab-litellm`). Config lives in `k8s/oss/litellm/config.yaml`.

LiteLLM covers: multi-provider routing, model aliasing, OpenAI-compatible API surface.

What it does NOT have (vs Kong LLM GW): semantic routing, AI semantic cache, prompt/response guardrails, per-consumer rate limiting, RAG injection, token-budget enforcement.

### agentgateway — MCP and A2A

agentgateway (OCI Helm, `cr.agentgateway.dev/charts/agentgateway`) handles all agent and MCP traffic at `agentgateway.lab`.

**MCP backends** — declared as `AgentgatewayBackend` CRDs in `k8s/oss/routes/mcp-servers.yaml`:

| Route | Backend service |
|---|---|
| `/mcp-dice-roller` | `mcp-dice-roller:8000/mcp` |
| `/mcp-destinations` | `mcp-destinations:8000/mcp` |
| `/mcp-flights` | `mcp-flights:8000/mcp` |
| `/mcp-book-flights` | `mcp-book-flights:8000/mcp` |

**A2A routes** — plain `HTTPRoute` resources pointing at `agentgateway-proxy` GatewayClass in `k8s/oss/routes/langgraph-agent.yaml`:

`/a2a/travel-orchestrator`, `/a2a/destination`, `/a2a/destination-decision`, `/a2a/flight-finder`, `/a2a/flight-booker`

All routes are unauthenticated. agentgateway does not enforce auth on MCP or A2A routes in this lab.

### LangGraph agents

Five Python microservices, each running FastAPI + LangGraph and exposing A2A-compatible endpoints:

| Agent | Port | Responsibility |
|---|---|---|
| `orchestrator` | 4200 | Coordinates the workflow via LangGraph graph; calls specialists over A2A |
| `destination` | 4201 | Calls REST destinations API to shortlist destinations |
| `destination-decision` | 4202 | Picks one destination from the shortlist |
| `flight-finder` | 4203 | Searches for flights via REST flights API |
| `flight-booker` | 4204 | Books the selected flight via REST book-flights API |

Every agent exposes:
- `/.well-known/agent-card.json`
- `/a2a/jsonrpc`
- `/health`

The orchestrator also has a `/plan` REST endpoint for quick testing.

**LangSmith tracing** — enabled automatically when the `langsmith-secret` k8s Secret is present (`LANGCHAIN_API_KEY`). Only the orchestrator is instrumented; specialist agents are not.

### MCP servers

Four MCP servers. All listen on port 8000 at path `/mcp` (StreamableHTTP protocol).

| Server | Language | Tools |
|---|---|---|
| `dice-roller` | Go | `roll_dice` |
| `destinations-adapter` | Python (FastMCP) | `get_destinations` |
| `flights-adapter` | Python (FastMCP) | `search_flights` |
| `book-flights-adapter` | Python (FastMCP) | `book_flight`, `list_bookings`, `cancel_booking` |

The Python adapters use FastMCP to wrap the Go REST server APIs as MCP tools.

### REST APIs

Three Go REST servers — identical to those in the Kong AI Lab:

| Server | Route | Notes |
|---|---|---|
| `rest-flights` | `traefik-api-gw.lab/flights` | Flight search by route and date |
| `rest-destinations` | `traefik-api-gw.lab/destinations` | Destination listing with filtering |
| `rest-book-flights` | `traefik-api-gw.lab/book-flights` | Booking CRUD, SQLite-backed |

OTEL env vars are configured on all three; the Go OTEL SDK is wired in the application code.

### LLM Analytics

Identical to the Kong AI Lab. Kafka consumer → embedding → DBSCAN clustering → plugin recommendations → React UI.

```
Kong LLM GW (kafka-log) or LiteLLM → Kafka topic: llm-usage
  → Ingestor (Python)
      generates sentence-transformer embeddings
      stores prompt + embedding + metadata in Postgres/pgvector
    → Analyzer (Python, CronJob every 5 min)
        DBSCAN clustering → Kong plugin recommendations
      → FastAPI (/stats, /requests, /clusters, /recommendations)
        → React UI at analytics.lab
```

The analytics pipeline uses the `kafka-log` plugin output format — when used with LiteLLM, a custom log middleware would be needed to produce the same format.

### Supporting services

| Service | Namespace | Purpose |
|---|---|---|
| Keycloak | `keycloak` | OIDC identity provider (present, not enforced on routes) |
| Kafka (KRaft) | `kafka` | Message broker; exposed directly on `kafka-direct.lab:9092` |
| Postgres + pgvector | `llm-analytics` | Stores LLM logs, embeddings, clusters, recommendations |
| Ollama | Host machine | Local LLM inference via `host.docker.internal` |
| Grafana LGTM | `otel-lgtm` | OTel collector, Loki, Tempo, Prometheus, Grafana — one container |
| Kafka UI | `kafka-ui` | Kafka management UI at `kafka-ui.lab` |

---

## How it's put together

All cluster operations are driven by `task` from the `k8s/` directory.

| Task | What it does |
|---|---|
| `task up` | Full setup — cluster → namespaces → build → secrets → helm → manifests → oss overlays → route → hosts |
| `task down` | Deletes the Kind cluster (`ai-lab-oss`) |
| `task build` | Builds and loads all 16 custom Docker images into Kind |
| `task secrets` | Creates `keycloak-realm` and `litellm-env` k8s Secrets |
| `task helm:install` | Installs MetalLB, Traefik, Keycloak, agentgateway |
| `task manifests:apply` | Applies base manifests (Kafka, otel-lgtm, Postgres, REST servers, MCP servers, Ollama, etc.) |
| `task oss:manifests-apply` | Applies Traefik routes, LiteLLM, agentgateway backends, LangGraph deployments |
| `task oss:langgraph-apply` | Rebuilds + redeploys the 5 LangGraph agent images only |
| `task hosts:apply` | Writes `.lab` hostnames to Mac `/etc/hosts` |
| `task dns:apply` | Patches CoreDNS rewrites for in-cluster `.lab` resolution |
| `task colima:route` | Adds macOS route so MetalLB IPs are reachable |

**Helm releases:**
1. **MetalLB** — assigns real IPs from the Kind Docker subnet
2. **Traefik** — `traefik/traefik`, GatewayClass `traefik`, LoadBalancer at `172.18.255.210`
3. **Keycloak** — CloudPirates OCI chart, realm imported on startup
4. **agentgateway** — `cr.agentgateway.dev/charts/agentgateway` v1.2.0, GatewayClass `agentgateway`

---

## Repository structure

```
.
├── applications/
│   ├── agents/travel-agency-langgraph/   # Python LangGraph multi-agent workflow
│   │   ├── orchestrator/                  # LangGraph graph + FastAPI (port 4200)
│   │   └── specialists/
│   │       ├── destination/              # Destination shortlist (port 4201)
│   │       ├── destination-decision/     # Picks one destination (port 4202)
│   │       ├── flight-finder/            # Flight search (port 4203)
│   │       └── flight-booker/            # Flight booking (port 4204)
│   ├── keycloak/                          # Realm export
│   ├── llm-analytics/                     # Kafka → Postgres analytics pipeline
│   │   ├── ingestor/                      # Kafka consumer + embeddings
│   │   ├── analyzer/                      # DBSCAN clustering + recommendations
│   │   ├── api/                           # FastAPI: /stats /requests /clusters
│   │   └── ui/                            # React dashboard (Vite + nginx)
│   ├── mcp-servers/
│   │   ├── dice-roller/                   # Native Go MCP server
│   │   ├── destinations-adapter/          # Python FastMCP → REST destinations
│   │   ├── flights-adapter/               # Python FastMCP → REST flights
│   │   └── book-flights-adapter/          # Python FastMCP → REST book-flights
│   └── rest-servers/                      # Go REST APIs (flights, destinations, book-flights)
│
├── k8s/
│   ├── Taskfile.yaml                      # All cluster operations
│   ├── helm/                              # Helm values (traefik, keycloak, agentgateway, metallb)
│   ├── manifests/                         # Base services (kafka, otel-lgtm, postgres, REST servers, etc.)
│   └── oss/
│       ├── agentgateway/                  # Gateway CRD (agentgateway-proxy)
│       ├── kafka/                         # Direct external Kafka listener
│       ├── langgraph/                     # LangGraph agent Deployments + Services
│       ├── litellm/                       # LiteLLM Deployment, ConfigMap, Service
│       ├── routes/                        # HTTPRoutes (rest-apis, langgraph-agent, mcp-servers, litellm)
│       └── traefik/                       # Traefik Gateway CRD
│
└── docs/
    └── comparison/demo-checklist.md
```

---

## Setup

### Prerequisites

| Tool | Notes |
|---|---|
| [Colima](https://github.com/abiosoft/colima) | macOS Docker runtime — `--network-address` required |
| [Kind](https://kind.sigs.k8s.io/) | Kubernetes in Docker |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | |
| [Helm](https://helm.sh/) | |
| [Task](https://taskfile.dev/) | |
| [Ollama](https://ollama.com/) running locally | Pull `llama3.2` before starting |
| OpenAI API key | Optional — `gemini-pro` and `ollama-llama` work without it |
| Google Gemini API key | Optional |

### Step 1 — Start Colima

```bash
colima start --cpu 6 --memory 12 --disk 100 --network-address --vm-type vz --mount-type virtiofs
```

### Step 2 — Create secrets file

```bash
cat <<'EOF' > export-secrets.sh
export OPENAI_API_KEY=<your OpenAI API key>
export GEMINI_API_KEY=<your Gemini API key>
export LANGCHAIN_API_KEY=<your LangSmith API key>  # optional
EOF
source ./export-secrets.sh
```

### Step 3 — Bring up the cluster

```bash
cd k8s
task up
```

Runs in order: Kind cluster → namespaces → build images → secrets → Helm releases → base manifests → OSS overlays → MetalLB routes → hosts.

The `colima:route` step asks for your Mac password.

---

## Hostnames

| Hostname | Service |
|---|---|
| `traefik-api-gw.lab` | Traefik — REST API routes (port 80) |
| `litellm.lab` | LiteLLM Proxy (port 80, forwards to 4000) |
| `agentgateway.lab` | agentgateway — MCP and A2A routes (port 80) |
| `kafka-direct.lab:9092` | Direct Kafka listener |
| `keycloak.lab:8080` | Keycloak |
| `analytics.lab` | LLM Analytics React dashboard |
| `analytics-api.lab` | LLM Analytics FastAPI |
| `grafana.lab:3000` | Grafana (otel-lgtm bundle) |
| `kafka-ui.lab` | Kafka UI |

`task hosts:apply` writes these to Mac `/etc/hosts`. `task dns:apply` patches CoreDNS so they resolve inside the cluster too. IPs are pinned via MetalLB annotations and don't drift across `task down` / `task up`.

---

## Demos

Full scripts live in `DEMO.md`. Quick-start snippets below.

### LLM routing {#llm-routing-demo}

```bash
# List available models
curl -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models | jq .

# Call OpenAI via LiteLLM
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai-gpt","messages":[{"role":"user","content":"What is LiteLLM?"}]}' | jq .

# Call Gemini via LiteLLM
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-pro","messages":[{"role":"user","content":"What is LiteLLM?"}]}' | jq .

# Call local Ollama via LiteLLM
curl http://litellm.lab/v1/chat/completions \
  -H "Authorization: Bearer sk-ai-lab-litellm" \
  -H "Content-Type: application/json" \
  -d '{"model":"ollama-llama","messages":[{"role":"user","content":"What is LiteLLM?"}]}' | jq .
```

### MCP tools {#mcp-demo}

```bash
# List MCP tools on each endpoint
curl -s -X POST http://agentgateway.lab/mcp-dice-roller \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | sed -n 's/.*"data": *\(.*\)/\1/p' | jq -r '.result.tools[].name'

curl -s -X POST http://agentgateway.lab/mcp-destinations \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | sed -n 's/.*"data": *\(.*\)/\1/p' | jq -r '.result.tools[].name'
```

All MCP endpoints are open — no auth required.

### A2A agents {#a2a-demo}

```bash
# Check agent cards
curl http://agentgateway.lab/a2a/travel-orchestrator/.well-known/agent-card.json | jq .

# Trigger the full travel workflow
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/a2a/jsonrpc \
  -H "Content-Type: application/json" -H "A2A-Version: 0.3.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "travel-1",
    "method": "message/send",
    "params": {
      "message": {
        "kind": "message",
        "messageId": "msg-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "{\"name\":\"Alice\",\"from\":\"AMS\",\"date\":\"2026-09-01\",\"activity\":\"museums\",\"vibe\":\"culture\",\"budget\":\"high\"}"}]
      }
    }
  }' | jq .

# Or use the simpler /plan endpoint
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/plan \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Plan a high budget museum trip for Alice from AMS on 2026-09-01"}' | jq .
```

### LLM Analytics {#llm-analytics-demo}

```bash
# Trigger the analyzer manually
kubectl create job --from=cronjob/llm-analyzer trigger-$(date +%s) -n llm-analytics

# Open the dashboard
open http://analytics.lab
```

---

## Observability

All components push to Grafana LGTM (`grafana.lab:3000`, default credentials `admin/admin`) via OTLP on port 4318. No Prometheus scraping, no log agent.

| Component | Traces | Logs | Metrics |
|---|---|---|---|
| `rest-flights` | ✓ | ✓ OTLP + stdout | ✓ |
| `rest-destinations` | ✓ | ✓ OTLP + stdout | ✓ |
| `rest-book-flights` | ✓ | ✓ OTLP + stdout | ✓ |
| `mcp-dice-roller` | ✓ | ✓ OTLP + stdout | ✓ |
| LangGraph agents (×5) | ✗ | stdout only | ✗ |
| LiteLLM | ✗ (Prometheus metrics on `/metrics`) | stdout | ✗ |
| agentgateway | ✗ | stdout | ✗ |

**LangSmith tracing** — the orchestrator sends LangGraph node traces to LangSmith when `LANGCHAIN_API_KEY` is present in the `langsmith-secret` k8s Secret. This covers LangGraph-level spans (nodes, edges, LLM calls) but is a separate signal not visible in Grafana Tempo.

For full distributed OTEL tracing across the agent chain (comparable to the Kong AI Lab), the LangGraph agents need OTEL SDK instrumentation added — see [Known gaps](#known-gaps).

---

## Capability comparison with Kong AI Lab

| Feature | This lab (OSS) | Kong AI Lab | Notes |
|---|---|---|---|
| HTTP routing | Traefik OSS | Kong API GW | Both cover the basics; Kong has a plugin ecosystem |
| LLM gateway | LiteLLM (3 models, key auth) | Kong LLM GW | LiteLLM: multi-provider, aliases. Kong adds: semantic routing, semantic cache, guardrails, RAG, token budgets, per-consumer rate limits |
| MCP | agentgateway (open, no auth) | Kong MCP GW | agentgateway routes MCP natively; Kong adds OAuth2 scope ACL, key-auth ACL, token exchange |
| A2A agents | Python + LangGraph | TypeScript + Volcano SDK | Same workflow, different runtimes |
| Auth enforcement | Keycloak present, routes unauthenticated | Keycloak + OIDC on every route | Significant gap — Kong enforces auth at the gateway layer |
| Observability | REST servers + dice-roller wired; agents not wired | All components fully wired (OTEL traces + logs + metrics) | Partial gap |
| LLM Analytics | Full pipeline (Kafka → Postgres → React) | Same | Parity |
| MCP adapters | Python FastMCP (explicit adapter per API) | Kong ai-mcp-proxy (auto-converts REST) | Kong approach is zero-code |
| Event/Kafka | Direct Kafka exposure | Kong Event Gateway (protocol proxy + auth) | Kong adds a managed broker layer |
| Insomnia files | None | 7 Insomnia v5 files | Gap |

---

## Known gaps

- LangGraph agents (×5) have no OTEL instrumentation — no traces, no OTLP logs, no metrics from the agent layer. Only LangSmith (orchestrator only, optional).
- agentgateway routes are open — no auth, no scope enforcement on MCP or A2A endpoints.
- LiteLLM has no OTEL export — it exposes a `/metrics` Prometheus endpoint but doesn't push to LGTM.
- No Insomnia collection files for MCP demo flows.
- Not a git repository — version control not set up.
- LLM Analytics pipeline was designed around the `kafka-log` plugin output format; using it with LiteLLM requires a compatible log middleware.
