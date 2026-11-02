"""What the REST API and the A2A handler do, in one place so both (and the tests) share it."""
import logging
from typing import Any

from dbos import DBOS, SetWorkflowID
from travel_common.contracts import BookingCommand, FareEvent, WatchRequest

from .store import TERMINAL_TRIP, DecisionConflict, NotFound, new_id
from .workflow import Deps, trip_workflow

log = logging.getLogger(__name__)


class DeskService:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.store = deps.store

    # ---- trips -----------------------------------------------------------------------------------------

    async def create_trip(self, request_text: str, requested_by: str = "anonymous") -> dict:
        text = request_text.strip()
        if not text:
            raise DecisionConflict("the trip request is empty")
        trip_id = new_id("trip")
        trip = await self.store.create_trip(trip_id, requested_by, text)
        with SetWorkflowID(trip_id):
            await DBOS.start_workflow_async(trip_workflow, trip_id, text)
        return trip

    async def snapshot(self, trip_id: str) -> dict:
        trip = await self.store.get_trip(trip_id)
        return {"trip": trip,
                "approvals": await self.store.list_approvals(trip_id=trip_id),
                "audit": list(reversed(await self.store.audit_log(trip_id)))}

    async def cancel_trip(self, trip_id: str, actor: str) -> dict:
        """Stop a trip: halt its workflow, release or cancel its booking, close open approvals."""
        trip = await self.store.get_trip(trip_id)
        if trip["status"] in TERMINAL_TRIP:
            raise DecisionConflict(f"the trip is already {trip['status']}")
        await DBOS.cancel_workflow_async(trip_id)
        booking = trip.get("booking") or {}
        if booking.get("booking_code") and booking.get("status") in ("held", "scheduled"):
            await self.deps.specialists.booking(BookingCommand(action="cancel", booking_code=booking["booking_code"]))
        try:
            await self.deps.specialists.watch(WatchRequest(action="unwatch", trip_id=trip_id))
        except Exception as exc:  # the watcher may never have been told about this trip
            log.info("unwatch %s: %s", trip_id, exc)
        await self.store.cancel_approvals(trip_id)
        await self.store.update_trip(trip_id, status="cancelled", summary=f"Cancelled by {actor}.")
        await self.store.audit(actor, "trip_cancelled", trip_id=trip_id)
        return await self.store.get_trip(trip_id)

    # ---- approvals -------------------------------------------------------------------------------------

    async def decide(self, approval_id: str, approver: str, decision: str, comment: str = "",
                     edit: dict[str, Any] | None = None) -> dict:
        approval, message = await self.store.decide(approval_id, approver, decision, comment, edit)
        if message is not None:
            await DBOS.send_async(approval["trip_id"], message, topic=f"approval:{approval_id}")
        return approval

    # ---- fare events -----------------------------------------------------------------------------------

    async def fare_event(self, event: FareEvent) -> dict:
        try:
            trip = await self.store.get_trip(event.trip_id)
        except NotFound:
            return {"accepted": False, "reason": "unknown trip"}
        if trip["status"] in TERMINAL_TRIP:
            return {"accepted": False, "reason": f"trip is {trip['status']}"}
        await self.store.audit("fare-watcher", "fare_drop", trip_id=event.trip_id, old_price=event.old_price,
                               new_price=event.new_price, drop_pct=event.drop_pct,
                               worth_rebooking=event.assessment.worth_rebooking, reason=event.assessment.reasoning)
        await DBOS.send_async(event.trip_id, event.model_dump(mode="json"), topic="fare")
        return {"accepted": True}
