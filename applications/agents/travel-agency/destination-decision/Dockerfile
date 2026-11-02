# Build context is applications/agents/travel-agency (so the shared `common` package is available):
#   docker build -f destination-decision/Dockerfile -t travel-agency-destination-decision applications/agents/travel-agency
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/destination-decision
COPY destination-decision/pyproject.toml destination-decision/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY destination-decision/travel_destination_decision ./travel_destination_decision
RUN uv sync --frozen --no-dev

ENV PATH="/src/destination-decision/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4202

CMD ["python", "-m", "travel_destination_decision.server"]
