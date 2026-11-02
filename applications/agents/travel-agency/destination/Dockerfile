# Build context is applications/agents/travel-agency (so the shared `common` package is available):
#   docker build -f destination/Dockerfile -t travel-agency-destination applications/agents/travel-agency
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/destination
COPY destination/pyproject.toml destination/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY destination/travel_destination ./travel_destination
RUN uv sync --frozen --no-dev

ENV PATH="/src/destination/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4201

CMD ["python", "-m", "travel_destination.server"]
