# AI Lab — Demo Guide (live deployment, `shared-pve-home`)

> This describes the system as it actually runs — a real, persistent Kubernetes deployment, not
> a local Kind cluster. `agentgateway.shared.pve-home.schenkeveld.io` is a LAN-private hostname
> (no public route), so these commands only work from the same home network this cluster runs
> on — this document exists to show what the system does and how it's exercised, not as a
> generic "clone and run anywhere" script. For the fully portable, laptop-local version, see
> [`DEMO-portable-lab.md`](DEMO-portable-lab.md).

## Setting the scene

Five independent Python microservices — a LangChain v1 orchestrator plus four specialist
agents — coordinating over A2A JSON-RPC through agentgateway, with MCP tools fronting the Go
REST backends:

```
Client → agentgateway → orchestrator (LangChain v1 create_agent)
                          → agentgateway → destination
                          → agentgateway → destination-decision
                          → agentgateway → flight-finder
                          → agentgateway → flight-booker   ⏸ pauses for human approval here
```

**Tip for any live demo:** every agent service logs its own activity to stdout as it happens —
requirements extracted, each tool call and result, final status. Keep this open in a second
terminal while running the commands below:

```bash
kubectl --context shared-pve-home logs -n travel-agency-langgraph \
  -l 'app in (travel-orchestrator-langgraph,travel-destination,travel-destination-decision,travel-flight-finder,travel-flight-booker)' \
  -f --prefix --max-log-requests=10
```

---

## Sanity checks

```bash
# All 5 agent pods healthy
kubectl --context shared-pve-home get pods -n travel-agency-langgraph

# All 5 agent cards reachable through agentgateway
for a in travel-orchestrator destination destination-decision flight-finder flight-booker; do
  curl -s http://agentgateway.shared.pve-home.schenkeveld.io/a2a/$a/.well-known/agent-card.json | jq -r .name
done
```

---

## Demo 1 — MCP tools via agentgateway

The MCP streamable-http protocol is session-scoped: `initialize` first to get a session ID, then
pass it on every subsequent call.

```bash
SESSION=$(curl -s -D - -o /dev/null -X POST http://agentgateway.shared.pve-home.schenkeveld.io/mcp-flights \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"1.0"}}}' \
  | grep -i '^mcp-session-id:' | tr -d '\r' | cut -d' ' -f2)

curl -s -X POST http://agentgateway.shared.pve-home.schenkeveld.io/mcp-flights \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq -r '.result.tools[].name'
```

| MCP endpoint | Tools |
|---|---|
| `/mcp-flights` | `list_airports`, `flight_price` |
| `/mcp-destinations` | `list_destinations`, `shortlist_destinations`, `get_destination` |
| `/mcp-book-flights` | `create_booking`, `list_bookings` |
| `/mcp-dice-roller` | `roll-2`, `roll-6`, `roll-20` |

The backends are plain REST services — no MCP code in them. agentgateway handles the protocol
conversion; the Python adapters in `applications/mcp-servers/*-adapter/` wrap each REST API.

---

## Demo 2 — A2A multi-agent workflow + human-in-the-loop approval

### Trigger a booking

```bash
curl -s -X POST http://agentgateway.shared.pve-home.schenkeveld.io/a2a/travel-orchestrator/a2a/jsonrpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": "demo-1", "method": "message/send",
    "params": {
      "message": {
        "kind": "message", "messageId": "demo-msg-1", "role": "user",
        "parts": [{"kind": "text", "text": "{\"passenger_name\": \"Demo\", \"origin\": \"AMS\", \"date\": \"2026-09-01\", \"activities\": [\"museums\"], \"vibes\": [\"culture\"], \"budget_level\": \"high\"}"}],
        "metadata": {"thread_id": "demo-1"}
      }
    }
  }' | jq .
```

The response includes `shortlist`, `selected_destination`, `flight`, and
`status: "awaiting_approval"` — booking is the one irreversible, money-committing step, so
`book_flight` pauses via LangGraph's `interrupt()` instead of completing automatically.

### Approve (or reject) it

```bash
# See what's waiting
curl http://agentgateway.shared.pve-home.schenkeveld.io/a2a/travel-orchestrator/pending | jq .

# Approve — completes the booking
curl -X POST http://agentgateway.shared.pve-home.schenkeveld.io/a2a/travel-orchestrator/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-1", "approved": true}' | jq .

# Or reject — confirms nothing was booked (the booking specialist is never even called)
curl -X POST http://agentgateway.shared.pve-home.schenkeveld.io/a2a/travel-orchestrator/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "demo-1", "approved": false}' | jq .
```

Or use the approval CLI instead of raw curl:

```bash
cd applications/agents/travel-agency-langgraph/orchestrator
ORCHESTRATOR_URL=http://agentgateway.shared.pve-home.schenkeveld.io uv run python approve_cli.py --once
```

It polls `/pending`, shows the proposed booking, and prompts `Approve? [y/n]`.

---

## Demo 3 — Observability

**LangSmith** (EU region — `eu.smith.langchain.com`): project `ai-lab-oss` under Tracing shows
one nested trace per request, spanning all 5 services via trace-header propagation across each
A2A hop. Known limitation: this doesn't currently span the approval pause itself — approving
resumes the run but starts a separate trace, not a continuation of the original (see
`docs/friction-log.md` for the full writeup — not in this repo, generated locally).

**Datasets & Experiments** (same LangSmith workspace): dataset `travel-orchestrator-trip-requests`
holds the offline evaluation results — see [Demo 4](#demo-4--evaluation-suite) below for how
those get produced.

**SigNoz** (self-hosted, `signoz.shared.pve-home.schenkeveld.io`): generic HTTP-level OTEL
traces/logs/metrics for the 5 agents and the 3 Python MCP adapters. The Go REST backends and the
dice-roller MCP server don't currently produce spans there despite correct config — a known,
unresolved gap, purely cosmetic.

---

## Demo 4 — Evaluation suite

```bash
cd applications/agents/travel-agency-langgraph/evals
uv sync   # first time only
export LANGCHAIN_API_KEY=<LangSmith EU key>
export LITELLM_BASE_URL=http://localhost:14000/v1   # needs the port-forward below
export LITELLM_API_KEY=<litellm master key, from the litellm-api-key secret in ns litellm>
kubectl --context shared-pve-home -n litellm port-forward svc/litellm 14000:4000 &
uv run run_eval.py
```

Runs 13 test cases (`dataset.py`) against the live orchestrator, scores each with 9 deterministic
evaluators plus an LLM-as-judge (`evaluators.py`), and reports a scored grid to LangSmith. This
is a test suite, not an optimization tool — it tells you where the agent needs improving; the
improvement itself happens afterward, in the prompt or code.

---

## Useful commands

```bash
# Watch all travel agent pods
kubectl --context shared-pve-home get pods -n travel-agency-langgraph -w

# Logs from a specific specialist
kubectl --context shared-pve-home logs -n travel-agency-langgraph -l app=travel-flight-finder -f

# Restart the orchestrator (e.g. to pick up a new image after a GitHub Actions build)
kubectl --context shared-pve-home rollout restart deployment/travel-orchestrator-langgraph -n travel-agency-langgraph

# Check every pod across the system is healthy
kubectl --context shared-pve-home get pods -A | grep -v Running | grep -v Completed
```
