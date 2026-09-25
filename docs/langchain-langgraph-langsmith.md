# LangChain, LangGraph, and LangSmith in this lab

This is a detailed rundown of the three LangChain-ecosystem pieces used by the travel-agency
multi-agent workflow (`applications/agents/travel-agency-langgraph/`), what each one is actually
responsible for, and exactly how they're wired together across the five agent microservices.

For the broader picture (Traefik, LiteLLM, agentgateway, MCP servers, REST APIs) see
[README.md](../README.md) and [AGENTS.md](../AGENTS.md). This doc only goes deep on the
LangChain/LangGraph/LangSmith layer.

---

## The five services, in one picture

```
POST /a2a/travel-orchestrator/plan
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ orchestrator (port 4200)                                     │
│ LangGraph create_react_agent — the only service with a       │
│ "graph" in the LangGraph sense                                │
│                                                                │
│  1. extract_requirements()  — LCEL chain (prompt | llm)       │
│  2. ReAct tool-calling loop, calling 4 @tool functions:        │
│       get_destination_shortlist → A2A → destination           │
│       pick_destination          → A2A → destination-decision  │
│       find_flight               → A2A → flight-finder         │
│       book_flight                → A2A → flight-booker        │
│                                                                │
│  LLM: ChatOpenAI → LiteLLM → OpenAI/Gemini/Ollama              │
│  Memory: PostgresSaver checkpointer, keyed by thread_id        │
│  Tracing: LangSmith (root run)                                 │
└───────────────┬───────────────┬───────────────┬───────────────┘
                │ A2A            │ A2A            │ A2A
                ▼                ▼                ▼
        destination      destination-decision  flight-finder   flight-booker
        (4201)           (4202)                (4203)          (4204)
        MultiServerMCPClient → agentgateway → MCP adapter → Go REST API
        Tracing: LangSmith (@traceable, nested under orchestrator's run)
```

All inter-service calls (orchestrator → specialist) go over **A2A JSON-RPC** through
`agentgateway`, not direct HTTP or a LangGraph subgraph — each specialist is a separate FastAPI
process with its own `/a2a/jsonrpc` endpoint. LangGraph itself only exists inside the
orchestrator's process; the specialists don't build a graph at all.

---

## LangChain

LangChain here means the `langchain-core` primitives plus two integration packages
(`langchain-openai`, `langchain-mcp-adapters`). It's the plumbing layer, not the orchestration
layer — LangGraph does the orchestrating.

### `langchain-openai` — talking to the LLM (orchestrator only)

`travel_orchestrator/graph.py` builds a chat model that points at the in-cluster LiteLLM proxy
instead of OpenAI directly:

```python
def build_llm() -> ChatOpenAI:
    return ChatOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_MODEL, temperature=0)
```

`LITELLM_BASE_URL` defaults to `http://litellm.litellm.svc.cluster.local:4000/v1`, and
`LITELLM_MODEL` defaults to the `openai-gpt` alias (→ `gpt-4o-mini`). Because LiteLLM speaks the
OpenAI wire protocol, `ChatOpenAI` doesn't know or care that requests are being proxied and
re-routed to OpenAI/Gemini/Ollama on LiteLLM's side — this is the same trick used everywhere
LangChain talks to a non-OpenAI backend through an OpenAI-compatible gateway.

Only the orchestrator imports `langchain-openai`. The four specialists never call an LLM
directly — `destination-decision` picks a destination by rolling a die via an MCP tool, and the
other three just shape data and call a REST-backed MCP tool. That keeps the LLM (and its cost,
latency, and non-determinism) confined to one place.

### LCEL — the requirements-extraction chain

Before the agent loop starts, the orchestrator runs a small **LangChain Expression Language**
chain to turn free text into structured data:

```python
REQUIREMENTS_CHAIN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Extract structured trip requirements from the traveller's request below."),
    ("human", "{input}"),
])

def extract_requirements(prompt: str, llm: ChatOpenAI) -> dict[str, Any]:
    chain = REQUIREMENTS_CHAIN_PROMPT | llm.with_structured_output(TripRequirements)
    requirements: TripRequirements = chain.invoke({"input": prompt.strip()})
    return requirements.model_dump()
```

The `|` operator composes a `Runnable` sequence: the prompt template formats the incoming text,
`with_structured_output(TripRequirements)` tells the model to return JSON matching a Pydantic
schema (passenger name, origin, date, budget level, vibes, activities — each with sensible
defaults baked into the field descriptions), and LangChain parses the response back into a
`TripRequirements` instance. This runs once per `/plan` call, before the agent (see below) ever
sees a message — it's plain LCEL, not a LangGraph node.

