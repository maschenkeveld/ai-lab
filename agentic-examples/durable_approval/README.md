# durable_approval

**Idea:** `human_approval` loses its pending approval if the process dies. Here the trip is a *workflow* (DBOS): plan with a
model, hold a flight, wait for a person, then confirm or release the hold. Every effect is a *step* whose result DBOS stores in a
database; waiting for the person is a durable `recv`. Kill the process while it waits, start it again, and the same workflow
carries on: the steps that already ran are **not** run again (the model is not called twice, the flight is not held twice).

It uses SQLite so there is nothing to start. The travel desk runs the same engine on Postgres.

## Run it, and break it

```bash
cd agentic-examples/durable_approval   # its own uv project: the first run creates .venv from uv.lock
task run                               # plans, holds a flight, then waits. Prints the workflow id.
#   ... press Ctrl-C (or `kill -9` it) while it waits ...
task resume                            # restart: no model call, no second hold, it just keeps waiting
task approve -- <workflow id>          # from any terminal; or: task approve -- <id> --deny "too expensive"
task status -- <workflow id>           # the steps DBOS recorded, and the booking system's record
task test                              # offline: approve, deny, expire, and "every step is recorded"
task clean                             # delete the local database and booking record
```

After a kill and `resume`, `task status` shows one `hold` and one `confirm` for the whole story. That is the point.

## Read this in `main.py`

| What | Where | Why |
|---|---|---|
| `@DBOS.step()` | `plan_trip`, `hold_flight`, `confirm_flight`, `release_flight` | each effect runs once per workflow; its result is stored, so a restart reuses it |
| `@DBOS.workflow()` | `trip_workflow` | the control flow. It must be deterministic: anything that touches the world is a step |
| `DBOS.recv("approval", timeout_seconds=...)` | the wait | durable: it survives a restart and costs nothing while waiting. Returns `None` on timeout, which becomes "expired, release the hold" |
| `DBOSClient(...).send(...)` | `cmd_approve` | how *another process* delivers the decision to a waiting workflow |
| `DBOS.set_event("status", ...)` | the workflow | publishes progress that `status` reads without disturbing the run |
| `DBOS.launch()` | `launch` | also what resumes interrupted workflows, which is why `resume` is just "launch and wait" |

## Things to know

- Denial and expiry go through the same path as approval, and both **release the hold**: whatever you reserve, give it back.
- `planning_model()` is a plain function on purpose. DBOS runs steps in their own threads, and Pydantic AI's `agent.override(...)`
  does not cross threads, so the tests patch this function instead.
- The model call is a *step*, so its answer is stored. On resume the workflow uses the model's earlier answer rather than asking again.
- SQLite is for development; DBOS recommends Postgres for production (that is what the travel desk uses).

## Things to try

- Set `APPROVAL_TTL_SECONDS=20`, run, and do nothing: the trip expires and the hold is released.
- Kill it *between* the plan and the hold (add a `time.sleep(10)` in `hold_flight`) and see where it picks up.
- Start two trips (`task run -- --id a` and `--id b` in two terminals) and approve them in the other order.
- Add a second approver: wait for two messages, and refuse the same name twice. The desk's `Store.decide` does this.
- Swap SQLite for Postgres by changing `DB_URL` (any `postgresql://...`).

## Files

`main.py`, `tests/test_durable.py`, `common.py` (model and tracing, copied into every example on purpose), `pyproject.toml` +
`uv.lock`, `Taskfile.yml`, `.gitignore` (the local database and booking record).
