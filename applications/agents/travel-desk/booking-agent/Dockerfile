# Build context is applications/agents/travel-desk (so the shared `common` package is available):
#   docker build -f booking-agent/Dockerfile -t travel-desk-booking-agent applications/agents/travel-desk
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /src
COPY common ./common
WORKDIR /src/booking-agent
COPY booking-agent/pyproject.toml booking-agent/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY booking-agent/booking_agent ./booking_agent
RUN uv sync --frozen --no-dev

ENV PATH="/src/booking-agent/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4303

CMD ["python", "-m", "booking_agent.server"]
