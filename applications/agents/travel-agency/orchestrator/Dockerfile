# Build context is applications/agents/travel-agency (so the shared `common` package is available):
#   docker build -f orchestrator/Dockerfile -t travel-agency-orchestrator applications/agents/travel-agency
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/orchestrator
COPY orchestrator/pyproject.toml orchestrator/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY orchestrator/travel_orchestrator ./travel_orchestrator
RUN uv sync --frozen --no-dev

ENV PATH="/src/orchestrator/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4200

CMD ["python", "-m", "travel_orchestrator.server"]
