# tool_facts

**Idea:** a model is good at judgment and unreliable at copying numbers, so the numbers never pass through it. A tool
computes the facts and stores them in the run's dependencies; an output validator overwrites whatever the model returned
with those stored facts. This is the pattern behind every agent in the travel desk.

## Run

```bash
cd agentic-examples/tool_facts      # its own uv project: the first run creates .venv from uv.lock
task run                            # a real model quotes three dates and recommends one
task run -- --liar                  # a model that lies about every price: watch the validator put the facts back
task test                           # the guarantees, offline (no model, no network)
task evals                          # score a model on the task; -- --llm gemini to compare another
task run:local                      # the same, against vLLM on this Mac (cd ../../k8s && task inference:run first)
```

By default the model is `gpt-4o-mini` through the lab's LLM gateway, so the cluster must be up and `OPENAI_KEY` set (`--llm gemini` uses Gemini instead). `--liar`,
`task test` and the `guard` half of `task evals` need no LLM at all.

## Read this in `main.py`

| What | Where | Why |
|---|---|---|
| `Trip` (a dataclass) | the run's **dependencies** | one object shared by the tool and the validator |
| `quote_dates` | `@agent.tool` | computes the fares and stores them in `ctx.deps.quotes`; takes **no arguments**, so the model cannot get an input wrong |
| `facts_win` | `@agent.output_validator` | overwrites `advice.options` with the stored quotes; raises `ModelRetry` if the model skipped the tool or recommends a date that was never quoted |
| `lying_model` | a `FunctionModel` | a scripted model that lies, so the guarantee can be tested without an LLM |

Sharing through `deps` matters: Pydantic AI runs tools in their own asyncio task, so a `ContextVar` set in a tool is
invisible to the validator. Dependencies are the same object everywhere.

## What it does not guard

Only facts. In `--liar` mode the prices are restored, but `reasoning` still says "Everything is 1 EUR!", because the
validator cannot tell whether a sentence is true. Judgment fields stay the model's; check them with evals, or keep
decisions that matter (who approves, what something costs) out of the model entirely, as the desk does.

## Things to try

- Delete `facts_win` and run `--liar`: the lies come straight through. That is the whole argument for the validator.
- Change `instructions` to "recommend the *most expensive* date" and run `task evals`: `RecommendsTheCheapest` fails, which is
  how a bad prompt shows up as a number.
- Run `task evals -- --llm gemini` and compare it with the default. Weaker models tend to fail on judgment, not on prices.
- Give the tool an argument (`dates: list[str]`) and see how often a model gets it wrong; the desk's tools read the request instead.
- Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://grafana.lab:4318` and look at the run in Grafana → Tempo.

## Files

`main.py` (the idea), `evals.py` (pydantic-evals: `honest` and `guard` datasets), `tests/test_guard.py`, `common.py` (model and
tracing, copied into every example on purpose), `pyproject.toml` + `uv.lock` (this example's own environment), `Taskfile.yml`.
