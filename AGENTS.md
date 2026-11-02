# AGENTS.md: AI Lab OSS operating guide

This repository is an open-source AI platform lab. All components are OSS; there is no Kong Gateway, no Konnect, no proprietary control-plane config.

---

## Purpose

A runnable reference for agentic AI on open-source infrastructure, for demos and for Field Engineering engagements, and the OSS side of a comparison with the Kong AI Lab (same kinds of parts, different stack). Optimise for: easy to run on a laptop, easy to explain in a demo, open protocols (A2A, MCP, OpenAI-compatible API), no proprietary components. It is a lab, not a product.

## Goal

Build and maintain a local OSS AI lab using:

- **Traefik OSS**: Kubernetes Gateway API HTTP routing for REST APIs
- **LiteLLM Proxy**: OpenAI-compatible LLM gateway (multi-provider, key auth), including a local model served by vLLM-Metal
- **agentgateway**: MCP tool routing and A2A agent-to-agent traffic
- **Python + Pydantic AI + FastA2A + DBOS**: the travel desk, a durable multi-agent workflow with human approval
- **FastMCP**: thin Python adapters that expose Go REST APIs as MCP tools
- **Postgres**: state for the desk (its own `desk-postgres`) and for LLM analytics
- **Direct Kafka**: KRaft broker exposed as a LoadBalancer (no Kafka proxy layer)
- **Keycloak**: OIDC identity provider (configured, not yet enforced on routes)
- **Grafana LGTM**: OTel collector, Loki, Tempo, Prometheus, Grafana in one container

The point of the lab is agents that interact over open protocols (A2A, MCP, an OpenAI-compatible LLM API) through a gateway, durably and with people in the loop. Keep the agents framework-light and the protocols open.

Do NOT add: Kong gateway config, Konnect control-plane resources, TypeScript agent runtime code, or proprietary managed-cloud bootstrap files.

---

## Cluster

Kind cluster name: `ai-lab-oss`. The node image is pinned in `k8s/kind-config.yaml` (Kubernetes v1.34.0): kind's default image drifts, and agentgateway v1.2.0's CRDs fail validation on Kubernetes 1.37. Bump the pin deliberately, not by accident.

This lab is local-only: Colima + kind on macOS, nothing shared. Copy `.env.example` to `.env` and set `OPENAI_KEY` for real LLM calls, or the `VLLM_*` block for a local model (the cluster comes up without either).

All cluster operations run from `k8s/`:

```bash
cd k8s
task doctor                   # preflight: tools, Colima network address, .env keys
task up                       # full setup (ends with sudo steps: colima:route, hosts:apply)
task down                     # delete cluster
task build                    # rebuild all local images
task oss:manifests-apply      # reapply gateway/desk/LLM config (fast iteration)
task build:desk               # rebuild only the 5 desk agent images
task oss:desk-apply           # redeploy + restart only the desk
task desk:model -- local-vllm # point the desk agents at another LiteLLM alias
task eval                     # evals against the live lab
task hosts:apply              # update Mac /etc/hosts
task dns:apply                # update in-cluster CoreDNS rewrites
```

After a Colima restart: `task colima:route` to restore MetalLB routing.

---

## File layout

```
applications/
  agents/travel-desk/               the travel desk, one uv project each
    common/                         travel_common: model/MCP/OTel wiring, A2A client+server helpers, contracts
    coordinator/                    durable workflow, approvals, REST API, web app (port 4300)
    destination-scout/              LLM agent: shortlist + rank (4301)
    flight-scout/                   LLM agent: quote dates + recommend (4302)
    booking-agent/                  no LLM: hold / confirm / cancel (4303)
    fare-watcher/                   durable agent: watches fares (4304)
    evals/                          pydantic-evals: extraction + live end-to-end
    desk_cli.py                     drive the desk from a terminal
  keycloak/                         Realm export (imported at cluster startup)
  llm-analytics/                    Kafka → Postgres analytics pipeline
    ingestor/  analyzer/  api/  ui/
  mcp-servers/
    travel-search-adapter/          Python FastMCP → REST destinations + flights
    bookings-adapter/               Python FastMCP → REST book-flights (hold/confirm/cancel)
  rest-servers/
    destinations/                   Go REST API
    flights/                        Go REST API (opt-in moving fares)
    book-flights/                   Go REST API (SQLite-backed; holds, idempotency)

agentic-examples/                   standalone examples, one self-contained uv project each (no cluster)
inference/                          vLLM-Metal scripts (run, check, host discovery)
llm/                                chat templates for local models

k8s/
  Taskfile.yaml                     All cluster tasks
  helm/                             Helm values: metallb, traefik, keycloak, agentgateway
  manifests/                        Base services: kafka, otel-lgtm, postgres, redis, REST servers, MCP servers
  oss/
    agentgateway/                   Gateway `agentgateway-proxy` (GatewayClass: agentgateway)
    kafka/                          Direct external Kafka listener
    litellm/                        LiteLLM Deployment, ConfigMap, Service
    routes/                         HTTPRoutes: rest-apis, travel-desk, mcp-servers, litellm
    travel-desk/                    Desk Deployments, Services, Postgres (namespace travel-desk)
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
  name: mcp-travel-search
  namespace: agentgateway-system
spec:
  mcp:
    targets:
      - name: travel-search
        static:
          host: mcp-travel-search.mcp-travel-search.svc.cluster.local
          port: 8000
          path: /mcp
          protocol: StreamableHTTP
```

