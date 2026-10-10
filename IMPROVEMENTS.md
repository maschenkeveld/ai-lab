# AI Lab – LangChain Take-Home Code Improvement Instructions

## Goal

Improve the existing repository:

```text
https://github.com/maschenkeveld/ai-lab
```

so it is a strong technical implementation for a LangChain Deployed Architect take-home.

The existing system is already substantial. **Do not rebuild it from scratch.**

The goal is to improve the current implementation in four main areas:

1. modernize the LangChain/LangGraph implementation;
2. strengthen LangSmith tracing and evaluation;
3. improve production-readiness of the Kubernetes deployment;
4. clean up inconsistencies and make the codebase reproducible.

Focus on working code. Do not create presentation slides or extensive interview documentation yet.

---

# 1. Read the Repository Before Changing Anything

Before editing:

1. Read the latest `AGENTS.md`.
2. Inspect the latest `main` branch.
3. Understand the current travel-agent architecture.
4. Inspect the existing LangSmith tracing.
5. Inspect the existing evaluation code.
6. Run or understand the existing tests documented in `TESTS.md`.
7. Preserve existing repository conventions.

Relevant areas include:

```text
AGENTS.md
README.md
TESTS.md
DEMO.md

docs/langchain-langgraph-langsmith.md

applications/agents/travel-agency-langgraph/

applications/agents/travel-agency-langgraph/evals/

k8s/oss/langgraph/

.github/workflows/
```

Do not make assumptions based only on these instructions. The repository may have changed.

---

# 2. Preserve the Existing Architecture

The existing architecture is intentional.

Keep:

```text
Client
  |
agentgateway
  |
Travel Orchestrator
  |
  +-- Destination Specialist
  +-- Destination Decision Specialist
  +-- Flight Finder Specialist
  +-- Flight Booker Specialist
```

Also keep:

```text
Specialist
   |
   v
MCP
   |
   v
REST service
```

Keep the following components unless there is a compelling technical reason to change them:

- Python
- LangChain/LangGraph
- A2A
- MCP
- agentgateway
- LiteLLM
- PostgreSQL checkpointing
- LangSmith
- OpenTelemetry
- Kubernetes

Do **not** add unrelated technology just to make the project look more sophisticated.

In particular, do not add:

- RAG;
- a vector database;
- DeepAgents solely because the interview mentions it;
- another gateway;
- another agent framework;
- more agents without a real reason.

---

# 3. Modernize the LangChain Agent Implementation

The orchestrator currently uses an older/prebuilt LangGraph agent construction pattern.

Inspect the current implementation, likely around:

```text
applications/agents/travel-agency-langgraph/orchestrator/
```

Verify the latest supported LangChain/LangGraph API.

Prefer the current LangChain agent API if migration is straightforward, e.g. the current equivalent of:

```python
from langchain.agents import create_agent
```

instead of relying on deprecated APIs such as older `create_react_agent` usage.

## Requirements

The migration must preserve:

- current tools;
- current system prompt behavior;
- LiteLLM-backed model access;
- Postgres checkpointing;
- `thread_id`;
- streaming;
- `/plan`;
- `/plan/stream`;
- A2A JSON-RPC endpoint;
- LangSmith tracing;
- specialist calls;
- retry behavior.

Do not rewrite the workflow into a completely different architecture.

## Acceptance criteria

After the change:

```text
normal booking works
retry after unavailable flight works
A2A endpoint works
streaming works
conversation persistence works
LangSmith traces still work
existing evals still run
```

---

# 4. Make Python Dependencies Reproducible

Inspect all Python projects involved in the agent workflow.

The current dependency definitions use broad version ranges in places.

Create a reproducible dependency setup.

Prefer the tooling already used by the repository.

If `uv` is already appropriate, generate and commit a lockfile.

The final repository should make it obvious which versions were used for the take-home.

Do not randomly upgrade every dependency.

Only upgrade what is necessary to:

- use current LangChain APIs;
- fix incompatibilities;
- support evaluation improvements.

---

# 5. Clean Up the Existing LangSmith Tracing

The system already propagates LangSmith tracing across A2A calls.

Preserve this.

The desired trace for one request should make the distributed execution obvious:

```text
trip planning
  |
  +-- requirements extraction
  |
  +-- agent/model
  |
  +-- destination tool
  |     |
  |     +-- destination specialist
  |
  +-- destination selection
  |     |
  |     +-- destination decision specialist
  |
  +-- flight search
  |     |
  |     +-- flight finder specialist
  |
  +-- booking
        |
        +-- flight booker specialist
```

## Improve trace naming if needed

Use meaningful operation names.

Avoid traces dominated by names such as:

```text
invoke
request
function
run
```

