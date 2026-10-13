<p align="center">
  <img src="ai-lab.png" alt="Kong AI Lab OSS" width="600"/>
</p>

# AI Lab — Live Deployment (`shared-pve-home`)

A multi-agent travel-booking system — LangChain v1 / LangGraph orchestrator + 4 specialist
agents, coordinating over A2A and MCP through agentgateway — running on a real, persistent
Kubernetes cluster (`shared-pve-home`), deployed via FluxCD GitOps, evaluated and traced with
LangSmith.

> Looking for the original, throwaway local-Kind version of this lab instead? See
> [`README-portable-lab.md`](README-portable-lab.md). This document describes how the system
> actually runs day to day; that one is for fully self-contained, offline, laptop-only use.

---

## Contents

- [Architecture](#architecture)
- [The agents](#the-agents)
- [Human-in-the-loop booking approval](#human-in-the-loop-booking-approval)
- [Evaluation](#evaluation)
- [Observability](#observability)
- [How it's deployed](#how-its-deployed)
- [Repository structure](#repository-structure)
- [Hostnames](#hostnames)
- [Known gaps](#known-gaps)

---

## Architecture

```
Client
  │
  └── agentgateway.shared.pve-home.schenkeveld.io
        ├── /a2a/travel-orchestrator ──► orchestrator (LangChain v1 create_agent, port 4200)
        │     ├── /a2a/destination ──────────► destination specialist
        │     ├── /a2a/destination-decision ─► destination-decision specialist
        │     ├── /a2a/flight-finder ────────► flight-finder specialist
        │     └── /a2a/flight-booker ────────► flight-booker specialist
        ├── /mcp-destinations ──► Python FastMCP adapter ──► REST destinations backend
        ├── /mcp-flights ────────► Python FastMCP adapter ──► REST flights backend
        ├── /mcp-book-flights ──► Python FastMCP adapter ──► REST book-flights backend
        └── /mcp-dice-roller ────► Go MCP server
```

Each component runs in its own Kubernetes namespace. The orchestrator persists conversation
state per `thread_id` in a dedicated database on the cluster's shared CNPG Postgres instance
(`AsyncPostgresSaver`); if Postgres is unreachable it degrades to stateless runs rather than
failing startup. LiteLLM (shared across the cluster) fronts the LLM calls — chat completions for
the orchestrator, and separately for the evaluation suite's LLM-as-judge.

---

## The agents

| Agent | Responsibility |
|---|---|
| `orchestrator` | LangChain v1 `create_agent` tool-calling loop; calls the 4 specialists below as tools over A2A |
| `destination` | Shortlists destinations via the `list_destinations` MCP tool |
| `destination-decision` | Picks one destination via the dice-roller MCP tool |
| `flight-finder` | Looks up a flight via the `flight_price` MCP tool |
| `flight-booker` | Books the selected flight via the `create_booking` MCP tool |

Only the orchestrator builds an agent graph; the specialists are plain FastAPI services calling
MCP tools via `langchain-mcp-adapters`' `MultiServerMCPClient`. Every service exposes
`/.well-known/agent-card.json`, `/a2a/jsonrpc`, and `/health`; the orchestrator additionally
exposes `/pending` and `/approve` for the approval gate below.

The orchestrator and all 4 specialists run at a deliberate `replicas: 1` — the orchestrator
because there's no distributed locking around a `thread_id` yet (a real, stated limitation, not
an oversight), and the MCP adapters because the MCP streamable-http protocol is session-scoped
and agentgateway doesn't yet support session-affine routing across MCP backend replicas
(confirmed live: a second replica caused a real `CrashLoopBackOff` on a sibling adapter).

---

## Human-in-the-loop booking approval

`book_flight` pauses via LangGraph's `interrupt()` before actually booking — the one
irreversible, money-committing step — and waits for a human decision:

1. A request that reaches booking comes back with `status: "awaiting_approval"` and a
   `pending_approval` object, not a final answer.
2. `GET /a2a/travel-orchestrator/pending` lists every run currently paused this way, keyed by
   `thread_id`.
3. `POST /a2a/travel-orchestrator/approve` with `{"thread_id": "...", "approved": true|false}`
   resumes that exact run — approving completes the booking, rejecting reports back cleanly that
   nothing was booked (confirmed live: the booking specialist is never even called on reject).

```bash
cd applications/agents/travel-agency-langgraph/orchestrator
ORCHESTRATOR_URL=http://agentgateway.shared.pve-home.schenkeveld.io uv run python approve_cli.py
```

polls `/pending`, shows each proposed booking, and prompts `Approve? [y/n]`.

---

## Evaluation

`applications/agents/travel-agency-langgraph/evals/` holds the evaluation suite, run against the
*live* orchestrator (not a mock):

- **`dataset.py`** — 13 examples across 5 categories (happy path, missing values, a guaranteed
  no-flight case, a documented-not-faked retry limitation, ambiguous free text), landing in the
  LangSmith dataset `travel-orchestrator-trip-requests`.
- **`evaluators.py`** — 9 deterministic evaluators plus one LLM-as-judge evaluator (for the one
  dimension — destination/vibe fit — that genuinely needs semantic judgment, not exact match).
- **`run_eval.py`** — the runner: calls the deployed orchestrator once per example, scores each
  result via LangSmith's `evaluate()`, then runs two sequential checks outside that framework
  (conversation-state isolation, and the approval gate itself — `evaluate()` scores independent
  examples concurrently, with no story for ordered/stateful flows).

```bash
cd applications/agents/travel-agency-langgraph/evals
uv sync
export LANGCHAIN_API_KEY=<LangSmith EU key>
export LITELLM_BASE_URL=... LITELLM_API_KEY=...   # for the LLM-judge; see evaluators.py
uv run run_eval.py
```

This is a test suite, not an optimization tool: it tells you *where* the agent needs improving
(a failing evaluator), the actual improvement happens afterward, in the prompt or code.

---

## Observability

Two separate, complementary signals:

- **LangSmith** (EU region — `eu.smith.langchain.com`, not the default US endpoint) — agent/tool
  call semantics. All 5 services propagate trace context across each A2A hop
  (`call_specialist()` + `tracing_context(parent=...)`), so a single uninterrupted request nests
  into one trace tree. **Known limitation:** this does not currently span the human-approval
  pause — `Command(resume=...)` starts a logically separate trace from the original paused run,
  since the SDK has no built-in way to continue a trace across a human-decision-length gap (see
  `docs/friction-log.md` for the full writeup and a scoped, deliberately-deferred fix).
- **SigNoz** (self-hosted, `signoz.shared.pve-home.schenkeveld.io`, ClickHouse-backed) —
  generic HTTP-level OTEL traces/logs/metrics. Covers the 5 LangGraph agents and the 3 Python MCP
  adapters; the Go REST backends and the dice-roller MCP server have correct OTEL config but
  currently produce no spans there — an open, unresolved gap (not investigated further; purely
  cosmetic, doesn't affect LangSmith or application behavior).

---

## How it's deployed

FluxCD GitOps from a separate `gitops` repository — not this one. Each component has its own
`prerequisites`/`deployment` Kustomization pair; application images are built by GitHub Actions
in *this* repo and pushed to GHCR (`ghcr.io/maschenkeveld/ai-lab/<name>`), tagged `latest` plus
the commit SHA, with the gitops repo's Flux `postBuild.substitute` pinning the tag each push
bumps. There is no `task up`/Kind/Colima step for this deployment target — that workflow only
applies to the portable lab.

---

## Repository structure

```
applications/
  agents/travel-agency-langgraph/
    orchestrator/                  # LangChain v1 agent + FastAPI (graph.py, server.py, approve_cli.py)
    specialists/
      destination/ destination-decision/ flight-finder/ flight-booker/
    evals/                         # dataset.py, evaluators.py, run_eval.py, tests/
  mcp-servers/
    dice-roller/                   # Native Go MCP server
    destinations-adapter/ flights-adapter/ book-flights-adapter/   # Python FastMCP adapters
  rest-servers/                    # Go REST APIs (flights, destinations, book-flights)
```

`k8s/` and the Kind-specific manifests referenced by `README-portable-lab.md` are unrelated to
this deployment target — the live system's manifests live in the separate `gitops` repository.

---

## Hostnames

| Hostname | Service |
|---|---|
| `agentgateway.shared.pve-home.schenkeveld.io` | A2A + MCP routes |
| `signoz.shared.pve-home.schenkeveld.io` | SigNoz UI |

LiteLLM has no external route in this deployment — it's ClusterIP-only; reach it via
`kubectl --context shared-pve-home -n litellm port-forward svc/litellm 14000:4000` when needed
(e.g. to run the eval suite's LLM-judge locally).

---

## Known gaps

- agentgateway routes are open — no auth, no scope enforcement on MCP or A2A endpoints.
- No distributed locking around a `thread_id` — two concurrent requests on the same thread could
  race (stated limitation, not yet hit in practice at this scale).
- LangSmith trace continuity breaks across the human-approval pause (see Observability above and
  `docs/friction-log.md`).
- SigNoz is missing spans from the Go REST backends and the dice-roller MCP server despite
  correct configuration — root cause not yet found.
- The LLM Analytics pipeline (Kafka → embeddings → clustering → dashboard), present in the
  portable-lab version, was explicitly scoped out of this deployment.
