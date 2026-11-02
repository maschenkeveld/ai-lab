# Build context is applications/agents/travel-desk (so the shared `common` package is available):
#   docker build -f flight-scout/Dockerfile -t travel-desk-flight-scout applications/agents/travel-desk
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/flight-scout
COPY flight-scout/pyproject.toml flight-scout/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY flight-scout/flight_scout ./flight_scout
RUN uv sync --frozen --no-dev

ENV PATH="/src/flight-scout/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4302

CMD ["python", "-m", "flight_scout.server"]
