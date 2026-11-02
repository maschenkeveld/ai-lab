import os

from pydantic import BaseModel, Field

DEFAULT_PASSENGER = os.getenv("DEFAULT_PASSENGER", "Maarten")
DEFAULT_ORIGIN = os.getenv("DEFAULT_ORIGIN", "AMS")
DEFAULT_DATE = os.getenv("DEFAULT_TRAVEL_DATE", "2026-07-15")


class TripRequirements(BaseModel):
    """What the traveller asked for, extracted from free text by the requirements agent.

    Each field's description is sent to the model as part of the output schema, so it directly shapes
    what gets extracted (and what the defaults are when the traveller doesn't say).
    """
    passenger_name: str = Field(description=f"Passenger's first name. Default '{DEFAULT_PASSENGER}' if not mentioned.")
    origin: str = Field(description=f"3-letter IATA origin airport code. Default '{DEFAULT_ORIGIN}' if not mentioned.")
    date: str = Field(description=f"Travel date as YYYY-MM-DD. Default '{DEFAULT_DATE}' if not mentioned.")
    region: str = Field(default="Europe", description="Region to travel within.")
    budget_level: str = Field(description="One of 'low', 'mid', 'high'. Default 'high' if not mentioned.")
    vibes: list[str] = Field(
        description="Desired trip vibes, chosen from: culture, city, food, design, nature, relax, "
                    "nightlife, beach. Default ['culture', 'city'] if none are mentioned."
    )
    activities: list[str] = Field(
        description="Desired activities, chosen from: museums, architecture, shopping, music, "
                    "walking. Default ['museums'] if none are mentioned."
    )
