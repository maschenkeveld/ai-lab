"""The coordinator's HTTP surface.

    /desk/                       the single-page app (static files)
    /desk/api/...                REST: trips, approvals, audit, policy
    /a2a/trip-coordinator/       the same coordinator as an A2A agent (create_trip, fare_event, get_trip)
    /health                      Kubernetes probe

Both prefixes are what agentgateway forwards unchanged, so the same app answers there and on a port-forward.
"""
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from travel_common.contracts import CreateTrip, FareEvent

from .auth import APPROVER, REQUESTER, Authenticator, User
from .policy import describe
from .service import DeskService
from .store import DecisionConflict, Forbidden, NotFound

UI_PREFIX = "/desk"
STATIC = Path(__file__).parent / "static"
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
FLIGHTS_URL = os.getenv("FLIGHTS_REST_URL", "http://rest-flights.rest-flights.svc.cluster.local:8000")


class NewTrip(BaseModel):
    request: str = Field(min_length=3, description="The traveller's request in plain language.")
    requested_by: str = "anonymous"  # only used when authentication is off; with Keycloak the token says who


class Decision(BaseModel):
    approver: str | None = None  # only used when authentication is off; with Keycloak the token says who
    decision: str = Field(pattern="^(approve|reject|edit)$")
    comment: str = ""
    edit: dict[str, Any] | None = None


class CancelTrip(BaseModel):
    actor: str = "anonymous"  # only used when authentication is off


class FareDrop(BaseModel):
    pct: float = Field(default=-25, le=-1, ge=-90)


