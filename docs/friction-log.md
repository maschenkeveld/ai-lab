# Friction Log — LangChain / LangGraph / LangSmith, and the Platform Underneath

What got in the way building and operating this system, and what worked well. Referenced from
`docs/presentation-deck.md`'s friction-log slide. Section 1 is scoped to LangChain/LangGraph/
LangSmith as the assignment asks; section 2 broadens to the infra/observability platform this
runs on. Every item was hit *for real* against live infrastructure, not imagined in the abstract.

---

## What got in the way

1. **`langchain-mcp-adapters` turns a tool failure into a misleading "successful" result.** An
   MCP error (`isError: true`) comes back as an ordinary `ToolMessage` with error text as its
   content, not a raised exception — nothing structurally signals failure. Every MCP tool call
   needs manual inspection to catch this. *Suggested fix: a `raise_on_mcp_error` option.*

2. **`evaluate()` has no way to reconcile a dataset against a changing definition.** As
   `dataset.py` evolved, there was no SDK-level "make the remote dataset match this list" — we
   hand-rolled `ensure_dataset()`, with no diffing. This bit us for real: a stale dataset
   silently masked every evaluator's output for a full run. *Suggested fix: a
   `client.sync_dataset()` that diffs and upserts.*

3. **`evaluate()`'s per-example model has no story for ordered or human-in-the-loop flows.** It
   scores independent rows concurrently — no way to express "run A, then B on the same thread."
   Thread-continuity and HITL-approval checks had to be sequential scripts outside `evaluate()`
   entirely, invisible in the normal LangSmith UI. *Suggested fix: an ordered/sequential
   evaluation mode.*

4. **LangGraph's checkpointer can't list "which threads are currently paused on an interrupt."**
   The biggest missing piece for a real approval inbox. We hand-rolled an in-memory
   `_pending_approvals` registry — which a pod restart silently wipes, even though the underlying
   paused runs are still resumable. *Suggested fix: `checkpointer.list_interrupted()`, querying
   the store directly instead of process memory.*

5. **`AsyncConnectionPool`'s `wait=False` default hides a dead Postgres connection.** The first
   version of `build_checkpointer()` used the default, which returns successfully regardless of
   reachability and left background workers endlessly retrying — confirmed live, this eventually
   took the pod down under load. Fix was trivial (`wait=True, timeout=10`) once found, but the
   default is backwards for a server process that should fail fast and loud.

6. **Resuming a paused run produces a disconnected LangSmith trace, not a continuation.** Verified
   via direct API query: one "trigger → pause → approve → complete" interaction produces three
   separate, unlinked traces — `Command(resume=...)` has no `parent_run_id` back to the original.
   Cross-service propagation itself works fine (specialists correctly nest into one tree within a
   single uninterrupted run); the gap is specifically at the pause/resume boundary. A workaround
   exists with the current SDK (`tracing_context(parent=<dotted_order>)`, the same trick used for
   A2A propagation) but was deliberately not implemented — it requires a new pattern inside
   `book_flight`, the single most safety-critical function in the system, this close to a
   deadline, for a trace-presentation improvement rather than a correctness fix.

---

## What worked well

- **`interrupt()` + `Command(resume=...)`** — correct on the first live test, both approve and
  reject paths. Simple mental model, does exactly what it says.
- **`create_react_agent` → `create_agent` migration** (LangChain v1) — clean, minimal changes.
- **Cross-service trace propagation, within a single run** — nesting a 5-service call chain into
  one LangSmith trace took very little code. Stops working across the pause/resume boundary
  (point 6 above), but that doesn't take away from how little effort the same-run case took.

---

## Infrastructure & Observability Friction

This runs on FluxCD GitOps on a real Kubernetes cluster, alongside shared SigNoz and LiteLLM —
several of the most instructive lessons were operational, not framework-specific.

7. **The same silent "stale endpoint after migration" bug appeared four times independently.**
   After moving from a throwaway local cluster to `shared-pve-home`, the eval suite's LLM-judge
   client and all three Python MCP adapters' OTEL exporters were still falling back to hardcoded
   old-cluster hostnames — silently, with zero crashes or visible errors, for ~17 days, found only
   by checking each one individually. One systemic pattern, not four bugs: any
   environment-overridable endpoint with a hardcoded fallback is a silent landmine after a
   migration. Worth a permanent checklist item: grep every service for
   `os.getenv(VAR, "<default>")` after any infra move.

8. **A stuck Helm pre-upgrade hook created a real chicken-and-egg deadlock.** Raising ClickHouse's
   memory limit (to fix an OOM loop) required a Helm upgrade — but the upgrade's own migration
   hook needed more query headroom than the old limit allowed, and the new limit couldn't land
   until that hook succeeded. Fixed via GitOps, not force: comment the component out, let Flux
   prune the whole namespace cleanly (PVCs included), re-enable for a genuine fresh install.

9. **Manual intervention racing against GitOps' own reconcile loop caused that exact deadlock.**
   Several rounds of manual `kubectl`/`helm` commands ran concurrently with Flux's ~1-minute
   reconciliation of the same resources — almost certainly why #8 happened at all. Lesson: once a
   resource is GitOps-owned, go fully hands-off (edit source, let the controller reconcile) or
   fully paused (suspend it first) — never both at once.

10. **A fresh observability stack isn't "just infrastructure as code" on a cold start.** After a
    clean teardown, SigNoz came up fully healthy at the k8s level yet produced zero trace data:
    its OTEL collector gets real pipeline config pushed dynamically, and refuses to register
    until an organization exists — which requires a human completing a first-run setup wizard in
    a browser. No YAML fixes that.

---

## Summary

Five framework gaps, one integration gap (HITL trace continuity), and four operational lessons —
all found by *operating* a real system, not reading docs. Three directly caused real bugs before
submission (a stale dataset masking every evaluator, an MCP error leaking into a JSON contract, a
schema bug silently zeroing bookings), all three caught by actually running the eval suite — the
strongest argument that evaluation needs to be standing practice, not a one-time checkbox. The
same pattern held one layer down, in the infrastructure itself.
