FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY travel_destination ./travel_destination
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0
ENV PORT=4201
CMD ["python", "-m", "travel_destination.server"]
