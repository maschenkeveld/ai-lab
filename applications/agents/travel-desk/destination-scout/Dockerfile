# Build context is applications/agents/travel-desk (so the shared `common` package is available):
#   docker build -f destination-scout/Dockerfile -t travel-desk-destination-scout applications/agents/travel-desk
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/destination-scout
COPY destination-scout/pyproject.toml destination-scout/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY destination-scout/destination_scout ./destination_scout
RUN uv sync --frozen --no-dev

ENV PATH="/src/destination-scout/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4301

CMD ["python", "-m", "destination_scout.server"]
