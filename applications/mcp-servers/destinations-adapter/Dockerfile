FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY mcp_destinations_adapter ./mcp_destinations_adapter
RUN pip install --no-cache-dir .

ENV HOST=0.0.0.0
ENV PORT=8000
ENV MCP_PATH=/mcp

CMD ["python", "-m", "mcp_destinations_adapter.server"]
