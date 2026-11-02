"""Evals for the pydantic_model agent: datasets from files, several local models, an LLM judge.

What runs, in order:
1. The judge model is loaded first (small memory fraction) and registered as pydantic_evals'
   default judge, so `LLMJudge` evaluators in the dataset files use it.
2. For each candidate model: load it in-process (local_llm.py, vLLM's `LLM` class on vLLM-Metal),
   score every dataset in datasets/ with pydantic_evals, free it, load the next.
3. Print a summary, the per-case results and the failures, and write everything to results/.

No server and no API key. Stop any running `task inference:run` first: loaded models reserve a
large part of unified memory.

The agent from main.py is reused unchanged (it has no model; each caller supplies one). The model
profile in local_llm.py makes it use prompted output, so these evals do not cover the tool-calling
output mode that main.py uses against the server.

Datasets are YAML files in datasets/ (`dataset_schema.json` gives editors completion). Cases with
one right answer are scored by `CityAndCountry`; cases with several acceptable answers carry an
`LLMJudge` rubric. A judge that is also a candidate grades its own answers, which is biased: use a
different `--judge` when it matters.

Run with:

    task evals                                      # all models, basic dataset
    task evals -- --datasets basic tricky --repeat 3 --temperature 0.5
    task evals -- --models mistral-7b --judge qwen2.5-14b --verbose
    task evals:schema                               # regenerate the JSON schema

(run from this folder)

Traces go to the OTLP collector (LGTM) when OTEL_EXPORTER_OTLP_ENDPOINT is set, as in main.py.
"""

import argparse
import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from local_llm import ServedModel, loaded, local_model
from main import MyModel, agent, configure_telemetry, shutdown_telemetry
from pydantic_evals import Dataset, set_eval_attribute
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.evaluators.llm_as_a_judge import set_default_judge_model
from pydantic_evals.reporting import EvaluationReport
from rich.console import Console
from rich.table import Table

HERE = Path(__file__).resolve().parent
LLM_DIR = HERE.parents[1] / "llm"  # chat templates, shared with `task inference:run`
DATASETS = HERE / "datasets"
JUDGE_MEMORY_FRACTION = 0.3  # the judge stays loaded, so it gets a small cap...
CANDIDATE_MEMORY_FRACTION = 0.6  # ...and candidates a larger cap that also counts the judge

MODELS = {
    "qwen2.5-14b": ServedModel("mlx-community/Qwen2.5-14B-Instruct-4bit"),
    # Mistral's stock template rejects a system message; this one folds it into the first user turn.
    "mistral-7b": ServedModel(
        "mlx-community/Mistral-7B-Instruct-v0.3-4bit", LLM_DIR / "mistral-chat-template.jinja"
    ),
}

COUNTRY_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "the netherlands": "netherlands",
}


def normalize_country(value: str) -> str:
    value = value.strip().lower()
    return COUNTRY_ALIASES.get(value, value)


@dataclass
class CityAndCountry(Evaluator[str, MyModel, None]):
    """Two deterministic assertions: the city and the country match the expected ones.

    Cases without an expected output are open-ended and get no result here (an LLMJudge scores them).
    """

    def evaluate(self, ctx: EvaluatorContext[str, MyModel, None]) -> dict[str, bool]:
        expected = ctx.expected_output
        if expected is None:
            return {}
        return {
            "city": ctx.output.city.strip().lower() == expected.city.lower(),
            "country": normalize_country(ctx.output.country) == normalize_country(expected.country),
        }


DatasetType = Dataset[str, MyModel, type(None)]  # no case metadata


def load_dataset(name: str) -> DatasetType:
    return DatasetType.from_file(DATASETS / f"{name}.yaml", custom_evaluator_types=[CityAndCountry])