A2A and web routes use plain `HTTPRoute` (Gateway API v1) pointing at the `agentgateway-proxy` Gateway in `agentgateway-system`. They live in `k8s/oss/routes/travel-desk.yaml`. HTTPRoutes do not strip the path prefix, so each service serves under the same prefix it is routed on (`/a2a/<name>`, `/desk`).

The agentgateway Gateway CRD is at `k8s/oss/agentgateway/gateway.yaml`. The MetalLB IP is pinned in the Helm values.

### LiteLLM config (`k8s/oss/litellm/config.yaml`)

Stored as a ConfigMap mounted into the LiteLLM pod. Model aliases:

| Alias | Backend |
|---|---|
| `openai-gpt` | `openai/gpt-4o-mini` |
| `gemini-pro` | `gemini/gemini-2.5-pro` |
| `ollama-llama` | `ollama_chat/llama3.2` via in-cluster Ollama |
| `local-vllm` | vLLM-Metal on the Mac host (`VLLM_BASE_URL`, `VLLM_API_KEY`, `VLLM_BACKEND_MODEL` from the `litellm-env` Secret) |
| `embedding-openai` | `openai/text-embedding-3-small` |

Master key is `sk-ai-lab-litellm` (hardcoded for local demo). Change it via `task secrets:litellm` (sourced from `.env`) or the `litellm-env` Secret directly. To add a model: add a `model_list` entry to the ConfigMap and run `task oss:manifests-apply`.

Every agent reaches models through Pydantic AI's OpenAI-compatible model (`LITELLM_BASE_URL=http://litellm.litellm.svc.cluster.local:4000/v1`, alias from `LITELLM_MODEL`, default `openai-gpt`); `destination-scout` also uses `embedding-openai` for ranking.

### The travel desk (`applications/agents/travel-desk/`)

Each project is a uv project (`pyproject.toml` + `uv.lock`) with an editable path dependency on `common/` (package `travel_common`). Docker build context is `applications/agents/travel-desk`; images are `travel-desk-<name>`. Unit tests: `cd applications/agents/travel-desk/<project> && uv run pytest` (the coordinator's and fare-watcher's tests start a Postgres in Docker; they skip without it).

**Who does what.**
- `coordinator`: the DBOS workflow (`workflow.py`), the approval model and audit log (`store.py`, `policy.py`), the REST API and A2A handler (`api.py`, `service.py`), the SPA (`static/`), wiring (`server.py`). Research is plain code over the scouts (`agents.py`).
- `destination-scout`, `flight-scout`: LLM agents whose tools do the exact work and whose **output validators put the tool's facts back**, so a model can only add judgment (ranking reasoning, a recommended date).
- `booking-agent`: **no LLM**, by design. A `FunctionWorker` (`common/travel_common/function_worker.py`) runs a plain handler behind the A2A interface.
- `fare-watcher`: a DBOS workflow per watch (durable sleep between checks), a code rule for "significant drop", an LLM only to judge a drop that already passed the rule.

