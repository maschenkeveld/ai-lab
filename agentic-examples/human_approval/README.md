# human_approval

**Idea:** the one step that cannot be undone should not run until a person says so. Declare the tool
`requires_approval=True`: when the model calls it, the run stops with `DeferredToolRequests` instead of executing it. Your
code decides, then resumes the *same* run with the decisions. Approval runs the tool; denial (`ToolDenied`) tells the model it
was refused and why, and the tool never runs.

## Run

```bash
cd agentic-examples/human_approval  # its own uv project: the first run creates .venv from uv.lock
task run                            # you are asked before anything is booked: y, n, or type a reason
task run -- --approve               # decide automatically: approve
task run -- --deny "too expensive"  # decide automatically: deny, and watch what the model does next
task test                           # offline (a scripted model): a denied booking never runs
```

Needs the lab's LLM gateway for the model (or `task run:local` for vLLM on your Mac). The tests need no model.

## Read this in `main.py`

| What | Where | Why |
|---|---|---|
| `@agent.tool(requires_approval=True)` | `book_flight` | the gate. The tool body does not run until approved |
| `output_type=[str, DeferredToolRequests]` | `agent` | lets a run end *either* with an answer *or* with "waiting for approval" |
| `run_with_approvals` | the loop | ask `decide` for each pending call, build a `DeferredToolResults`, resume with `message_history` |
| `ToolDenied(reason)` | the denial | the model reads the reason and adapts (try `--deny "too expensive"`: it offers the next option) |
| `Ledger` | the dependencies | records what really happened; the tests assert a denial leaves it empty |

## Limits, on purpose

The paused run lives in memory: kill the process while it waits and the approval is gone. `durable_approval` is the same idea
made restart-proof. In the travel desk the approval is a Postgres row with an owner, an expiry and an audit trail.

## Things to try

- Deny with different reasons and compare how the model answers; try an empty reason.
- Make `search_flights` need approval too and see how many questions you are asked.
- Replace `ask_the_human` with a function that auto-approves bookings under 50 EUR and asks above it: that is a policy gate (the
  desk's `policy.py` does exactly this, in code the model cannot influence).
- Ask for two bookings in one request and see `result.output.approvals` hold both at once.
- Run `task run:local` to see how a local model handles the deny path.

## Files

`main.py`, `tests/test_approval.py`, `common.py` (model and tracing, copied into every example on purpose), `pyproject.toml` +
`uv.lock`, `Taskfile.yml`.
