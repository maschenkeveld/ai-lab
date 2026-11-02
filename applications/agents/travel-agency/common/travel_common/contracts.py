"""Typed contracts between the orchestrator and the specialists.

These are the A2A payloads: the caller sends a request model as a `data` part, the specialist replies with
its result model as a `data` artifact. The same classes are used on both sides, so a contract change is a
type error on one side, not a runtime surprise on the other.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")  # REST/MCP payloads carry fields we pass through untouched


class Destination(_Model):
    name: str
    country: str
    airport_iata_codes: list[str] = Field(default_factory=list)
    blurb: str = ""


class DestinationRequest(_Model):
    vibes: list[str] = Field(default_factory=list)
    budget_level: str = ""
    activities: list[str] = Field(default_factory=list)
    origin: str = ""
    limit: int = 5


class DestinationResult(_Model):
    destinations: list[Destination]


class DecisionRequest(_Model):
    shortlist: list[Destination]
    tried_iata_codes: list[str] = Field(default_factory=list)


class DecisionResult(_Model):
    selected_destination: Destination | None


class FlightRequest(_Model):
    origin: str
    to_iata: str
    date: str


class Flight(_Model):
    flight_number: str
    price: float
    currency: str = ""


class FlightResult(_Model):
    flight: Flight | None


class BookingRequest(_Model):
    passenger_name: str
    from_iata: str
    to_iata: str
    date: str


class BookingResult(_Model):
    booking: dict[str, Any]