**Rules to keep.**
- *Code owns facts and money; models add judgment.* Never let a model decide who approves, what a booking costs, or copy an ID, date or price. Read the request in a tool through `current_request()`.
- *Tools run in their own asyncio task.* A value a tool stores in a `ContextVar` is invisible to the output validator. Share through the request dict (`current_request()["_facts"] = ...`), which is the same object everywhere.
- *DBOS workflows must be deterministic.* Every effect (an A2A call, a DB write, reading the clock) is a `@DBOS.step`; only step results and `recv`/`sleep` may drive control flow. Steps take and return JSON-serialisable values (no datetimes).
- *The booking is a saga.* Hold (idempotency key `<trip>:hold:<attempt>`), approve, confirm; every non-success path cancels the hold. A rebooking holds the new booking, confirms it, and only then cancels the old one.
- *Approval decisions go through `Store.decide`* (row lock, one decision per approver, exactly-once resolution), then `DBOS.send` to the workflow. Do not update approval rows elsewhere.
- *The audit log is append-only* (a trigger rejects UPDATE/DELETE). Add events with `Store.audit`; never try to edit history.
- *The vocabulary lives in the contract.* `TripRequirements` filters vibes, activities and budget to what the REST services accept; keep it that way when adding fields a model fills in.
- *One workflow engine per database*: the coordinator and fare-watcher run one replica with the `Recreate` strategy.

**Policy and timings** are environment variables on the coordinator (`k8s/oss/travel-desk/coordinator.yaml`): `AUTO_APPROVE_BELOW_EUR` (60), `SECOND_APPROVER_FROM_EUR` (180), `APPROVAL_TTL_SECONDS` (900), `ESCALATE_AFTER_SECONDS` (300), `MONITOR_SECONDS` (1800), `DEMO_MODE` (enables "simulate a fare drop"). The watcher checks every 30s, and flags a drop of at least 8% and 10 EUR.

**The REST API only accepts** `budget_level` of `low`/`mid`/`high` and vibes `city beach nature culture food nightlife relax design`; an unmatched combination returns an empty shortlist, not an error.

**A2A.** Agent card at `/a2a/<name>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<name>/`, `/health`. `message/send` is asynchronous (returns a submitted task; callers poll `tasks/get`, see `common/travel_common/a2a_client.py`). Pydantic AI 2.x has no `Agent.to_a2a()`; LLM agents use `fasta2a.pydantic_ai.AgentWorker` via `create_a2a_app`, which subclasses it (`DataAwareWorker`) because the stock bridge rejects `data` parts.

**Rebuild and redeploy** (images use `imagePullPolicy: Never` and `:latest`, so a restart is needed):
```bash
cd k8s
task build:desk-coordinator        # or any build:desk-<name>; `task build:desk` for all five
task oss:desk-apply                # apply manifests + rollout restart
```
`kubectl apply` of a manifest that contains `__OTEL_ENDPOINT__` must go through the Taskfile (it substitutes the endpoint); applying it raw points the pod at a nonexistent host.

**Evals** (`applications/agents/travel-desk/evals/`, pydantic-evals): `cd k8s && task eval`. The `extraction` suite scores how a model reads trip requests (`-- --suite extraction --model local-vllm` to compare models); the `desk` suite creates real trips, applies a human decision and checks policy, outcome, hold release and audit trail.

**The terminal client** `desk_cli.py` drives the REST API (`new`, `inbox`, `approve`, `reject`, `edit`, `cancel`, `drop`, `show`, `demo`).

### MCP adapters (`applications/mcp-servers/*-adapter/`)

Python packages using FastMCP. Each adapter:
1. Calls its REST service with httpx
2. Declares MCP tools with `@mcp.tool` decorators
3. Runs FastMCP's Starlette app at `0.0.0.0:8000` path `/mcp`

Agents reach them through agentgateway only (`agentgateway.lab/mcp-travel-search`, `/mcp-bookings`). MCP sessions are stateful and an agent keeps its session open: after replacing an MCP server pod, restart the agents that use it, or their tool calls return 404.

### REST servers (`applications/rest-servers/`)

Go HTTP servers, originally shared with the Kong AI Lab; two are extended here, additively (defaults unchanged). All three have the OTEL SDK wired in the application code (same fanout handler: stdout JSON + OTLP); OTEL env vars are set in `k8s/manifests/rest-*.yaml`.
- `book-flights` (`holds.go`): `hold`, `hold_seconds` and `idempotency_key` on create; `GET /v1/bookings/{code}`, `POST .../confirm`, `POST .../cancel`; states `held`, `scheduled`, `cancelled`, `expired`.
- `flights` (`market.go`): `FARE_TICK_SECONDS` makes fares move over time; `ENABLE_FARE_SHOCKS=true` enables `POST /v1/fare-shocks` for the demo's fare drop. The desk sets both.

The Go toolchain is not required locally: build through Docker (`task build:rest-flights`).

### Local inference (`inference/`, `llm/`)