def write_schema() -> None:
    schema = DatasetType.model_json_schema_with_evaluators([CityAndCountry])
    (DATASETS / "dataset_schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    print("Written datasets/dataset_schema.json")


# ---- running ---------------------------------------------------------------------------------


def run_dataset(dataset: DatasetType, key: str, model, repeat: int) -> EvaluationReport:
    async def task(text: str) -> MyModel:
        # One conversation id per run, recorded with the case so a result can be matched to its trace.
        conversation_id = f"{HERE.name}-evals:{uuid.uuid4()}"
        set_eval_attribute("conversation_id", conversation_id)
        result = await agent.run(text, model=model, metadata={"conversation_id": conversation_id})
        return result.output

    return dataset.evaluate_sync(
        task, name=f"{dataset.name}/{key}", max_concurrency=1, repeat=repeat, progress=False
    )


# ---- summarising -----------------------------------------------------------------------------


def case_records(report: EvaluationReport) -> dict[str, dict[str, Any]]:
    """Group a report's runs by case (repeats of a case share `source_case_name`)."""
    cases: dict[str, dict[str, Any]] = {}
    for c in report.cases:
        entry = cases.setdefault(
            c.source_case_name or c.name,
            {
                "input": c.inputs,
                "expected": c.expected_output.model_dump() if c.expected_output else None,
                "runs": [],
            },
        )
        entry["runs"].append(
            {
                "output": c.output.model_dump(),
                "checks": {
                    n: {"passed": a.value, "reason": a.reason} for n, a in c.assertions.items()
                },
                "conversation_id": c.attributes.get("conversation_id"),
                "seconds": round(c.task_duration, 3),
                "input_tokens": c.metrics.get("input_tokens"),
                "output_tokens": c.metrics.get("output_tokens"),
            }
        )
    for f in report.failures:
        entry = cases.setdefault(
            f.source_case_name or f.name, {"input": f.inputs, "expected": None, "runs": []}
        )
        entry["runs"].append({"error": f.error_message})
    return cases


def run_passed(run: dict[str, Any]) -> bool:
    return "error" not in run and all(c["passed"] for c in run["checks"].values())


def pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.0f}%" if denominator else "-"


def summary_table(results: list[dict[str, Any]]) -> Table:
    check_names = sorted({n for r in results for c in r["cases"].values() for run in c["runs"]
                          for n in run.get("checks", {})})  # fmt: skip
    table = Table(title="Summary (share of runs that passed each check)")
    for column in [
        "dataset",
        "model",
        "runs",
        "all checks",
        *check_names,
        "errors",
        "s/run",
        "load s",
    ]:
        table.add_column(column, justify="left" if column in ("dataset", "model") else "right")
    for r in results:
        runs = [run for c in r["cases"].values() for run in c["runs"]]
        ok = [run for run in runs if "error" not in run]
        per_check = []
        for name in check_names:
            seen = [run["checks"][name]["passed"] for run in ok if name in run["checks"]]
            per_check.append(pct(sum(seen), len(seen)))
        mean_s = sum(run["seconds"] for run in ok) / len(ok) if ok else 0
        table.add_row(
            r["dataset"], r["model"], str(len(runs)), pct(sum(map(run_passed, runs)), len(runs)),
            *per_check, str(len(runs) - len(ok)), f"{mean_s:.2f}",
            f"{r['load_s']:.1f}" if r["load_s"] else "(judge)",
        )  # fmt: skip
    return table


def case_table(dataset: str, results: list[dict[str, Any]]) -> Table:
    """One row per case, one column per model: runs that passed every check / runs."""
    mine = [r for r in results if r["dataset"] == dataset]
    table = Table(title=f"Cases: {dataset} (runs passed / runs)")
    table.add_column("case")
    for r in mine:
        table.add_column(r["model"], justify="right")
    for case in mine[0]["cases"]:
        cells = []
        for r in mine:
            runs = r["cases"][case]["runs"]
            passed = sum(map(run_passed, runs))
            style = "green" if passed == len(runs) else "red" if passed == 0 else "yellow"
            cells.append(f"[{style}]{passed}/{len(runs)}[/{style}]")
        table.add_row(case, *cells)
    return table