Prefer domain concepts such as:

```text
extract_trip_requirements
get_destination_shortlist
select_destination
find_flight
book_flight
```

## Useful metadata

Where appropriate include:

```text
thread_id
service_name
agent_name
model_alias
application version / git SHA
environment
```

Do not include secrets or unnecessary PII.

---

# 6. Connect Evaluation Runs to the Distributed Trace

This is a high-value improvement.

The current LangSmith evaluation code calls the deployed orchestrator over HTTP.

Inspect:

```text
applications/agents/travel-agency-langgraph/evals/run_eval.py
```

If the current LangSmith SDK supports it cleanly, propagate the evaluation trace context into the remote orchestrator request.

Desired execution:

```text
LangSmith experiment run
        |
        v
evaluation target
        |
        | trace context
        v
deployed orchestrator
        |
        +-- model
        +-- tools
        +-- A2A specialist
                |
                +-- MCP
```

The project already propagates LangSmith context from the orchestrator to specialist services.

Extend the same concept outward from:

```text
evaluation runner -> orchestrator
```

if this can be done cleanly using the current LangSmith APIs.

Do not implement brittle custom tracing hacks if the SDK already provides a supported approach.

---

# 7. Expand the Evaluation Dataset

The existing dataset is a good start but currently contains mostly straightforward successful examples.

Expand it to roughly **12–20 useful scenarios**.

Do not create dozens of almost identical examples.

Include meaningful categories.

## Normal successful cases

Examples:

```text
culture + museums
nature + hiking
nightlife + food
design + architecture
```

## Missing/default values

Create requests where one or more values are omitted:

```text
origin
budget
activity
date
vibe
```

Verify defaults.

## Retry case

Create a scenario where:

```text
first selected destination -> no flight
second destination -> valid flight
```

The agent should retry correctly.

## Complete no-flight case

Create a scenario where all relevant destinations fail.

Expected behavior:

```text
no fake flight
no booking
clear explanation
bounded number of retries
```

## Ambiguous request

Use one or two requests with less structured language.

## Thread state

Test at least:

```text
same thread continues conversation
different thread does not inherit state
```

---

# 8. Add Reference Expectations to Dataset Cases

Where practical, dataset entries should contain expected invariants.

For example:

```python
{
    "inputs": {
        "trip_request": {...}
    },
    "reference_outputs": {
        "must_book": True,
        "origin": "AMS",
        "expect_retry": False
    },
    "metadata": {
        "category": "happy_path"
    }
}
```

Do not require one exact destination where several answers are valid.

Prefer invariants over exact-text matching.

---

# 9. Expand Deterministic Evaluators

Keep the existing booking-code evaluator.

Add useful deterministic checks.

Suggested evaluators:

## `booking_succeeded`

When a booking is expected:

- booking exists;
- booking code exists;
- booking state is valid.

## `no_false_booking`

When no valid route exists:

- no booking;
- no fake booking code;
- no invented flight.

## `origin_correct`

Booked/search flight starts at the expected origin.

## `date_correct`

Booked flight date matches the expected/requested date.

## `tool_sequence_valid`

Normal expected sequence:

```text
get_destination_shortlist
select_destination
find_flight
book_flight
```

## `retry_behavior_valid`

For a retry scenario:

```text
select destination
find flight -> none
select another destination
find flight -> success
book
```

## `never_book_before_flight`

This should be a hard invariant.

## `no_duplicate_destination_retry`

A destination already known to fail should not be repeatedly retried.

## `requirements_correct`

Check structured requirement extraction where reference expectations exist.

---

# 10. Keep and Improve Semantic Evaluation

The existing LLM-as-judge evaluator for destination/request fit is useful.

Keep it.

Make the rubric explicit.

It should evaluate whether the outcome reasonably matches:

```text
vibe
activity
budget
user request
```

Keep semantic evaluators focused.

Avoid generic evaluators such as:

```text
is_response_good
```

Possible semantic evaluators:

```text
trip_matches_preferences
response_is_complete
```

Do not use an LLM judge for things that can be checked deterministically.

---

# 11. Deal Properly With Random Destination Selection

The destination-decision specialist uses a random/dice-based mechanism.

This makes regression evaluation noisy.

Do not simply remove this without thinking.

Create a way to distinguish:

## Deterministic evaluation mode

Allow evaluation to use predictable selection.

Possible implementations:

```text
seeded randomness
deterministic selector config
evaluation-only selection strategy
```

Choose the least invasive option.

The normal application behavior should remain understandable.

## Stochastic integration mode

It should still be possible to run the real non-deterministic path repeatedly and measure behavior across multiple runs.

Document this distinction in code comments/readme.

---

# 12. Add One Meaningful Baseline-vs-Candidate Experiment

