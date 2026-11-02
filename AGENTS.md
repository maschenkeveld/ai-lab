# AGENTS.md — AI Lab OSS operating guide

This repository is an open-source AI platform lab. All components are OSS; there is no Kong Gateway, no Konnect, no proprietary control-plane config.

---

## Purpose

A runnable reference for agentic AI on open-source infrastructure, for demos and for Field Engineering engagements, and the OSS side of a comparison with the Kong AI Lab (same travel-agency workflow, different stack). Optimise for: easy to run on a laptop, easy to explain in a demo, open protocols (A2A, MCP, OpenAI-compatible API), no proprietary components. It is a lab, not a product.

## Goal

Build and maintain a local OSS AI lab using:

- **Traefik OSS** — Kubernetes Gateway API HTTP routing for REST APIs
- **LiteLLM Proxy** — OpenAI-compatible LLM gateway (multi-provider, key auth)
- **agentgateway** — MCP tool routing and A2A agent-to-agent traffic
- **Python + Pydantic AI + FastA2A** — travel agency multi-agent workflow: 5 agents that talk to each other over A2A and use MCP tools, all through agentgateway
- **FastMCP** — thin Python adapters that expose Go REST APIs as MCP tools
- **Direct Kafka** — KRaft broker exposed as a LoadBalancer (no Kafka proxy layer)
- **Keycloak** — OIDC identity provider (configured, not yet enforced on routes)
- **Grafana LGTM** — OTel collector, Loki, Tempo, Prometheus, Grafana in one container

The point of the lab is agent-to-agent: separate agents interacting over the open A2A protocol through agentgateway, as a comparison with the Kong AI Lab. Keep the agents framework-light and the protocols open (A2A, MCP, OpenAI-compatible LLM API).

Do NOT add: Kong gateway config, Konnect control-plane resources, TypeScript agent runtime code, or proprietary managed-cloud bootstrap files.

---

## Cluster

Kind cluster name: `ai-lab-oss`. The node image is pinned in `k8s/kind-config.yaml` (Kubernetes v1.34.0):
kind's default image drifts, and agentgateway v1.2.0's CRDs fail validation on Kubernetes 1.37. Bump the pin
deliberately, not by accident.

This lab is local-only: Colima + kind on macOS, nothing shared. Copy `.env.example` to `.env` and set
`OPENAI_KEY` for real LLM calls (the cluster comes up without it).

All cluster operations run from `k8s/`:

```bash
cd k8s
task doctor                   # preflight: tools, Colima network address, .env keys
task up                       # full setup (ends with sudo steps: colima:route, hosts:apply)
task down                     # delete cluster
task build                    # rebuild all local images
task oss:manifests-apply      # reapply gateway/agent/LLM config (fast iteration)
task build:agency            # rebuild only the 5 agent images
task oss:agency-apply         # redeploy + restart only the 5 agents
task hosts:apply              # update Mac /etc/hosts
task dns:apply                # update in-cluster CoreDNS rewrites
```

After a Colima restart: `task colima:route` to restore MetalLB routing.

---

## File layout

