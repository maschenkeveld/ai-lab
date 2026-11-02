# agentic-examples

Small, runnable examples of one idea each, for trying Pydantic AI and the agent protocols by hand and then changing them.
Every folder is **self-contained**: its own `pyproject.toml`, `uv.lock` and `.venv` (created on the first run), its own
`Taskfile.yml`, README and tests. Nothing is imported from another folder or from the rest of the repo, so you can copy one
out, break it, and the others keep working.

They need no cluster unless a README says so. The default model is OpenAI's `gpt-4o-mini` through the lab's LLM gateway (agentgateway; `OPENAI_KEY` in the
repo's `.env`); every example also runs against a model on your Mac with `task run:local` (start it with
`cd ../k8s && task inference:run`), or against any OpenAI-compatible endpoint with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. `--llm gemini|ollama|local` picks another model by gateway route.
Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://grafana.lab:4318` to see the runs in Grafana → Tempo.

## The examples, in a sensible order

| Folder | The idea | Needs |
|---|---|---|
| [`pydantic_model`](pydantic_model/) | A typed-output agent, datasets as files, evals with an LLM judge, results per model | a model (vLLM on the Mac) |
| [`tool_facts`](tool_facts/) | Tools produce the facts and an output validator stops a model changing them | a model (the guard half needs none) |
| [`mcp_tools`](mcp_tools/) | An agent that discovers and calls MCP tools (bundled server, or the lab's) | a model |
| [`human_approval`](human_approval/) | Pause before an irreversible tool until a human approves or denies | a model (tests need none) |
| [`a2a_pair`](a2a_pair/) | Two agents talking A2A: a scout served over the wire and a planner that asks it | a model |
| [`durable_approval`](durable_approval/) | A DBOS workflow that waits for a human and survives a kill and restart | a model for the plan step (tests need none) |

The last three build on each other the way the travel desk does: a gate (`human_approval`), made restart-proof
(`durable_approval`), between agents that speak a standard protocol (`a2a_pair`, `mcp_tools`), with facts owned by code
(`tool_facts`).

## Working in one

```bash
cd agentic-examples/<folder>
task --list          # what it offers: run, test, evals, run:local, ...
task test            # offline tests: no model, no network (every example has them except pydantic_model, which has evals)
task run             # try it
```

Each README ends with *Things to try*: small changes that show why the example is built the way it is. Change `common.py` freely;
it is copied into every folder on purpose.