The project should support at least one experiment where a change can actually be compared.

Good candidates:

## Model comparison

Using LiteLLM aliases:

```text
openai-gpt
vs
gemini-pro
```

Measure:

```text
success
semantic fit
latency
tool correctness
```

OR:

## Ranking comparison

```text
keyword-only destination ranking
vs
semantic embedding ranking
```

Measure:

```text
preference fit
booking success
latency
```

OR:

## Prompt comparison

```text
current orchestration prompt
vs
improved orchestration prompt
```

Measure:

```text
tool sequence
retry behavior
booking success
```

Do not invent results.

The evaluation runner should make it reasonably easy to execute and compare variants.

---

# 13. Collect Useful Operational Metrics During Evaluation

If available from LangSmith runs, record or surface:

```text
latency
number of model calls
number of tool calls
retry count
token usage
cost
errors
```

Do not build a separate metrics database just for this.

Use the information already available through the runtime/LangSmith wherever possible.

---

# 14. Consider a Structured Final Agent Result

Inspect the current approach to reconstructing the final result from message/tool history.

If it is brittle, consider introducing a clear typed response model.

Conceptually:

```python
class TripPlanResult(BaseModel):
    requirements: TripRequirements
    shortlist: list[Destination]
    selected_destination: Destination | None
    flight: Flight | None
    booking: Booking | None
    status: Literal["booked", "no_flight", "failed"]
    answer: str
```

This can make:

```text
API contracts
evaluators
tests
debugging
```

much cleaner.

However, do this only if it can be implemented without destabilizing the working agent.

---

# 15. Improve Error Semantics Where Useful

Avoid making every failure indistinguishable.

Instead of relying only on:

```json
{"flight": null}
```

consider explicit domain outcomes internally, for example:

```text
flight_found
no_flight_available
backend_unavailable
invalid_request
booking_failed
```

This can improve:

- retry behavior;
- evaluator precision;
- observability;
- final error messages.

Keep this pragmatic. Do not build a huge domain error framework.

---

# 16. Production-Shape the Kubernetes Deployment Without Breaking the Lab

Current manifests are intentionally local/Kind oriented.

Keep the simple local deployment.

Add a **production-style configuration or overlay** rather than replacing the local setup.

Production configuration should demonstrate:

## Immutable image references

Do not use:

```text
:latest
```

for the production path.

Use:

```text
git SHA
```

or image digest.

## Readiness and liveness probes

Use existing health endpoints where appropriate.

## Resource requests and limits

Add reasonable starting values.

Do not pretend they are benchmark-derived unless measured.

## Multiple replicas

At minimum support/document production replicas for:

```text
orchestrator
specialists
```

where appropriate.

## PodDisruptionBudget

Add for replicated production workloads where useful.

## Horizontal scaling

Add an HPA or production scaling example where appropriate.

Remember these services may be more I/O-bound than CPU-bound.

Do not overclaim the usefulness of CPU-only scaling.

---

# 17. Keep Agent State External

The Postgres checkpointer is important.

Do not move state into pod-local memory.

Ensure the production-style deployment works conceptually with multiple orchestrator replicas.

Document in code/readme comments that:

```text
compute is horizontally scalable
conversation state is externalized
```

Also document the unresolved/important issue of concurrent requests against the same `thread_id`.

Do not pretend this issue does not exist.

---

# 18. Improve Secret Handling in the Production Path

Do not hard-code real secrets.

Production manifests should obtain secrets from Kubernetes Secrets or the actual external secret solution used by the self-hosted environment.

Relevant secrets include:

```text
LANGCHAIN/LANGSMITH API key
OpenAI key
Gemini key
LiteLLM key
database credentials
```

Do not add a new Vault/secret product solely for this take-home.

---

# 19. Keep Local and Production Configurations Clearly Separate

A reviewer should be able to tell:

```text
this is the developer/lab deployment
```

versus:

```text
this is the production-style deployment
```

Do not make Kind unnecessarily difficult just to make production YAML look nicer.

---

# 20. Add Build/Version Metadata

If practical, expose or trace the deployed version.

Useful values:

```text
git SHA
service version
model alias
environment
```

This lets an evaluation experiment be tied to a particular application revision.

Do not expose secrets.

---

# 21. CI Improvements

Inspect the existing GitHub Actions workflow.

Keep the existing image build process.

Add only useful validation.

Potential PR checks:

```text
Python tests
linting
small deterministic eval subset
manifest validation
```

Potential main/release checks:

```text
broader evaluation
container build
immutable SHA tag
```

Do not make every commit trigger a huge expensive LLM benchmark unless it is clearly worthwhile.

---

# 22. Clean Repository Hygiene

