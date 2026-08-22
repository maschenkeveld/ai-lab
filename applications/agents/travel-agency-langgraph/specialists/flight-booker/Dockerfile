FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY travel_flight_booker ./travel_flight_booker
RUN pip install --no-cache-dir .
ENV HOST=0.0.0.0
ENV PORT=4204
CMD ["python", "-m", "travel_flight_booker.server"]
