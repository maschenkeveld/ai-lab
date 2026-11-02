# Build context is applications/agents/travel-desk (so the shared `common` package is available):
#   docker build -f fare-watcher/Dockerfile -t travel-desk-fare-watcher applications/agents/travel-desk
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/fare-watcher
COPY fare-watcher/pyproject.toml fare-watcher/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY fare-watcher/fare_watcher ./fare_watcher
RUN uv sync --frozen --no-dev

ENV PATH="/src/fare-watcher/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4304

CMD ["python", "-m", "fare_watcher.server"]
