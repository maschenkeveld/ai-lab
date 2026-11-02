<p align="center">
  <img src="ai-lab.png" alt="Kong AI Lab OSS" width="600"/>
</p>

# AI Lab — Local Kind cluster

> A fully local, throwaway Kind cluster (Colima on macOS). The only external dependency is an LLM
> provider key. The agents are Pydantic AI services that talk to each other over A2A through agentgateway.

A local open-source AI platform lab running on a Kind cluster named `ai-lab-oss`.

**Agent-to-agent, in the open** — five separate agents (an orchestrator plus four specialists) coordinate over the open A2A protocol, and reach their tools over open MCP, with every hop going through agentgateway. Agents are written with Pydantic AI (MIT, model-agnostic); nothing in the lab is proprietary. The same travel-agency workflow runs on the Kong AI Lab, so the two can be compared side by side.

**Fully reproducible** — Kind on a laptop, Taskfile, and Helm for config, scriptable end-to-end.

---

## What this repository is for

A runnable reference for agentic AI on open-source infrastructure, small enough for a laptop and complete enough to demo and to build from. It exists to:

- **Show agent-to-agent (A2A) working end to end.** Five separate agents coordinate a trip over the open A2A protocol, use tools over MCP, and call models through an OpenAI-compatible gateway, with every hop routed by agentgateway.
- **Give Field Engineers and SAs a proven baseline** for customer conversations about AI gateways, so an engagement starts from a working setup instead of a blank page.
- **Support a side-by-side comparison with the Kong AI Lab.** The same travel-agency workflow runs on both, so differences in routing, auth, observability and agent tooling can be shown rather than described (see [Capability comparison](#capability-comparison-with-kong-ai-lab)).
- **Stay vendor-neutral.** Pydantic AI (MIT), A2A and MCP are open standards; nothing here needs a proprietary component, and any OpenAI-compatible model works through LiteLLM.

It is a lab, not a product: routes are unauthenticated and the cluster is throwaway. Start with `DEMO.md` to present it and `TESTS.md` to verify it.

---

---

## Contents

- [AI Lab — Local Kind cluster](#ai-lab--local-kind-cluster)
  - [Contents](#contents)
  - [What this repository is for](#what-this-repository-is-for)
  - [Architecture](#architecture)
  - [What's in the lab](#whats-in-the-lab)
    - [Traefik — HTTP edge routing](#traefik--http-edge-routing)
    - [LiteLLM — LLM gateway](#litellm--llm-gateway)
    - [agentgateway — MCP and A2A](#agentgateway--mcp-and-a2a)
    - [Pydantic AI agents](#pydantic-ai-agents)
    - [MCP servers](#mcp-servers)
    - [REST APIs](#rest-apis)
    - [LLM Analytics](#llm-analytics)
    - [Supporting services](#supporting-services)
  - [How it's put together](#how-its-put-together)
  - [Repository structure](#repository-structure)
  - [Setup](#setup)
    - [Prerequisites](#prerequisites)
    - [Step 1 — Start Colima](#step-1--start-colima)
    - [Step 2 — Create secrets file](#step-2--create-secrets-file)
    - [Step 3 — Bring up the cluster](#step-3--bring-up-the-cluster)
  - [Hostnames](#hostnames)
  - [Demos](#demos)
    - [LLM routing {#llm-routing-demo}](#llm-routing-llm-routing-demo)
    - [MCP tools {#mcp-demo}](#mcp-tools-mcp-demo)
    - [A2A agents {#a2a-demo}](#a2a-agents-a2a-demo)
    - [LLM Analytics {#llm-analytics-demo}](#llm-analytics-llm-analytics-demo)
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
        ├── /a2a/travel-orchestrator ──► Pydantic AI orchestrator (port 4200)
        │     ├── /a2a/destination ──► destination specialist (port 4201)
        │     ├── /a2a/destination-decision ──► destination-decision specialist (port 4202)
        │     ├── /a2a/flight-finder ──► flight-finder specialist (port 4203)
        │     └── /a2a/flight-booker ──► flight-booker specialist (port 4204)
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

**A2A routes** — plain `HTTPRoute` resources pointing at `agentgateway-proxy` GatewayClass in `k8s/oss/routes/travel-agency.yaml`:

`/a2a/travel-orchestrator`, `/a2a/destination`, `/a2a/destination-decision`, `/a2a/flight-finder`, `/a2a/flight-booker`

All routes are unauthenticated. agentgateway does not enforce auth on MCP or A2A routes in this lab.

### Pydantic AI agents

Five Python services (`applications/agents/travel-agency/`), each an A2A server built with [Pydantic AI](https://ai.pydantic.dev) and [FastA2A](https://github.com/pydantic/fasta2a). Every call between agents goes over A2A through agentgateway; every tool call goes over MCP through agentgateway.

| Agent | Port | Responsibility |
|---|---|---|
| `orchestrator` | 4200 | Tool-calling planner. Extracts trip requirements, then calls the four specialists as tools over A2A. Owns the booking approval gate |
| `destination` | 4201 | Shortlists destinations (MCP `list_destinations`), drops the origin city, re-ranks with LiteLLM embeddings |
| `destination-decision` | 4202 | Picks one destination from the shortlist using the dice-roller MCP tool (`roll-20`) |
| `flight-finder` | 4203 | Looks up a flight via the `flight_price` MCP tool |
| `flight-booker` | 4204 | Books the selected flight via the `create_booking` MCP tool |

Each agent serves an agent card at `/a2a/<name>/.well-known/agent-card.json`, A2A JSON-RPC at `/a2a/<name>/`, and `/health`. `message/send` is asynchronous: it returns a task, and the caller polls `tasks/get`. The orchestrator also has `/plan`, `/plan/stream`, `/pending` and `/approve` REST endpoints under `/a2a/travel-orchestrator/`.

Shared code (model and MCP wiring, OTel setup, the A2A client and server helpers, the typed request/result contracts) lives in `common/`. The contracts are Pydantic models used on both sides of each A2A call.

**Human-in-the-loop booking approval** — the orchestrator's `book_flight` tool is declared with `requires_approval=True`, so the run pauses before the one irreversible step and waits for a human decision:

1. A trip request that reaches booking comes back with `status: "awaiting_approval"` and a `pending_approval` object describing the proposed booking.
2. `GET /a2a/travel-orchestrator/pending` lists every paused run, keyed by `thread_id`. The list is a query on Postgres, so it survives an orchestrator restart.
3. `POST /a2a/travel-orchestrator/approve` with `{"thread_id": "...", "approved": true|false}` resumes that exact run. Approving books the flight; rejecting reports that nothing was booked.

Over A2A, a paused run is a task in the `input-required` state.

`approve_cli.py` wraps these calls:

```bash
cd applications/agents/travel-agency
./approve_cli.py plan "Cultural city trip from Amsterdam, mid budget, museums, 2026-12-18. Book it."
./approve_cli.py pending
./approve_cli.py approve <thread_id>      # or: reject <thread_id>
```

**Conversation state** — message history is stored per `thread_id` in the `agent_threads` table of the existing `llm-analytics` Postgres, so a later request with the same `thread_id` continues the same conversation. Without Postgres the orchestrator falls back to in-memory state.

**Tracing** — all five services export OpenTelemetry traces to the LGTM collector (Starlette, httpx and Pydantic AI instrumentation), and trace context is propagated across each A2A hop, so one trip is one trace tree in Grafana Tempo.

### MCP servers

Four MCP servers. All listen on port 8000 at path `/mcp` (StreamableHTTP protocol).

| Server | Language | Tools |
|---|---|---|
| `dice-roller` | Go | `roll-2`, `roll-6`, `roll-20` |
| `destinations-adapter` | Python (FastMCP) | `list_destinations`, `shortlist_destinations`, `get_destination` |
| `flights-adapter` | Python (FastMCP) | `list_airports`, `flight_price` |
| `book-flights-adapter` | Python (FastMCP) | `create_booking`, `list_bookings` |

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
| `task build` | Builds and loads all custom Docker images into Kind |
| `task secrets` | Creates the `keycloak-realm` and `litellm-env` k8s Secrets |
| `task helm:install` | Installs MetalLB, Traefik, Keycloak, agentgateway |
| `task manifests:apply` | Applies base manifests (Kafka, otel-lgtm, Postgres, REST servers, MCP servers, Ollama, etc.) |
| `task oss:manifests-apply` | Applies Traefik routes, LiteLLM, agentgateway backends and the travel-agency agents |
| `task oss:agency-apply` | Redeploys the 5 agents and restarts them (rebuild first with `task build:agency`) |
| `task doctor` | Preflight: tools, Colima network address, `.env` keys |
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
│   ├── agents/travel-agency/              # Pydantic AI agents, one uv project each
│   │   ├── common/                        # Model/MCP/OTel wiring, A2A helpers, typed contracts
│   │   ├── orchestrator/                  # Planner + approval gate + FastAPI (port 4200)
│   │   ├── destination/                   # Destination shortlist (port 4201)
│   │   ├── destination-decision/          # Picks one destination (port 4202)
│   │   ├── flight-finder/                 # Flight search (port 4203)
│   │   ├── flight-booker/                 # Flight booking (port 4204)
│   │   └── approve_cli.py                 # Plan / list / approve / reject from a terminal
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
│       ├── litellm/                       # LiteLLM Deployment, ConfigMap, Service
│       ├── routes/                        # HTTPRoutes (rest-apis, travel-agency, mcp-servers, litellm)
│       ├── travel-agency/                 # Agent Deployments + Services
│       └── traefik/                       # Traefik Gateway CRD
│
└── docs/                                  # Architecture diagrams and the enablement deck (PDF)
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
| [uv](https://docs.astral.sh/uv/) | Only to run the agents' tests locally |
| OpenAI API key | Needed for the agents (`openai-gpt`, `embedding-openai`). The cluster comes up without it |
| Google Gemini API key | Optional |
| [Ollama](https://ollama.com/) | Optional — only for the `ollama-llama` alias |

### Step 1 — Start Colima

```bash
colima start --cpu 6 --memory 12 --disk 100 --network-address --vm-type vz --mount-type virtiofs
```

### Step 2 — Add your key

```bash
cp .env.example .env     # then set OPENAI_KEY (and optionally GEMINI_KEY)
cd k8s && task doctor    # preflight check
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

# Plan a trip. It pauses for approval before booking, so the response has
# status "awaiting_approval" and a pending_approval object, not a confirmation.
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/plan \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"demo-thread-1","prompt":"Cultural city trip from Amsterdam, mid budget, museums, 2026-12-18. Book it."}' | jq .

# See the booking waiting for a decision
curl http://agentgateway.lab/a2a/travel-orchestrator/pending | jq .

# Approve it (use "approved": false to reject and confirm nothing gets booked)
curl -X POST http://agentgateway.lab/a2a/travel-orchestrator/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-thread-1", "approved": true}' | jq .


# The same request as a raw A2A call: message/send returns a task, poll tasks/get for the result
curl -X POST http://agentgateway.lab/a2a/flight-finder/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"kind":"message","messageId":"m1","role":"user","parts":[{"kind":"data","data":{"origin":"AMS","to_iata":"VIE","date":"2026-12-18"}}]}}}' | jq .
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
| Pydantic AI agents (×5) | ✓ | ✓ OTLP + stdout | ✓ |
| LiteLLM | ✗ (Prometheus metrics on `/metrics`) | stdout | ✗ |
| agentgateway | ✗ | stdout | ✗ |

The agents are instrumented with Starlette, httpx and Pydantic AI (`Agent.instrument_all()`), and trace context travels across every A2A hop through agentgateway, so a trip shows up in Tempo as one tree: orchestrator → specialist → MCP tool → REST API, with the LLM calls inside each agent. There is no separate tracing vendor.

---

## Capability comparison with Kong AI Lab

| Feature | This lab (OSS) | Kong AI Lab | Notes |
|---|---|---|---|
| HTTP routing | Traefik OSS | Kong API GW | Both cover the basics; Kong has a plugin ecosystem |
| LLM gateway | LiteLLM (3 models, key auth) | Kong LLM GW | LiteLLM: multi-provider, aliases. Kong adds: semantic routing, semantic cache, guardrails, RAG, token budgets, per-consumer rate limits |
| MCP | agentgateway (open, no auth) | Kong MCP GW | agentgateway routes MCP natively; Kong adds OAuth2 scope ACL, key-auth ACL, token exchange |
| Agent framework | Pydantic AI (Python, MIT) | TypeScript agents | Same workflow, different runtimes. Pydantic AI is model-agnostic and runs against LiteLLM here |
| A2A agents | FastA2A servers, routed by agentgateway | A2A routed through Kong | Both speak the open A2A protocol; the lab shows the same four-hop flow on each |
| Auth enforcement | Keycloak present, routes unauthenticated | Keycloak + OIDC on every route | Significant gap — Kong enforces auth at the gateway layer |
| Observability | REST servers, dice-roller, and the Pydantic AI agents all OTEL-wired (traces+logs+metrics); LiteLLM/agentgateway logs-only | All components fully wired (OTEL traces + logs + metrics) | LiteLLM/agentgateway still a gap |
| LLM Analytics | Full pipeline (Kafka → Postgres → React) | Same | Parity |
| MCP adapters | Python FastMCP (explicit adapter per API) | Kong ai-mcp-proxy (auto-converts REST) | Kong approach is zero-code |
| Event/Kafka | Direct Kafka exposure | Kong Event Gateway (protocol proxy + auth) | Kong adds a managed broker layer |
| Insomnia files | None | 7 Insomnia v5 files | Gap |

---

## Known gaps

- agentgateway routes are open — no auth, no scope enforcement on MCP or A2A endpoints.
- `mcp-dice-roller` spans land in their own trace: agentgateway does not forward `traceparent` to it, so the dice roll is not part of the trip's trace tree (the Python MCP adapters and REST servers are).
- An agent keeps its MCP session open, so if an MCP server pod is replaced (the stateful dice roller in particular), restart the agent that uses it (`kubectl rollout restart deploy/destination-decision -n travel-agency`) or its tool calls return 404.
- LiteLLM has no OTEL export — it exposes a `/metrics` Prometheus endpoint but doesn't push to LGTM.
- No Insomnia collection files for MCP demo flows.
- LLM Analytics pipeline was designed around the `kafka-log` plugin output format; using it with LiteLLM requires a compatible log middleware.
