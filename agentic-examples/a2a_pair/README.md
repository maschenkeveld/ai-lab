# a2a_pair

**Idea:** A2A (Agent2Agent) is the open protocol for one agent to call another. The *scout* is a Pydantic AI agent served over
A2A as its own process; the *planner* is another agent whose only tool is an A2A call to the scout. The planner never imports
the scout. All it knows is a URL, and everything else (what the scout can do, how to talk to it) comes over the wire.

## Run

```bash
cd agentic-examples/a2a_pair        # its own uv project: the first run creates .venv from uv.lock
task demo                           # starts the scout, runs the planner once, stops the scout
# or by hand, in two terminals:
task serve                          # terminal 1: the scout on http://127.0.0.1:4500
task run -- "Which is nearer, Vienna or Lisbon?"      # terminal 2: the planner
curl http://127.0.0.1:4500/.well-known/agent-card.json | jq .     # the scout's agent card
task test                           # offline: the real scout in-process, a stub model
```

Needs the lab's LiteLLM for the models (or `LLM_*` variables for another endpoint). Both agents use the same model here;
nothing requires that, and you could point the scout at a different one.

## What A2A looks like

The printout at the end shows the lifecycle of each question the planner asked:

```
question 1: submitted -> working -> completed
```

`message/send` does not return the answer. It returns a *task* in state `submitted`. The caller polls `tasks/get` until the
task is final (`completed`, `failed`, `input-required`, ...). That is why a slow agent, or one that waits for a human, fits the
same protocol as a fast one. The travel desk's coordinator, booking agent and fare-watcher all speak exactly this.

## Read this

| What | Where |
|---|---|
| Turning an agent into an A2A server | `build_app` in `scout.py`: `AgentWorker` + `FastA2A` (Pydantic AI 2.x has no `agent.to_a2a()`) |
| The agent card | `FastA2A(name, description, skills, ...)`, served at `/.well-known/agent-card.json` |
| An A2A call as a tool | `ask_scout` and `ask` in `main.py`: send, poll, read the text artifact |
| Facts owned by a tool | `facts_about` in `scout.py` (same idea as `tool_facts`) |

## Limits, on purpose

Tasks live in memory (`InMemoryStorage`), so a restart loses them. The stock FastA2A bridge exchanges text; the desk adds
typed JSON payloads on top (see `applications/agents/travel-desk/common/travel_common/a2a_server.py` at the repo root).
There is no authentication.

## Things to try

- `curl` the scout's JSON-RPC by hand: `message/send`, then `tasks/get` (see `DEMO.md` or `TESTS.md` for the request shape).
- Add a second scout on another port (`SCOUT_PORT=4501`) for different cities and give the planner a tool for each.
- Ask about a city the scout has no facts for and watch it say so instead of inventing something.
- Make the scout slow (`await asyncio.sleep(5)` in a tool) and watch the planner's polling see `working` for longer.
- Route the call through agentgateway as the lab does: point `SCOUT_URL` at a route like `http://agentgateway.lab/a2a/...`.

## Files

`scout.py` (the server), `main.py` (the planner and the A2A client), `tests/test_a2a.py`, `common.py` (model and tracing, copied
into every example on purpose), `pyproject.toml` + `uv.lock`, `Taskfile.yml`.
