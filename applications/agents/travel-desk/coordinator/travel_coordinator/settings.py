"""Configuration, all from the environment so the same image runs in the demo and in tests."""
import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Policy:
    """When does a booking need a human? Thresholds are on the flight price in EUR."""
    auto_approve_below: float = field(default_factory=lambda: _f("AUTO_APPROVE_BELOW_EUR", 60))
    second_approver_from: float = field(default_factory=lambda: _f("SECOND_APPROVER_FROM_EUR", 180))
    approval_ttl_seconds: float = field(default_factory=lambda: _f("APPROVAL_TTL_SECONDS", 900))
    escalate_after_seconds: float = field(default_factory=lambda: _f("ESCALATE_AFTER_SECONDS", 300))
    hold_buffer_seconds: float = field(default_factory=lambda: _f("HOLD_BUFFER_SECONDS", 300))
    max_edits: int = field(default_factory=lambda: int(_f("MAX_EDITS", 3)))
    monitor_seconds: float = field(default_factory=lambda: _f("MONITOR_SECONDS", 900))
    allow_self_approval: bool = field(default_factory=lambda: os.getenv("ALLOW_SELF_APPROVAL", "false").lower() == "true")


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/coordinator")


@dataclass(frozen=True)
class Agents:
    """A2A base URLs of the other agents. In the cluster these all point at agentgateway."""
    destination_scout: str = field(default_factory=lambda: os.getenv("AGENT_DESTINATION_SCOUT", "http://agentgateway.lab/a2a/destination-scout"))
    flight_scout: str = field(default_factory=lambda: os.getenv("AGENT_FLIGHT_SCOUT", "http://agentgateway.lab/a2a/flight-scout"))
    booking_agent: str = field(default_factory=lambda: os.getenv("AGENT_BOOKING", "http://agentgateway.lab/a2a/booking-agent"))
    fare_watcher: str = field(default_factory=lambda: os.getenv("AGENT_FARE_WATCHER", "http://agentgateway.lab/a2a/fare-watcher"))
    self_url: str = field(default_factory=lambda: os.getenv("PUBLIC_AGENT_BASE_URL", "http://agentgateway.lab/a2a/trip-coordinator"))