### `langchain-openai` — semantic re-ranking (destination specialist)

The `destination` specialist also uses `langchain-openai`, but for embeddings rather than chat:

```python
def get_embeddings() -> OpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY, model=LITELLM_EMBEDDING_MODEL)
    return _embeddings
```

`LITELLM_EMBEDDING_MODEL` defaults to LiteLLM's `embedding-openai` alias (→
`openai/text-embedding-3-small`). `get_destinations()` fetches a wider-than-needed candidate pool
from the `list_destinations` MCP tool (which filters by vibes/budget/activities server-side, in
Go, via keyword matching — see `applications/rest-servers/destinations/`), excludes the origin
airport, and then calls `rank_by_similarity()`:

```python
query = f"A {budget}-budget trip with vibes: {...}. Preferred activities: {...}."
query_vector = await embeddings.aembed_query(query)
document_vectors = await embeddings.aembed_documents([_destination_text(d) for d in candidates])
ranked = sorted(zip(candidates, document_vectors), key=lambda p: _cosine_similarity(query_vector, p[1]), reverse=True)
```

Each candidate is embedded as a short text built from its name, country, blurb, vibes, and
activities; cosine similarity against the trip-request embedding decides the final order before
truncating to `limit`. If the embedding call fails (LiteLLM/OpenAI unreachable), `rank_by_similarity`
catches the exception, logs a warning, and falls back to the keyword-filtered order from the MCP
tool — so a down embedding model degrades ranking quality rather than failing the request. This
re-ranking step itself is wrapped in `@traceable`, so it shows up as its own span nested under
`get_destinations` in the LangSmith trace.

Semantic ranking only happens in the `destination` specialist — the Go REST layer's own
`/v1/shortlist` endpoint (exposed as the separate `shortlist_destinations` MCP tool) still does
plain keyword-substring scoring and isn't called from this flow.

### `langchain-mcp-adapters` — MCP tools as LangChain tools (all 4 specialists)

Each specialist wraps `MultiServerMCPClient` around a single MCP endpoint reached through
agentgateway, and discovers that server's tools at startup via FastAPI's `lifespan`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _tools
    _mcp_client = MultiServerMCPClient({
        "destinations": {"url": f"{AGENTGATEWAY_URL}/mcp-destinations", "transport": "streamable_http"}
    })
    await _mcp_client.__aenter__()
    _tools = _mcp_client.get_tools()   # tools/list happens here
    yield
    await _mcp_client.__aexit__(None, None, None)
```

`get_tools()` returns LangChain `BaseTool` objects backed by the remote MCP tool — calling
`tool.ainvoke({...})` sends an MCP `tools/call` over the same StreamableHTTP connection. This is
the only thing `langchain-mcp-adapters` is used for; the specialists don't build agents or chains
around these tools, they just look one up by name and invoke it directly (`next(t for t in
_tools if t.name == "list_destinations")`).

| Specialist | MCP server (via agentgateway) | Tool called |
|---|---|---|
| `destination` | `/mcp-destinations` | `list_destinations` |
| `destination-decision` | `/mcp-dice-roller` | `roll-20` |
| `flight-finder` | `/mcp-flights` | `flight_price` |
| `flight-booker` | `/mcp-book-flights` | `create_booking` |

The orchestrator does **not** use `langchain-mcp-adapters` — it never talks to MCP directly. Its
four `@tool`-decorated functions (`get_destination_shortlist`, `pick_destination`, `find_flight`,
`book_flight`) are plain LangChain tools whose bodies make an A2A JSON-RPC HTTP call to a
specialist; the specialist is the one holding the MCP connection.

> **Two separate embedding models in this repo, worth not confusing:** the `destination`
> specialist's `OpenAIEmbeddings` (above) is the only consumer of LiteLLM's `embedding-openai`
> alias. The LLM-analytics `ingestor` (`applications/llm-analytics/ingestor/`) also generates
> embeddings, but independently — it runs a local `sentence-transformers/all-MiniLM-L6-v2` model
> to embed LLM prompt logs for DBSCAN clustering, unrelated to LiteLLM or trip planning.

---

## LangGraph

LangGraph appears in exactly one place: the orchestrator. There is no hand-authored
`StateGraph` — `graph.py`'s docstring calls this out explicitly ("replaces the hand-authored
StateGraph"). Instead it uses the prebuilt ReAct agent constructor:

```python
def build_agent():
    llm = build_llm()
    checkpointer = build_checkpointer()
    return create_react_agent(llm, TOOLS, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)
