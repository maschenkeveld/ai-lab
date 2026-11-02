"""Shared building blocks for the travel-agency agents.

Each agent is a Pydantic AI `Agent` served over A2A. Everything that is the same across agents lives
here: the gateway-backed model, MCP toolsets via agentgateway, the A2A server wrapper and client, and
OpenTelemetry setup.
"""
