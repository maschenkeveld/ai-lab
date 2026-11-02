# Build context is applications/agents/travel-agency (so the shared `common` package is available):
#   docker build -f flight-finder/Dockerfile -t travel-agency-flight-finder applications/agents/travel-agency
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/flight-finder
COPY flight-finder/pyproject.toml flight-finder/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY flight-finder/travel_flight_finder ./travel_flight_finder
RUN uv sync --frozen --no-dev

ENV PATH="/src/flight-finder/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4203

CMD ["python", "-m", "travel_flight_finder.server"]