```

`create_react_agent` builds a small graph under the hood (an LLM node that decides whether to
call a tool, a tool-execution node, and an edge that loops back to the LLM until it stops calling
tools) — but none of that topology is authored by hand here. The actual control flow ("call
get_destination_shortlist, then pick_destination, then find_flight, retrying with a growing
exclusion list on failure, then book_flight") lives entirely in `SYSTEM_PROMPT` as natural-language
instructions the model follows, not in graph edges. This is a deliberate trade: less code, but the
workflow's correctness now depends on the LLM reliably following prompt instructions rather than
on structurally-enforced graph transitions.

### The four tools

```python
TOOLS = [get_destination_shortlist, pick_destination, find_flight, book_flight]
```

Each is a thin `@tool`-decorated wrapper around `call_specialist(base_url, payload)`, which does
an A2A `tasks/send` JSON-RPC POST to a specialist through agentgateway and unpacks the JSON
payload the specialist embeds in its response text. The interesting retry logic is entirely
prompt-driven: `find_flight` returns `{"flight": null}` on failure (never raises), and the system
prompt tells the model to call `pick_destination` again with the failed IATA code added to
`tried_iata_codes`, then retry `find_flight` — the model, not the graph, drives this loop.

### Runtime entry points

`server.py` (the orchestrator's FastAPI app) exposes two ways to drive the agent:

- `run_agent()` → `agent.ainvoke(...)`, used by `/plan` and the A2A `/a2a/jsonrpc` endpoint —
  runs to completion and returns a single structured result (`answer`, `requirements`,
  `shortlist`, `selected_destination`, `flight`, `booking`), reconstructed in
  `summarize_result()` by scanning the final message list for each tool's last output.
- `stream_agent()` → `agent.astream_events(..., version="v2")`, used by `/plan/stream` — yields
  LangGraph's event stream (token deltas, tool start/end) over Server-Sent Events, reduced by
  `serialize_event()` into a small JSON-serializable shape per event.

### State and memory — `PostgresSaver`

```python
def build_checkpointer():
    global _pool
    try:
        _pool = ConnectionPool(conninfo=CHECKPOINT_DB_URL, max_size=10, kwargs={"autocommit": True, "prepare_threshold": 0})
        checkpointer = PostgresSaver(_pool)
        checkpointer.setup()
        return checkpointer
    except Exception:
        logger.warning(...)
        return None
