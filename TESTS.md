# Integration Tests

## Prerequisites

- Colima running with `--network-address`
- `cd k8s && task up` completed
- All pods Running: `kubectl get pods -A | grep -v Running | grep -v Completed`
- Mac routing in place: `task colima:route`

## How to run

Tell Claude: **"Run the integration tests"** — Claude will execute these checks live using curl, kubectl, and jq, and report each as ✓ PASS or ✗ FAIL with the actual response.

---

## Test suites

### 1. Cluster health

| Check | Command |
|---|---|
| All pods Running or Completed | `kubectl get pods -A` — no CrashLoopBackOff, no Pending, no Error |
| MetalLB IPs assigned | `kubectl get svc -A --field-selector spec.type=LoadBalancer -o wide` |
| Nodes Ready | `kubectl get nodes` |

---

### 2. Traefik — REST API routing

```bash
# Health endpoints
curl -sf http://traefik-api-gw.lab/flights/health && echo "PASS" || echo "FAIL"
curl -sf http://traefik-api-gw.lab/destinations/health && echo "PASS" || echo "FAIL"
curl -sf http://traefik-api-gw.lab/book-flights/health && echo "PASS" || echo "FAIL"

# Flights API
curl -sf "http://traefik-api-gw.lab/flights/v1/airports" | jq 'length > 0' # → true
curl -sf "http://traefik-api-gw.lab/flights/v1/price?from=AMS&to=LHR&date=2026-09-01" | jq '.fictive_price' # → number

# Destinations API
curl -sf "http://traefik-api-gw.lab/destinations/v1/destinations?vibes=adventure" | jq 'length > 0' # → true

# Book-flights health
curl -sf "http://traefik-api-gw.lab/book-flights/health" | jq '.ok' # → true
```

Expected: all return 200 with valid JSON bodies.

---

### 3. LiteLLM — multi-provider LLM routing

```bash
# Model list
curl -sf -H "Authorization: Bearer sk-ai-lab-litellm" http://litellm.lab/v1/models \
  | jq '.data[].id' # → "openai-gpt", "gemini-pro", "ollama-llama"

# Call each model alias
for MODEL in openai-gpt gemini-pro ollama-llama; do
  echo -n "$MODEL: "
  curl -sf http://litellm.lab/v1/chat/completions \
    -H "Authorization: Bearer sk-ai-lab-litellm" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with just: ok\"}],\"max_tokens\":5}" \
    | jq -r '.choices[0].message.content // "FAIL"'
done

# Bad key → 401
curl -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer bad-key" http://litellm.lab/v1/models # → 401
```

Expected: model list contains all three aliases; each completion returns a non-empty response.

---

### 4. agentgateway — MCP tool listing

All MCP endpoints are open (no auth).

```bash
for ENDPOINT in mcp-dice-roller mcp-destinations mcp-flights mcp-book-flights; do
  echo -n "$ENDPOINT tools: "
  curl -sf -X POST http://agentgateway.lab/$ENDPOINT \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
    | sed -n 's/.*"data": *\(.*\)/\1/p' \
    | jq -r '[.result.tools[].name] | join(", ")'
done
```

Expected:
- `mcp-dice-roller`: `roll_dice`
- `mcp-destinations`: `get_destinations`
- `mcp-flights`: `search_flights`
- `mcp-book-flights`: `book_flight`, `list_bookings`, `cancel_booking`

---

### 5. agentgateway — A2A agent cards

```bash
for AGENT in travel-orchestrator destination destination-decision flight-finder flight-booker; do
  echo -n "$AGENT card: "
  curl -sf http://agentgateway.lab/a2a/$AGENT/.well-known/agent-card.json \
    | jq -r '.name // "FAIL"'
done
```

Expected: each returns a JSON agent card with a `name` field.

---

### 6. A2A — full travel workflow (E2E)

```bash
curl -s -X POST http://agentgateway.lab/a2a/travel-orchestrator/a2a/jsonrpc \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 0.3.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-1",
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
```

Expected: JSON-RPC response with a `result` containing text that includes a destination, a flight, and a PNR booking reference (6-character alphanumeric string).

Verify all specialist agents were invoked:
```bash
for AGENT in travel-orchestrator travel-destination travel-destination-decision travel-flight-finder travel-flight-booker; do
  echo -n "$AGENT last log: "
  kubectl logs -n travel-agency-langgraph deploy/$AGENT --tail=3 2>/dev/null | tail -1 || echo "(no logs)"
done
```

---

### 7. Observability — OTEL traces in Tempo

```bash
# REST servers are wired — hit a route and check traces appear in Tempo
curl -sf http://traefik-api-gw.lab/flights/v1/airports > /dev/null

# Check REST server is emitting (look for OTLP startup log)
kubectl logs -n rest-flights deploy/rest-flights --tail=5 | grep -i "otel\|tracing\|otlp"
```

Then open **Grafana → Explore → Tempo** and search service `rest-flights`. Expect at least one recent trace with a `http.request` root span.

Note: LangGraph agents do NOT emit OTEL traces — only LangSmith (if configured).

---

### 8. LLM Analytics pipeline

```bash
# Trigger the analyzer
kubectl create job --from=cronjob/llm-analyzer trigger-$(date +%s) -n llm-analytics
sleep 30

# Check it ran
kubectl logs -n llm-analytics -l job-name --tail=20

# Check the API responds
curl -sf http://analytics-api.lab/stats | jq .
curl -sf http://analytics-api.lab/recommendations | jq '.recommendations | length'
```

Expected: analyzer job completes without error; `/stats` returns a non-null JSON object.

---

## Expected: all PASS

Claude reports each test as **✓ PASS** or **✗ FAIL** with the actual response value.
