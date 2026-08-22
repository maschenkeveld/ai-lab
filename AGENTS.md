# AGENTS.md — AI Lab OSS operating guide

This repository is an open-source AI platform lab. All components are OSS; there is no Kong Gateway, no Konnect, no proprietary control-plane config.

---

## Goal

Build and maintain a local OSS AI lab using:

- **Traefik OSS** — Kubernetes Gateway API HTTP routing for REST APIs
- **LiteLLM Proxy** — OpenAI-compatible LLM gateway (multi-provider, key auth)
- **agentgateway** — MCP tool routing and A2A agent-to-agent traffic
- **Python + LangGraph** — travel agency multi-agent workflow (5 microservices)
- **FastMCP** — thin Python adapters that expose Go REST APIs as MCP tools
- **Direct Kafka** — KRaft broker exposed as a LoadBalancer (no Kafka proxy layer)
- **Keycloak** — OIDC identity provider (configured, not yet enforced on routes)
- **Grafana LGTM** — OTel collector, Loki, Tempo, Prometheus, Grafana in one container

Do NOT add: Kong gateway config, Konnect control-plane resources, TypeScript agent runtime code, or proprietary managed-cloud bootstrap files.

---

## Cluster

Kind cluster name: `ai-lab-oss`

All cluster operations run from `k8s/`:

```bash
cd k8s
task up                       # full setup
task down                     # delete cluster
task build                    # rebuild all 16 local images
task oss:manifests-apply      # reapply gateway/agent/LLM config (fast iteration)
task oss:langgraph-apply      # rebuild + redeploy only the 5 LangGraph agents
task hosts:apply              # update Mac /etc/hosts
task dns:apply                # update in-cluster CoreDNS rewrites
```

After a Colima restart: `task colima:route` to restore MetalLB routing.

---

## File layout

