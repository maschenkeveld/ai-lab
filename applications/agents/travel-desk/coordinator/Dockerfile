# Build context is applications/agents/travel-desk (so the shared `common` package is available):
#   docker build -f coordinator/Dockerfile -t travel-desk-coordinator applications/agents/travel-desk
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/coordinator
COPY coordinator/pyproject.toml coordinator/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY coordinator/travel_coordinator ./travel_coordinator
RUN uv sync --frozen --no-dev

ENV PATH="/src/coordinator/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4300

CMD ["python", "-m", "travel_coordinator.server"]