```
applications/
  agents/travel-agency/             Python Pydantic AI agents, one uv project each
    common/                         travel_common: model/MCP/OTel wiring, A2A client+server helpers, contracts
    orchestrator/                   Planner + approval gate + FastAPI (port 4200)
    destination/                    Shortlist + embedding re-rank (port 4201)
    destination-decision/           Picks one destination (port 4202)
    flight-finder/                  Flight lookup (port 4203)
    flight-booker/                  Flight booking (port 4204)
    approve_cli.py                  Plan / pending / approve / reject from a terminal
  keycloak/                         Realm export (imported at cluster startup)
  llm-analytics/                    Kafka → Postgres analytics pipeline
    ingestor/                       Kafka consumer + sentence-transformer embeddings
    analyzer/                       DBSCAN clustering + plugin recommendations (CronJob)
    api/                            FastAPI: /stats /requests /clusters /recommendations
    ui/                             React dashboard (Vite + nginx)
  mcp-servers/
    dice-roller/                    Native Go MCP server
    destinations-adapter/           Python FastMCP → REST destinations
    flights-adapter/                Python FastMCP → REST flights
    book-flights-adapter/           Python FastMCP → REST book-flights
  rest-servers/
    destinations/                   Go REST API
    flights/                        Go REST API
    book-flights/                   Go REST API (SQLite-backed)

k8s/
  Taskfile.yaml                     All cluster tasks
  helm/                             Helm values: metallb, traefik, keycloak, agentgateway
  manifests/                        Base services: kafka, otel-lgtm, postgres, redis, REST servers, MCP servers
  oss/
    agentgateway/                   Gateway `agentgateway-proxy` (GatewayClass: agentgateway)
    kafka/                          Direct external Kafka listener
    travel-agency/                  Agent Deployments + Services (namespace travel-agency)
    litellm/                        LiteLLM Deployment, ConfigMap, Service
    routes/                         HTTPRoutes: rest-apis, travel-agency, mcp-servers, litellm
    traefik/                        Traefik Gateway CRD
```

---

## Component reference

### Traefik routes (`k8s/oss/routes/rest-apis.yaml`)

Routes use `GatewayClass: traefik` and point at the `traefik` Gateway in namespace `traefik`. Every route strips its prefix before forwarding:

```yaml
filters:
  - type: URLRewrite
    urlRewrite:
      path:
        type: ReplacePrefixMatch
        replacePrefixMatch: /
```

The `traefik` Gateway and GatewayClass are declared in `k8s/oss/traefik/gateway.yaml`. The MetalLB IP is fixed at `172.18.255.210` via a `metallb.universe.tf/loadBalancerIPs` annotation on the Traefik Service.

### agentgateway backends (`k8s/oss/routes/mcp-servers.yaml`)

MCP endpoints use the `AgentgatewayBackend` CRD (API group `agentgateway.dev/v1alpha1`):

```yaml
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayBackend
metadata:
  name: mcp-dice-roller
  namespace: agentgateway-system
spec:
  mcp:
    targets:
      - name: dice-roller
        static:
          host: mcp-dice-roller.mcp-dice-roller.svc.cluster.local
          port: 8000
          path: /mcp
          protocol: StreamableHTTP
```

A2A routes use plain `HTTPRoute` (Gateway API v1) pointing at the `agentgateway-proxy` Gateway in `agentgateway-system`. These live in `k8s/oss/routes/travel-agency.yaml`. HTTPRoutes do not strip the `/a2a/<name>` prefix, so each agent serves its A2A app under that same prefix.

The agentgateway Gateway CRD is at `k8s/oss/agentgateway/gateway.yaml`. The MetalLB IP is pinned in the Helm values.

### LiteLLM config (`k8s/oss/litellm/config.yaml`)

Stored as a ConfigMap mounted into the LiteLLM pod. Four model aliases:

| Alias | Backend |
|---|---|
| `openai-gpt` | `openai/gpt-4o-mini` |
| `gemini-pro` | `gemini/gemini-2.5-pro` |
| `ollama-llama` | `ollama_chat/llama3.2` via in-cluster Ollama |
| `embedding-openai` | `openai/text-embedding-3-small` |

Master key is `sk-ai-lab-litellm` (hardcoded for local demo). Change it via `task secrets:litellm` (sourced from `.env`) or the `litellm-env` Secret directly.

To add a model: add a `model_list` entry to the ConfigMap and run `task oss:manifests-apply`.

Consumed by every agent through Pydantic AI's OpenAI-compatible model (`LITELLM_BASE_URL=http://litellm.litellm.svc.cluster.local:4000/v1`, alias `openai-gpt` by default), and by the `destination` agent for embeddings-based re-ranking (alias `embedding-openai`) — see "Pydantic AI agents" below.

