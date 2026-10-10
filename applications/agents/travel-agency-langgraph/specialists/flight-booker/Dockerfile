FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY travel_flight_booker ./travel_flight_booker
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4204

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["python", "-m", "travel_flight_booker.server"]