vLLM-Metal serves a model on the Mac GPU: `task inference:install`, `task inference:run`, `task inference:check` (from `k8s/`). It is exposed as the `local-vllm` LiteLLM alias (the secret gets the host URL from `task inference:url`). Use Qwen2.5-14B-Instruct 4-bit with `VLLM_TOOL_PARSER=hermes` and `llm/qwen25-chat-template.jinja`. Mistral-7B-Instruct-v0.3 with the `mistral` parser returns tool calls as plain text and never stops, so tool-calling agents hang on it.

### Standalone examples (`agentic-examples/`)

Small runnable examples of one idea each, with no cluster needed. **Every folder is self-contained**: its own `pyproject.toml`,
`uv.lock`, `.venv`, `Taskfile.yml`, README and tests, and nothing imported from elsewhere (each has its own copy of `common.py`: the
model and tracing). Keep it that way: an example you can copy out and break is the point.
- `pydantic_model`: typed output, datasets as files, evals with an LLM judge (the reference layout)
- `tool_facts`: tools produce the facts, an output validator overwrites whatever a model returns (share state through `deps`, not a `ContextVar`: tools run in their own task)
- `mcp_tools`: `MCPToolset` over stdio (bundled server) or HTTP (the lab's); tools discovered, never named in the agent
- `human_approval`: `requires_approval=True`, `DeferredToolRequests`, `ToolDenied`
- `a2a_pair`: a FastA2A server and an agent whose tool is an A2A call (send, poll, read)
- `durable_approval`: a DBOS workflow on SQLite with a durable `recv`; `kill -9` and resume. DBOS runs steps in threads, so patch module-level functions in tests rather than using `agent.override`

Default model is `openai-gpt` via LiteLLM; `task run:local` uses vLLM on the Mac; `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` select anything OpenAI-compatible. Traces go to LGTM when `OTEL_EXPORTER_OTLP_ENDPOINT=http://grafana.lab:4318` is set. Run an example's tests with `cd agentic-examples/<folder> && task test`.

---

## Conventions

- Use `.lab` hostnames everywhere: `traefik-api-gw.lab`, `litellm.lab`, `agentgateway.lab`, `keycloak.lab:8080`, `grafana.lab:3000`. They resolve in-cluster (CoreDNS rewrite) and on Mac (`/etc/hosts`). Never hardcode `172.18.x.x` IPs.
- Traefik routes go in `k8s/oss/routes/rest-apis.yaml` and use `GatewayClass: traefik`.
- agentgateway routes go in `k8s/oss/routes/travel-desk.yaml` (HTTPRoute) and `k8s/oss/routes/mcp-servers.yaml` (AgentgatewayBackend + HTTPRoute). Both use `GatewayClass: agentgateway`.
- LiteLLM config lives in `k8s/oss/litellm/config.yaml` as a ConfigMap.
- Desk k8s resources live in `k8s/oss/travel-desk/`; desk source in `applications/agents/travel-desk/`; MCP adapter source in `applications/mcp-servers/`.
- Reusable application code belongs in `applications/`, not in `k8s/`.
- Do not put route auth config in this repo; there is none.

---

## Observability

LGTM stack at `grafana.lab:3000` (admin/admin). Receives OTLP push on port 4318; no scraping.

**What's currently wired:**
- REST servers (all 3): OTEL traces + logs + metrics via OTLP
- Desk agents (all 5) and the MCP adapters: OTEL traces + logs + metrics via OTLP (`setup_otel` in `common/travel_common/otel.py`: Starlette + httpx + Pydantic AI instrumentation). Trace context propagates across every A2A hop and into the REST servers, so a trip's research is one trace tree. An approval is a separate trace (it starts when a human decides).

**What's not wired (gap vs Kong AI Lab):**
- LiteLLM: Prometheus `/metrics` endpoint, not pushed to LGTM
- agentgateway: stdout logs only

To view what's in Grafana: Explore → Tempo (traces), Explore → Loki (logs from all components).

---

## Taskfile cheat sheet

```bash
# Cluster
task up                       # Full setup
task down                     # Delete cluster
task build                    # Build + load all images

# Iteration
task oss:manifests-apply      # Reapply all OSS overlays (gateway config, desk, LLM)
task build:desk               # Rebuild the 5 desk agent images
task oss:desk-apply           # Redeploy + restart the desk
task manifests:apply          # Reapply base manifests (kafka, otel-lgtm, REST servers, MCP servers)
task desk:model -- <alias>    # Point the desk at another LiteLLM alias
task eval                     # Run the evals against the live lab

# Local model
task inference:install        # once
task inference:run            # blocking terminal
task inference:check

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
- Do not let a model decide approvals, prices or IDs; code owns those.
