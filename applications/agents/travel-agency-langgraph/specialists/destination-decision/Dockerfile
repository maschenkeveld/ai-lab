FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY travel_destination_decision ./travel_destination_decision
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0
ENV PORT=4202
CMD ["python", "-m", "travel_destination_decision.server"]