```

`CHECKPOINT_DB_URL` points at the same Postgres instance the `llm-analytics` pipeline uses
(`postgres.llm-analytics.svc.cluster.local`), just a different concern (LangGraph checkpoints,
not LLM usage logs/embeddings). The checkpointer persists conversation state — including which
destinations have already been tried — keyed by `thread_id`, passed via
`config = {"configurable": {"thread_id": thread_id}}` on every `ainvoke`/`astream_events` call. A
caller resumes a trip-planning conversation by reusing the same `thread_id` on a later `/plan`
call; omitting it starts fresh. If Postgres isn't reachable at startup, `build_checkpointer()`
swallows the exception and returns `None` — the orchestrator still runs, just without
cross-request memory (each call is a one-shot, stateless run).

The agent object itself is built lazily and cached as a module-level singleton (`get_agent()`),
so the checkpointer's connection pool is set up once per pod, not per request.

### What LangGraph is *not* used for here

- The four specialists don't use LangGraph at all — no graph, no `create_react_agent`, not even
  as a dependency (check any specialist's `pyproject.toml`: no `langgraph` package).
- There's no multi-agent LangGraph construct (no supervisor graph, no subgraphs-as-nodes) tying
  the five services together — that coordination happens over A2A/HTTP, driven by the
  orchestrator's system prompt.

---

## LangSmith

LangSmith is the tracing/observability layer for the LangChain/LangGraph calls specifically —
separate from, and not visible in, the OTLP/Grafana-LGTM tracing that the REST servers and
`mcp-dice-roller` use (see AGENTS.md's Observability section). It's opt-in: everything works with
`LANGCHAIN_API_KEY` unset, just without traces.

### Enabling it

`task secrets:langsmith` reads `LANGCHAIN_API_KEY` from `.env` and creates a `langsmith-secret`
k8s Secret in the `travel-agency-langgraph` namespace (skipped with a log line if the key is
absent). Each of the five Deployments (`k8s/oss/langgraph/*.yaml`) sets:

```yaml
- name: LANGCHAIN_TRACING_V2
  value: "true"
- name: LANGCHAIN_PROJECT
  value: ai-lab-oss
- name: LANGCHAIN_API_KEY
  valueFrom:
    secretKeyRef: { name: langsmith-secret, key: api-key, optional: true }
```

`LANGCHAIN_TRACING_V2=true` is what makes `langsmith`/LangChain's global tracer active; when the
Secret key is missing (`optional: true`), the env var is simply absent and tracing degrades to a
no-op rather than the pod failing to start.

### One trace tree across five services

The interesting part isn't that tracing is on — it's that all **five** services' spans land in a
single nested trace per trip-planning request, even though each specialist is an independent
FastAPI process reached over HTTP/A2A. That's done with explicit trace-context propagation:

**Orchestrator side** — after a tool's LLM-driven call kicks off a LangSmith run, `call_specialist`
grabs the current run and turns it into propagatable headers:

```python
run_tree = get_current_run_tree()
if run_tree is not None:
    headers = run_tree.to_headers()
...
response = client.post(f"{base_url}/a2a/jsonrpc", json=body, headers=headers)
```

**Specialist side** — every specialist's `/a2a/jsonrpc` handler reads those headers back out of
the inbound request and opens a `tracing_context` scoped to them before doing its work:

```python
@app.post("/a2a/jsonrpc")
async def jsonrpc(request: JsonRpcRequest, http_request: Request) -> dict[str, Any]:
    payload = extract_json(request.params or {})
    with tracing_context(parent=dict(http_request.headers)):
        result = await get_destinations(payload)   # @traceable
    ...
```

The specialist's actual logic function is wrapped in `@traceable` (`get_destinations`,
`pick_destination`, `find_flight`, `book_flight` — one per specialist), so entering
`tracing_context(parent=...)` with the orchestrator's headers makes that `@traceable` run attach
as a **child** of the orchestrator's tool-call span instead of starting its own root trace. The
net effect: opening one trace in the LangSmith `ai-lab-oss` project for a `/plan` call shows the
full call tree — requirement extraction → agent LLM turns → each tool call → the corresponding
specialist's traced function — even though four of those five hops crossed a process and network
boundary via agentgateway.

Routing A2A calls through agentgateway doesn't add anything to this trace propagation — it's
plain HTTP proxying from agentgateway's point of view, so the LangSmith trace headers pass through
untouched; agentgateway itself isn't LangSmith-aware and doesn't appear as a span.

### Where this shows up, and where it doesn't

- **In LangSmith**: full node/edge/LLM-call detail for the orchestrator's agent loop, plus each
  specialist's traced function, nested into one tree per `thread_id`/request, in project
  `ai-lab-oss`.
- **In Grafana Tempo (OTLP)**: nothing from the LangGraph agents — they have no OTEL SDK wired in
  (`otel.py` in each agent package only sets up FastAPI/httpx auto-instrumentation for *their own*
  HTTP layer, unrelated to LangSmith; see AGENTS.md's "Known gaps" for the status of OTEL vs.
  LangSmith coverage across the lab). LangSmith and OTLP tracing are two independent signals here
  that happen to both originate from the same requests.

---

## Quick reference: dependencies per service

| Service | `langgraph` | `langchain-openai` | `langchain-mcp-adapters` | `langsmith` |
|---|---|---|---|---|
| `orchestrator` | ✓ (`create_react_agent`, `PostgresSaver`) | ✓ (`ChatOpenAI`) | — | ✓ (root traces) |
| `destination` | — | ✓ (`OpenAIEmbeddings`) | ✓ | ✓ (`@traceable`) |
| `destination-decision` | — | — | ✓ | ✓ (`@traceable`) |
| `flight-finder` | — | — | ✓ | ✓ (`@traceable`) |
| `flight-booker` | — | — | ✓ | ✓ (`@traceable`) |

Only the orchestrator talks to a chat LLM or builds a graph; `destination` is the one specialist
that also calls an LLM-adjacent model (embeddings, for re-ranking) rather than just shaping an MCP
tool call into an A2A response. LangSmith stitches every specialist's spans back into the
orchestrator's trace regardless.