### Pydantic AI agents (`applications/agents/travel-agency/`)

Each agent is a uv project (`pyproject.toml` + `uv.lock`) with an editable path dependency on `common/` (package `travel_common`). Each service has:
- `server.py` (orchestrator: also `service.py`, `planner.py`, `store.py`, `worker.py`) — builds the Pydantic AI `Agent` and serves it as an A2A app via FastA2A
- `Dockerfile` — the build context is `applications/agents/travel-agency` (so `common/` is available); image `travel-agency-<name>`
- `tests/` — unit tests run with `uv run pytest` in each project, no cluster needed

A2A surface per agent: agent card at `/a2a/<name>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<name>/`, `/health`. `message/send` is asynchronous (returns a submitted task; callers poll `tasks/get`). The orchestrator also exposes `/plan`, `/plan/stream`, `/pending`, `/approve` under `/a2a/travel-orchestrator/`.

Design rules:
- Specialists are LLM agents whose precise work lives in code tools. Tools read the request via the `current_request()` contextvar set by `DataAwareWorker` (`common/travel_common/a2a_server.py`), so IDs, dates and codes never depend on the model copying them.
- A2A payloads are the Pydantic models in `common/travel_common/contracts.py`, sent as `data` parts. Both sides use the same classes.
- The orchestrator is a tool-calling planner whose tools are A2A calls to the specialists (`get_destination_shortlist`, `pick_destination`, `find_flight`, `book_flight`). Specialist URLs come from `SPECIALIST_*` env vars and point at agentgateway, never directly at services.
- The booking approval gate is owned by the orchestrator: `book_flight` is `requires_approval=True`, so the run pauses with `DeferredToolRequests`; `/approve` resumes it. A paused run is an `awaiting_approval` row in Postgres and an `input-required` A2A task. Rejection is a `ToolDenied`.
- Specialist MCP tools are reached through agentgateway (`common/travel_common/mcp.py`): `destination` → `mcp-destinations` (`list_destinations`), `destination-decision` → `mcp-dice-roller` (`roll-20`), `flight-finder` → `mcp-flights` (`flight_price`), `flight-booker` → `mcp-book-flights` (`create_booking` only).
- The REST API only accepts `budget_level` of `low`/`mid`/`high` and vibes `city beach nature culture food nightlife relax design`; an unmatched combination returns an empty shortlist, not an error.

Conversation state: message history per `thread_id` in the `agent_threads` table of the `llm-analytics` Postgres (`DATABASE_URL`); falls back to memory if unset.

Pydantic AI 2.x has no `Agent.to_a2a()`; use `fasta2a.pydantic_ai.AgentWorker` (see `create_a2a_app`). The FastA2A bridge rejects inbound `data` parts and has no input-required support, which is why `common` subclasses the worker.

To rebuild and redeploy:
```bash
cd k8s
task build:agency-orchestrator            # or any build:agency-<name>; `task build:agency` for all five
task oss:agency-apply                     # apply manifests + rollout restart (imagePullPolicy: Never + :latest needs the restart)
```

Run an agent's tests: `cd applications/agents/travel-agency/<name> && uv run pytest`.

Evals (`applications/agents/travel-agency/evals/`, pydantic-evals) run against the live lab: `cd k8s && task eval` (add `-- --no-judge` to skip the LLM judge). Cases plan a trip, then approve or reject the paused booking; deterministic evaluators in `evaluators.py` check requirements, shortlist, selection, pending booking and decision handling, an LLM judge scores the final answer, and a thread-continuity check runs a second turn on the same `thread_id`.

### MCP adapters (`applications/mcp-servers/*-adapter/`)

Python packages using FastMCP. Each adapter:
1. Imports the REST client (httpx calls to `traefik-api-gw.lab`)
2. Declares MCP tools with `@mcp.tool()` decorators
3. Runs FastMCP's Starlette app at `0.0.0.0:8000` path `/mcp`

