# --- build stage ---
FROM golang:1.25-bookworm AS builder

WORKDIR /app

# Better caching
COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libsqlite3-dev \
  && rm -rf /var/lib/apt/lists/*

ENV CGO_ENABLED=1
RUN GOOS=linux go build -trimpath -o server .

# --- run stage ---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlite3-0 ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# ensure DB directory exists
RUN mkdir -p /data

WORKDIR /app
COPY --from=builder /app/server /app/server

ENV ADDR=:8000
ENV DB_PATH=/data/bookings.db

EXPOSE 8000
CMD ["/app/server"]
