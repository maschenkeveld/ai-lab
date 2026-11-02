# Build context is applications/agents/travel-agency (so the shared `common` package is available):
#   docker build -f flight-booker/Dockerfile -t travel-agency-flight-booker applications/agents/travel-agency
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/flight-booker
COPY flight-booker/pyproject.toml flight-booker/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY flight-booker/travel_flight_booker ./travel_flight_booker
RUN uv sync --frozen --no-dev

ENV PATH="/src/flight-booker/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4204

CMD ["python", "-m", "travel_flight_booker.server"]
