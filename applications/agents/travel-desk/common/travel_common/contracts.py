"""Typed contracts between the desk's agents.

These are the A2A payloads: the caller sends a request model as a `data` part and the agent replies with its
result model as a `data` artifact. The same classes are used on both sides, so a contract change is a type
error on one side, not a runtime surprise on the other.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")  # REST/MCP payloads carry fields we pass through untouched


# ---- what the traveller asked for ---------------------------------------------------------------------------

# The vocabularies the destination service accepts; anything else is a 400 further down the line.
VIBES = ("culture", "city", "food", "design", "nature", "relax", "nightlife", "beach")
ACTIVITIES = ("museums", "architecture", "shopping", "music", "walking")
BUDGETS = ("low", "mid", "high")


class TripRequirements(_Model):
    """A trip request, extracted from free text by the coordinator."""
    traveller: str = Field(description="Passenger's first name.")
    origin: str = Field(description="3-letter IATA origin airport code.")
    date: str = Field(description="Preferred travel date as YYYY-MM-DD.")
    flexible_days: int = Field(default=0, ge=0, le=7, description="How many days either side of the date are acceptable.")
    budget_level: str = Field(default="mid", description="One of 'low', 'mid', 'high'.")
    vibes: list[str] = Field(default_factory=list, description="Wanted vibes: culture, city, food, design, nature, relax, nightlife, beach.")
    activities: list[str] = Field(default_factory=list, description="Wanted activities: museums, architecture, shopping, music, walking.")
    max_price_eur: float | None = Field(default=None, description="Hard price ceiling for the flight, if the traveller gave one.")
    notes: str = Field(default="", description="Anything else the traveller said that matters (e.g. 'prefers weekends').")

    @model_validator(mode="after")
    def _keep_to_the_vocabulary(self) -> "TripRequirements":
        """Whatever a model returns, only valid values leave here. A vibe that is really an activity moves to
        activities (and the reverse), unknown values are dropped, and a bad budget becomes 'mid'."""
        vibes: list[str] = []
        activities: list[str] = []
        for value in [*self.vibes, *self.activities]:
            key = value.strip().lower()
            if key in VIBES and key not in vibes:
                vibes.append(key)
            elif key in ACTIVITIES and key not in activities:
                activities.append(key)
        self.vibes, self.activities = vibes, activities
        self.budget_level = self.budget_level.strip().lower() if self.budget_level.strip().lower() in BUDGETS else "mid"
        self.origin = self.origin.strip().upper()
        return self


# ---- destination-scout --------------------------------------------------------------------------------------


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
    reasoning: str = ""


# ---- flight-scout -------------------------------------------------------------------------------------------


class FlightQuoteRequest(_Model):
    origin: str
    to_iata: str
    dates: list[str] = Field(description="Candidate travel dates, YYYY-MM-DD.")
    preferences: str = ""


class FlightOption(_Model):
    date: str
    flight_number: str
    price: float
    currency: str = "EUR"
    priced_at: str = ""


class FlightOptions(_Model):
    options: list[FlightOption]
    recommended_date: str | None = None
    reasoning: str = ""


# ---- booking-agent (deterministic, no LLM: this is where money moves) ---------------------------------------


class BookingCommand(_Model):
    action: Literal["hold", "confirm", "cancel", "get"]
    booking_code: str = ""
    traveller: str = ""
    origin: str = ""
    to_iata: str = ""
    date: str = ""
    idempotency_key: str = ""
    hold_seconds: int = 900


class BookingState(_Model):
    booking_code: str
    status: Literal["held", "scheduled", "cancelled", "expired"]
    flight_number: str = ""
    origin: str = ""
    to_iata: str = ""
    date: str = ""
    hold_expires_at_ts: int = 0
    idempotent_replay: bool = False


# ---- fare-watcher -------------------------------------------------------------------------------------------


class WatchRequest(_Model):
    action: Literal["watch", "unwatch"] = "watch"
    trip_id: str
    origin: str = ""
    to_iata: str = ""
    date: str = ""
    booked_price: float = 0.0
    currency: str = "EUR"
    callback_url: str = ""
    interval_seconds: float = 30
    max_checks: int = 20
    min_drop_pct: float = 8.0
    min_drop_eur: float = 10.0


class WatchState(_Model):
    watch_id: str
    trip_id: str
    status: Literal["watching", "stopped", "completed"]
    baseline_price: float = 0.0
    last_price: float = 0.0
    checks: int = 0


class FareAssessment(_Model):
    worth_rebooking: bool
    reasoning: str


class FareEvent(_Model):
    """Sent by the fare-watcher to the coordinator when a booked fare has dropped enough to matter."""
    type: Literal["fare_event"] = "fare_event"
    trip_id: str
    watch_id: str
    old_price: float
    new_price: float
    currency: str = "EUR"
    drop_pct: float
    priced_at: str = ""
    assessment: FareAssessment


# ---- coordinator --------------------------------------------------------------------------------------------


class CreateTrip(_Model):
    type: Literal["create_trip"] = "create_trip"
    request: str = Field(description="The traveller's request in plain language.")
    requested_by: str = "anonymous"


class Ack(_Model):
    ok: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)