def build_router(service: DeskService, auth: Authenticator) -> APIRouter:
    r = APIRouter(prefix=f"{UI_PREFIX}/api")
    caller = Depends(auth.current)  # a User from a verified token, or None when authentication is off

    def require_approver(user: User | None) -> None:
        if user is not None and not user.is_approver:
            raise Forbidden(f"this needs the '{APPROVER}' role")

    def sees(user: User | None, trip: dict) -> bool:
        return user is None or user.is_approver or trip["requested_by"] == user.username

    @r.get("/config")
    async def config() -> dict:
        """Public: what the web app needs before anyone has signed in."""
        return {"demo_mode": DEMO_MODE, "policy": describe(service.deps.policy), "auth": auth.public_config()}

    @r.get("/me")
    async def me(user: User | None = caller) -> dict:
        return {"authenticated": user is not None, "username": user.username if user else None,
                "roles": sorted(user.roles) if user else [], "is_approver": user.is_approver if user else True}

    @r.get("/trips")
    async def trips(user: User | None = caller) -> list[dict]:
        rows = await service.store.list_trips()
        return [t for t in rows if sees(user, t)]

    @r.post("/trips", status_code=202)
    async def create_trip(body: NewTrip, user: User | None = caller) -> dict:
        if user is not None and not user.can_request:
            raise Forbidden(f"this needs the '{REQUESTER}' or '{APPROVER}' role")
        return await service.create_trip(body.request, user.username if user else body.requested_by)

    @r.get("/trips/{trip_id}")
    async def trip(trip_id: str, user: User | None = caller) -> dict:
        snapshot = await service.snapshot(trip_id)
        if not sees(user, snapshot["trip"]):
            raise Forbidden("this trip belongs to someone else")
        return snapshot

    @r.post("/trips/{trip_id}/cancel")
    async def cancel(trip_id: str, body: CancelTrip, user: User | None = caller) -> dict:
        if not sees(user, await service.store.get_trip(trip_id)):
            raise Forbidden("this trip belongs to someone else")
        return await service.cancel_trip(trip_id, user.username if user else body.actor)

    @r.get("/approvals")
    async def approvals(status: str | None = Query(None, pattern="^(pending|approved|rejected|expired|superseded|cancelled)$"),
                        user: User | None = caller) -> list[dict]:
        require_approver(user)
        return await service.store.list_approvals(status=status)

    @r.get("/approvals/{approval_id}")
    async def approval(approval_id: str, user: User | None = caller) -> dict:
        row = await service.store.get_approval(approval_id)
        if not sees(user, await service.store.get_trip(row["trip_id"])):
            raise Forbidden("this approval belongs to someone else's trip")
        return row

    @r.post("/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: Decision, user: User | None = caller) -> dict:
        require_approver(user)
        approver = user.username if user else (body.approver or "")
        if not approver.strip():
            raise HTTPException(422, "an approver name is required")
        return await service.decide(approval_id, approver, body.decision, body.comment, body.edit)

    @r.get("/audit")
    async def audit(trip_id: str | None = None, limit: int = Query(100, le=500), user: User | None = caller) -> list[dict]:
        if trip_id:
            if not sees(user, await service.store.get_trip(trip_id)):
                raise Forbidden("this trip belongs to someone else")
        else:
            require_approver(user)
        return await service.store.audit_log(trip_id, limit)

    @r.post("/demo/fare-drop/{trip_id}")
    async def fare_drop(trip_id: str, body: FareDrop, user: User | None = caller) -> dict:
        """Demo helper: push the booked route's fare down so the fare-watcher notices on its next check."""
        require_approver(user)
        if not DEMO_MODE:
            raise HTTPException(404, "demo mode is off (set DEMO_MODE=true)")
        trip_row = await service.store.get_trip(trip_id)
        proposal = trip_row.get("proposal") or {}
        if not proposal or trip_row["status"] not in ("booked", "rebooking"):
            raise DecisionConflict("fare drops only apply to a booked trip")
        shock = {"from": proposal["origin"], "to": proposal["to_iata"], "date": proposal["flight"]["date"], "pct": body.pct}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{FLIGHTS_URL}/v1/fare-shocks", json=shock)
        if response.status_code != 200:
            raise HTTPException(502, f"flights service refused the fare shock: {response.text}")
        await service.store.audit(user.username if user else "demo", "fare_shock", trip_id=trip_id, **shock)
        return shock

    return r


def build_app(service: DeskService, lifespan, auth: Authenticator | None = None) -> FastAPI:
    app = FastAPI(title="Travel desk coordinator", lifespan=lifespan)

    @app.exception_handler(NotFound)
    async def _not_found(_, exc: NotFound):
        return JSONResponse({"error": "not_found", "detail": f"no such record: {exc}"}, 404)

    @app.exception_handler(Forbidden)
    async def _forbidden(_, exc: Forbidden):
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, 403)

    @app.exception_handler(DecisionConflict)
    async def _conflict(_, exc: DecisionConflict):
        return JSONResponse({"error": "conflict", "detail": str(exc)}, 409)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": "trip-coordinator"}

    app.include_router(build_router(service, auth or Authenticator(enabled=False)))

    @app.get(f"{UI_PREFIX}")
    @app.get(f"{UI_PREFIX}/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount(f"{UI_PREFIX}/static", StaticFiles(directory=STATIC), name="static")
    return app


async def a2a_handler(service: DeskService, data: dict[str, Any]) -> dict[str, Any]:
    """Other agents reach the coordinator with a typed message: create a trip, report a fare drop, or look a trip up."""
    kind = data.get("type")
    if kind == "create_trip":
        command = CreateTrip.model_validate(data)
        trip = await service.create_trip(command.request, command.requested_by)
        return {"trip_id": trip["id"], "status": trip["status"]}
    if kind == "fare_event":
        return await service.fare_event(FareEvent.model_validate(data))
    if kind == "get_trip":
        snapshot = await service.snapshot(str(data["trip_id"]))
        trip = snapshot["trip"]
        return {"trip_id": trip["id"], "status": trip["status"], "summary": trip["summary"], "price": trip["price"],
                "pending_approvals": [a["id"] for a in snapshot["approvals"] if a["status"] == "pending"]}
    raise ValueError(f"unknown message type {kind!r}; expected create_trip, fare_event or get_trip")