def print_failures(console: Console, results: list[dict[str, Any]]) -> None:
    shown = False
    for r in results:
        for name, case in r["cases"].items():
            groups: dict[str, list[dict[str, Any]]] = {}
            for run in case["runs"]:
                if not run_passed(run):
                    key = json.dumps(
                        [run.get("error"), run.get("output"), [c for c, v in run.get("checks", {}).items() if not v["passed"]]]
                    )  # fmt: skip
                    groups.setdefault(key, []).append(run)
            for runs in groups.values():
                run = runs[0]
                if not shown:
                    console.print("\n[bold]Failures[/bold]")
                    shown = True
                console.print(
                    f"\n[red]✘[/red] {r['dataset']} / {r['model']} / {name}  ({len(runs)}x)"
                )
                console.print(f"  input:    {case['input']}")
                if case["expected"]:
                    console.print(f"  expected: {case['expected']}")
                if "error" in run:
                    console.print(f"  error:    {run['error']}")
                    continue
                console.print(f"  got:      {run['output']}")
                for check, result in run["checks"].items():
                    if not result["passed"]:
                        reason = f": {result['reason']}" if result["reason"] else ""
                        console.print(f"  failed:   {check}{reason}")
    if not shown:
        console.print("\nNo failures.")


# ---- main ------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Evals for the pydantic_model agent")
    parser.add_argument("--models", nargs="+", choices=sorted(MODELS), default=sorted(MODELS))
    parser.add_argument("--judge", choices=sorted(MODELS), default="qwen2.5-14b")
    available = sorted(p.stem for p in DATASETS.glob("*.yaml"))
    parser.add_argument("--datasets", nargs="+", choices=available, default=["basic"])
    parser.add_argument("--repeat", type=int, default=1, help="runs per case")
    parser.add_argument("--temperature", type=float, default=0.0, help="of the candidate models")
    parser.add_argument("--verbose", action="store_true", help="also print pydantic_evals' tables")
    parser.add_argument("--write-schema", action="store_true")
    args = parser.parse_args()
    if args.write_schema:
        return write_schema()

    console = Console()
    configure_telemetry(f"{HERE.name}_evals")
    (HERE / "results").mkdir(exist_ok=True)
    datasets = {name: load_dataset(name) for name in args.datasets}
    results: list[dict[str, Any]] = []

    judge_served = MODELS[args.judge]
    with loaded(judge_served, JUDGE_MEMORY_FRACTION) as (judge_llm, judge_load_s):
        # The judge answers with temperature 0 whatever the candidates use.
        set_default_judge_model(local_model(judge_llm, judge_served, temperature=0.0))
        console.print(f"Judge: {args.judge} (loaded in {judge_load_s:.1f}s)")
        for key in args.models:
            served = MODELS[key]
            if key == args.judge:
                console.print(f"[yellow]{key} judges its own answers (biased)[/yellow]")
                load = nullcontext((judge_llm, 0.0))
            else:
                load = loaded(served, CANDIDATE_MEMORY_FRACTION)
            with load as (llm, load_s):
                model = local_model(llm, served, args.temperature)
                for name, dataset in datasets.items():
                    console.print(f"\n=== {name} / {key}")
                    report = run_dataset(dataset, key, model, args.repeat)
                    if args.verbose:
                        report.print(
                            include_input=True,
                            include_output=True,
                            include_expected_output=True,
                            include_reasons=True,
                        )
                    results.append(
                        {
                            "dataset": name,
                            "model": key,
                            "repo_id": served.repo_id,
                            "load_s": load_s,
                            "cases": case_records(report),
                        }
                    )

    console.print()
    console.print(summary_table(results))
    for name in datasets:
        console.print()
        console.print(case_table(name, results))
    print_failures(console, results)

    out = HERE / "results" / f"{datetime.now():%Y%m%dT%H%M%S}.json"
    record = {
        "api": "vllm.LLM (in-process)",
        "judge": {"model": args.judge, "repo_id": judge_served.repo_id, "temperature": 0.0},
        "candidate_temperature": args.temperature,
        "repeat": args.repeat,
        "results": results,
    }
    out.write_text(json.dumps(record, indent=2, default=str))
    console.print(f"\nWritten to {out.relative_to(HERE.parents[1])}")
    shutdown_telemetry()


if __name__ == "__main__":
    main()