Pattern:
```python
mcp = FastMCP("destinations-adapter")

@mcp.tool
async def list_destinations(vibes: str, budget_level: str, activities: list[str], limit: int) -> dict:
    return httpx.get("http://traefik-api-gw.lab/destinations/v1/destinations", ...).json()
```

### REST servers (`applications/rest-servers/`)

Go HTTP servers — identical to those in the Kong AI Lab. All three have OTEL SDK wired in the application code (same fanout handler: stdout JSON + OTLP). OTEL env vars are set in their respective `k8s/manifests/rest-*.yaml` files.

The dice-roller Go MCP server (`applications/mcp-servers/dice-roller/`) is also OTEL-instrumented.

---

## Conventions

- Use `.lab` hostnames everywhere — `traefik-api-gw.lab`, `litellm.lab`, `agentgateway.lab`, `keycloak.lab:8080`, `grafana.lab:3000`. They resolve in-cluster (CoreDNS rewrite) and on Mac (`/etc/hosts`). Never hardcode `172.18.x.x` IPs.
- Traefik routes go in `k8s/oss/routes/rest-apis.yaml` and use `GatewayClass: traefik`.
- agentgateway routes go in `k8s/oss/routes/travel-agency.yaml` (HTTPRoute) and `k8s/oss/routes/mcp-servers.yaml` (AgentgatewayBackend). Both use `GatewayClass: agentgateway`.
- LiteLLM config lives in `k8s/oss/litellm/config.yaml` as a ConfigMap.
- Agent k8s resources live in `k8s/oss/travel-agency/`.
- MCP adapter source lives in `applications/mcp-servers/`.
- Agent source lives in `applications/agents/travel-agency/`.
- Reusable application code belongs in `applications/`, not in `k8s/`.
- Do not put route auth config in this repo — there is none.

---

## Observability

LGTM stack at `grafana.lab:3000` (admin/admin). Receives OTLP push on port 4318 — no scraping.

**What's currently wired:**
- REST servers (all 3): OTEL traces + logs + metrics via OTLP
- `mcp-dice-roller`: OTEL traces + logs + metrics via OTLP
- Pydantic AI agents (all 5): OTEL traces + logs + metrics via OTLP (`setup_otel` in `common/travel_common/otel.py`: Starlette + httpx + Pydantic AI instrumentation). Trace context propagates across every A2A hop, so a trip is one trace tree.

**What's not wired (gap vs Kong AI Lab):**
- LiteLLM: Prometheus `/metrics` endpoint, not pushed to LGTM
- agentgateway: stdout logs only

To view what's in Grafana: Explore → Tempo (traces from REST/MCP tier and the agents), Explore → Loki (logs from all components).

---

## Taskfile cheat sheet

```bash
# Cluster
task up                       # Full setup
task down                     # Delete cluster
task build                    # Build + load all images

# Iteration
task oss:manifests-apply      # Reapply all OSS overlays (gateway config, agents, LLM)
task build:agency             # Rebuild the 5 agent images
task oss:agency-apply         # Redeploy + restart the 5 agents
task manifests:apply          # Reapply base manifests (kafka, otel-lgtm, REST servers, MCP servers)

# Networking
task colima:route             # Restore MetalLB routing after Colima restart
task hosts:apply              # Update Mac /etc/hosts
task dns:apply                # Update in-cluster CoreDNS

# LLM Analytics
kubectl create job --from=cronjob/llm-analyzer trigger-$(date +%s) -n llm-analytics
```

---

## What not to do

- Do not add Kong control-plane configuration (Konnect CPs, decK files, KIC resources).
- Do not add Konnect-managed cloud bootstrap files (Terraform, KonnectAPIAuthConfiguration).
- Do not add TypeScript agent runtime code; use Python + Pydantic AI.
- Do not hardcode LoadBalancer IPs; use `.lab` hostnames.
- Do not create a `deck/` directory or any Kong-specific subdirectory.
