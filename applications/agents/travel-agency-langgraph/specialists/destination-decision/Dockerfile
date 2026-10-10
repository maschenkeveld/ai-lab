FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY travel_destination_decision ./travel_destination_decision
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=4202

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["python", "-m", "travel_destination_decision.server"]
