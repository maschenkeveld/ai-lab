# pydantic_model

Local copy of the Pydantic AI example [`pydantic_model`](https://pydantic.dev/docs/ai/examples/getting-started/pydantic-model/):
turn a text description into a typed Pydantic model (`MyModel`: city and country).
It is also the **reference layout** for every example in `agentic-examples/`, and the place to learn
how evals run with Pydantic Evals.

## Files

| File | Role |
|---|---|
| `main.py` | The agent (`Agent(output_type=MyModel)`, **no model**), `server_model()` (vLLM's OpenAI-compatible API), `configure_telemetry()`, and the run. Importing it has no side effects and needs no environment variables. |
| `evals.py` | The evals runner: loads datasets, loads models, runs `pydantic_evals`, prints and saves the results. Reuses `agent` from `main.py` and supplies its own model. |
| `datasets/*.yaml` | Eval datasets as files: `basic.yaml` (8 exact cases + 3 judged cases), `tricky.yaml` (7 exact cases that trip models up). `dataset_schema.json` gives editors completion and validation. |
| `local_llm.py` | Loads a model in-process with vLLM's `LLM` class and wraps `LLM.chat()` as a Pydantic AI model. Generic: copy unchanged into other examples. |
| `Taskfile.yml` | This example's tasks: `run`, `evals`, `evals:schema`. |
| `pyproject.toml`, `uv.lock` | This example's own dependencies and venv. |
| `results/` | One JSON file per eval run, with every case, output, check and judge reason. |

## Run

After setting the `VLLM_*` variables in the repo's `.env` (see `.env.example`) and installing vLLM-Metal once with `cd k8s && task inference:install`. Run the commands below from this folder (`task run`, `task evals`, `task evals:schema`):

```bash
(cd ../../k8s && task inference:run)   # terminal 1, blocking: serves VLLM_MODEL from .env
task run                               # terminal 2: main.py against that server

# Stop `task inference:run` first. Models are loaded in the eval process itself, no server.
task evals                                          # all models, basic dataset
task evals -- --datasets basic tricky --repeat 3 --temperature 0.5
task evals -- --models mistral-7b --judge qwen2.5-14b --verbose
task evals:schema                                   # regenerate dataset_schema.json
```

`--verbose` also prints Pydantic Evals' own per-case tables, including the judge's reasons.

## How the evals work

1. **Dataset files** are loaded with `Dataset.from_file(..., custom_evaluator_types=[CityAndCountry])`. A file lists cases (`inputs`, `expected_output`) and the evaluators that score them. Custom evaluators must be dataclasses to be loadable by name.
2. **Exact cases** are scored by the custom `CityAndCountry` evaluator (city and country, with "USA" = "United States"). It returns no result for a case without an expected output.
3. **Open-ended cases** have several acceptable answers, so they carry a per-case `LLMJudge` evaluator with a rubric. The judge is a local model, registered with `set_default_judge_model`. It is loaded first with a small memory fraction and stays loaded; candidates load after it with a larger cap (the fraction is a total across everything loaded).
4. **The loop over models** is our own code: load a candidate, run every dataset with `dataset.evaluate_sync(task, repeat=...)`, free the model.
5. **Output:** a summary table (share of runs passing each check, errors, seconds per run), a cases-by-models table, grouped failures with the judge's reasons, and the JSON file in `results/`.


## Changes from upstream

- The model is local vLLM instead of a hosted model; `Caching()` is left out.
- The agent has no model; callers pass one with `model=`.
- Traces go to the lab's LGTM: set `OTEL_EXPORTER_OTLP_ENDPOINT=http://grafana.lab:4318` and Pydantic AI's spans (agent run, model call, tool call) appear in Tempo under service `pydantic_model`. Prompts and responses are included only with `AGENT_TRACE_CONTENT=1`. Unset = nothing is sent.

## Results (2026-10-26, M5 Max)

`--datasets basic tricky --repeat 3 --temperature 0.5`, judge Qwen2.5-14B-Instruct 4-bit at temperature 0:

| Dataset | Model | Runs | All checks passed | Mean per run |
|---|---|---|---|---|
| basic | Mistral-7B-Instruct-v0.3 4-bit | 33 | 100% | 0.33 s |
| basic | Qwen2.5-14B-Instruct 4-bit | 33 | 100% | 0.45 s |
| tricky | Mistral-7B-Instruct-v0.3 4-bit | 21 | 86% | 0.36 s |
| tricky | Qwen2.5-14B-Instruct 4-bit | 21 | 86% | 0.43 s |

On `tricky`, both models fail only the `injection` case, every run: they answer "London, United Kingdom" because the input tells them to ignore their instructions. The other 6 cases pass for both.

Judge check: given a rubric for "a very large city on the US east coast", the judge passed Boston, failed Tokyo and Los Angeles with sensible reasons, and also failed Miami (a debatable call, since the rubric says "very large"). Read the judge's reasons (`--verbose` or the JSON) rather than trusting the verdict alone.

## Known limits

- **Model choice matters for tool calling through the server.** `main.py` uses tool-calling output. With Mistral-7B-Instruct-v0.3 and the `mistral` parser, vLLM returns the tool call as plain text in `content` and the model does not stop (finish reason `length`), so a run takes minutes. Qwen2.5-14B-Instruct with `VLLM_TOOL_PARSER=hermes` and `llm/qwen25-chat-template.jinja` returns a proper `tool_calls` entry and finishes in about 2 seconds. Set both in `.env` when you serve Qwen (see `.env.example`).

- The evals use prompted output (the model profile in `local_llm.py` asks for it), while `main.py` uses the default tool-calling output through the server. The evals therefore do not test `main.py`'s output mode. The in-process adapter does not parse tool calls or stream yet; add that when an example needs it.
- When the judge is also a candidate (the default, Qwen2.5-14B judging Qwen2.5-14B), it grades its own answers, which is biased. The run prints a warning. Use `--judge` with a different model when it matters.
- Both models pass all `basic` cases, so that dataset does not separate them; `tricky` separates neither (both fail the injection case).
- Three judged cases and one judge model are a thin basis: the judge has not been validated against human labels.
- `local_llm.py` takes vLLM from the Homebrew vllm-metal environment by appending its site-packages to `sys.path`; Python 3.12 must match that install.
