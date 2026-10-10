FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps from the lockfile first (separate layer, cached across code-only changes).
# --frozen fails the build if pyproject.toml and uv.lock have drifted - the whole point
# of a lockfile is that this can't silently happen.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY travel_orchestrator ./travel_orchestrator
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4200

# Set at build time (see .github/workflows/build-images.yml) so a deployed instance can
# report which commit it's running — surfaced in LangSmith trace metadata (see graph.py).
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["python", "-m", "travel_orchestrator.server"]
