FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY travel_flight_finder ./travel_flight_finder
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0
ENV PORT=4203
CMD ["python", "-m", "travel_flight_finder.server"]