Before considering the work complete:

- remove tracked `__pycache__`;
- remove tracked `.pyc` files;
- update `.gitignore`;
- ensure no secrets are committed;
- remove dead code;
- remove clearly stale comments;
- verify example commands;
- make package names consistent;
- ensure service names in docs/config match reality.

---

# 23. Fix Documentation That Directly Describes the Code

This coding pass should only fix documentation necessary to accurately understand/run the implementation.

Do not write the final interview deck yet.

Update:

## `README.md`

Make sure the actual agent/tracing architecture is accurately described.

## Travel agent README

It should no longer describe the implementation as only a skeleton if it is now fully implemented.

## `docs/langchain-langgraph-langsmith.md`

Ensure it matches the actual current code after modernization.

## `AGENTS.md`

Only update if implementation changes require it.

---

# 24. Specific Documentation Consistency Issues to Check

Review for contradictions around:

## LangSmith

Some older text may say only the orchestrator is traced while newer code traces specialists too.

Make this consistent.

## LangGraph

Do not describe all five services as building LangGraphs if only the orchestrator actually does.

Prefer:

```text
LangChain/LangGraph orchestrator
+
A2A specialist services
```

if that is the accurate implementation.

## Tool names

Ensure README/demo names match actual MCP tool names.

---

# 25. Testing Required Before Completion

Run the system through the following scenarios.

## Happy path

```text
request
 -> shortlist
 -> selection
 -> flight
 -> booking
```

## Retry

```text
request
 -> destination A
 -> no flight
 -> destination B
 -> flight
 -> booking
```

## No flight

```text
all destinations exhausted
 -> no booking
 -> clean explanation
```

## Thread continuation

Same `thread_id`.

## Thread isolation

Different `thread_id`.

## Streaming

Verify `/plan/stream`.

## A2A

Verify the JSON-RPC path.

## LangSmith

Verify one trace contains the expected distributed execution.

## Evaluations

Run the dataset and confirm evaluators appear correctly in LangSmith.

---

# 26. Do Not Hide Weaknesses by Overengineering Them

Some current limitations are useful architecture discussion points.

Examples:

```text
random destination selection
prompt-controlled tool ordering
lab-scale Kubernetes
open development routes
Postgres fallback to stateless mode
distributed architecture overhead
```

Only fix these where doing so materially improves correctness.

Otherwise document them clearly.

The interview benefits from having real trade-offs to discuss.

---

# 27. Priority Order

Work in this order.

## Priority 1

```text
verify current system
modernize agent API
lock dependencies
keep functionality working
```

## Priority 2

```text
improve LangSmith tracing
expand evaluation dataset
expand evaluators
handle deterministic/stochastic testing
```

## Priority 3

```text
run baseline/candidate experiment
collect real results
```

## Priority 4

```text
production Kubernetes overlay/config
immutable images
probes
replicas
resources
HPA/PDB where appropriate
```

## Priority 5

```text
CI improvements
repo cleanup
code documentation consistency
```

---

# 28. Things That Are Explicitly Out of Scope for This Pass

Do not yet spend significant time on:

```text
presentation deck
video recording
long architecture report
business-impact slides
product-feedback slides
full self-hosted LangSmith deployment
new UI
new agent framework
RAG
new vector database
complete identity implementation
complex load-testing infrastructure
```

Those can be addressed after the core code/evaluation improvements are complete.

---

# 29. Definition of Done

This improvement pass is complete when:

## Application

- current supported LangChain approach is used;
- happy-path booking works;
- retry works;
- no-flight behavior is correct;
- A2A works;
- MCP works;
- streaming works;
- Postgres thread persistence works.

## LangSmith

- distributed traces remain intact;
- trace names are meaningful;
- evaluation dataset contains meaningful success and failure cases;
- deterministic evaluators exist;
- semantic evaluator exists;
- deterministic/stochastic evaluation is handled deliberately;
- at least one real experiment comparison can be run.

## Kubernetes

- local deployment remains simple;
- production-style deployment/config exists;
- production images are immutable;
- health probes exist;
- resource configuration exists;
- replica/scaling strategy is represented;
- secrets are not hard-coded.

## Repository

- dependencies are reproducible;
- no tracked Python cache files;
- docs accurately describe the code;
- existing tests pass;
- new evaluation behavior is documented.

---

# 30. Final Principle

The current lab is already strong.

The goal is not to make it bigger.

The goal is to make it:

```text
current
reproducible
measurable
observable
testable
production-shaped
```

with **LangSmith evaluation as the main area of improvement**.

Do not replace working architecture simply because another implementation might look more fashionable.

Every substantial code change should have a clear answer to:

> What concrete problem does this solve, and how can we demonstrate that it improved the system?