```
applications/
  agents/travel-agency-langgraph/   Python LangGraph agents
    orchestrator/                   FastAPI + LangGraph graph (port 4200)
    specialists/
      destination/                  Calls REST destinations (port 4201)
      destination-decision/         Picks one destination (port 4202)
      flight-finder/                Calls REST flights (port 4203)
      flight-booker/                Calls REST book-flights (port 4204)
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
    agentgateway/                   Gateway CRD (agentgateway-proxy GatewayClass)
    kafka/                          Direct external Kafka listener
    langgraph/                      LangGraph agent Deployments + Services
    litellm/                        LiteLLM Deployment, ConfigMap, Service
    routes/                         HTTPRoutes: rest-apis, langgraph-agent, mcp-servers, litellm
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

A2A routes use plain `HTTPRoute` (Gateway API v1) pointing at the `agentgateway-proxy` Gateway in `agentgateway-system`. These live in `k8s/oss/routes/langgraph-agent.yaml`.

The agentgateway Gateway CRD is at `k8s/oss/agentgateway/gateway.yaml`. The MetalLB IP is pinned in the Helm values.

### LiteLLM config (`k8s/oss/litellm/config.yaml`)

Stored as a ConfigMap mounted into the LiteLLM pod. Three model aliases:

| Alias | Backend |
|---|---|
| `openai-gpt` | `openai/gpt-4o-mini` |
| `gemini-pro` | `gemini/gemini-2.5-pro` |
| `ollama-llama` | `ollama_chat/llama3.2` via in-cluster Ollama |

Master key is `sk-ai-lab-litellm` (hardcoded for local demo). Change it in `k8s/secrets.yaml` or the `litellm-env` Secret.

To add a model: add a `model_list` entry to the ConfigMap and run `task oss:manifests-apply`.

### LangGraph agents (`applications/agents/travel-agency-langgraph/`)

Each agent is a Python package with:
- `server.py` — FastAPI app, exposes `/.well-known/agent-card.json`, `/a2a/jsonrpc`, `/health`, and (orchestrator only) `/plan`
- `graph.py` — LangGraph `StateGraph` definition (orchestrator only; specialists have simpler logic)
- `Dockerfile` — builds a container image named `travel-<agent>-langgraph`

The orchestrator graph calls specialists by making HTTP POST requests to their agentgateway-proxied URLs. Specialist URLs come from env vars (`DESTINATION_AGENT_URL`, etc.) that point at `agentgateway.lab/a2a/<agent>`.

LangSmith tracing is enabled when `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY` is set (via `langsmith-secret` k8s Secret). Only the orchestrator is wired.

To rebuild and redeploy a single agent:
```bash
docker build -t travel-orchestrator-langgraph applications/agents/travel-agency-langgraph/orchestrator
kind load docker-image travel-orchestrator-langgraph --name ai-lab-oss
kubectl rollout restart deploy/travel-orchestrator-langgraph -n travel-agency-langgraph
```

Or use `task oss:langgraph-apply` to rebuild all five.

### MCP adapters (`applications/mcp-servers/*-adapter/`)

Python packages using FastMCP. Each adapter:
1. Imports the REST client (httpx calls to `traefik-api-gw.lab`)
2. Declares MCP tools with `@mcp.tool()` decorators
3. Runs FastMCP's Starlette app at `0.0.0.0:8000` path `/mcp`

Pattern:
```python
mcp = FastMCP("destinations-adapter")

@mcp.tool()
def get_destinations(vibes: str, budget: str) -> list[dict]:
    return httpx.get("http://traefik-api-gw.lab/destinations/v1/destinations", ...).json()
```

### REST servers (`applications/rest-servers/`)

Go HTTP servers — identical to those in the Kong AI Lab. All three have OTEL SDK wired in the application code (same fanout handler: stdout JSON + OTLP). OTEL env vars are set in their respective `k8s/manifests/rest-*.yaml` files.

The dice-roller Go MCP server (`applications/mcp-servers/dice-roller/`) is also OTEL-instrumented.

---

## Conventions

- Use `.lab` hostnames everywhere — `traefik-api-gw.lab`, `litellm.lab`, `agentgateway.lab`, `keycloak.lab:8080`, `grafana.lab:3000`. They resolve in-cluster (CoreDNS rewrite) and on Mac (`/etc/hosts`). Never hardcode `172.18.x.x` IPs.
- Traefik routes go in `k8s/oss/routes/rest-apis.yaml` and use `GatewayClass: traefik`.
- agentgateway routes go in `k8s/oss/routes/langgraph-agent.yaml` (HTTPRoute) and `k8s/oss/routes/mcp-servers.yaml` (AgentgatewayBackend). Both use `GatewayClass: agentgateway`.
- LiteLLM config lives in `k8s/oss/litellm/config.yaml` as a ConfigMap.
- LangGraph agent k8s resources live in `k8s/oss/langgraph/`.
- MCP adapter source lives in `applications/mcp-servers/`.
- Agent source lives in `applications/agents/travel-agency-langgraph/`.
- Reusable application code belongs in `applications/`, not in `k8s/`.
- Do not put route auth config in this repo — there is none.

---

## Observability

LGTM stack at `grafana.lab:3000` (admin/admin). Receives OTLP push on port 4318 — no scraping.

**What's currently wired:**
- REST servers (all 3): OTEL traces + logs + metrics via OTLP
- `mcp-dice-roller`: OTEL traces + logs + metrics via OTLP

**What's not wired (gap vs Kong AI Lab):**
- LangGraph agents: no OTEL SDK — only LangSmith on the orchestrator
- LiteLLM: Prometheus `/metrics` endpoint, not pushed to LGTM
- agentgateway: stdout logs only

To view what's in Grafana: Explore → Tempo (traces from REST/MCP tier), Explore → Loki (logs from all components).

---

## Taskfile cheat sheet

```bash
# Cluster
task up                       # Full setup
task down                     # Delete cluster
task build                    # Build + load all images

# Iteration
task oss:manifests-apply      # Reapply all OSS overlays (gateway config, agents, LLM)
task oss:langgraph-apply      # Rebuild + redeploy the 5 LangGraph agents only
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
- Do not add TypeScript agent runtime code; use Python + LangGraph.
- Do not hardcode LoadBalancer IPs; use `.lab` hostnames.
- Do not create a `deck/` directory or any Kong-specific subdirectory.
