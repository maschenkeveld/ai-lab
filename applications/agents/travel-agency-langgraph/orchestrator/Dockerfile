FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY travel_orchestrator ./travel_orchestrator
RUN pip install --no-cache-dir .

ENV HOST=0.0.0.0
ENV PORT=4200

CMD ["python", "-m", "travel_orchestrator.server"]
