FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps from the lockfile first (separate layer, cached across code-only changes).
# --frozen fails the build if pyproject.toml and uv.lock have drifted - the whole point
# of a lockfile is that this can't silently happen.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY mcp_destinations_adapter ./mcp_destinations_adapter
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=8000
ENV MCP_PATH=/mcp

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["python", "-m", "mcp_destinations_adapter.server"]
