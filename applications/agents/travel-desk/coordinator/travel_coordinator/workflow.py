"""The trip workflow: research, hold, human approval, confirm, then watch the fare.

It is a DBOS workflow, so it is durable. Every effect on the outside world (calling an agent, writing a row)
is a checkpointed step, and waiting for a person is a durable `recv`: if the pod dies while a trip waits for
approval, the restarted pod resumes the same workflow and keeps waiting. Nothing here depends on in-memory state.

The booking is a saga. A flight is first *held* (reserved, not paid), then a human approves it (or policy
does), then it is *confirmed*. Rejecting, expiry, an edit or any failure *cancels the hold*. The hold carries an
idempotency key, so a retried step never creates a second one.

    researching -> awaiting_approval -> booked -> completed
                       |  \\-> (edit: back to researching)
                       \\-> rejected | expired | failed | cancelled
"""
import logging
import time
from dataclasses import dataclass
from typing import Any

from dbos import DBOS
from travel_common.contracts import (
    BookingCommand,
    BookingState,
    FareEvent,
    TripRequirements,
    WatchRequest,
)

from .agents import Llm, Specialists, research
from .policy import required_approvals
from .settings import Agents, Policy
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Deps:
    store: Store
    specialists: Specialists
    llm: Llm
    policy: Policy
    agents: Agents


_deps: Deps | None = None


def configure(deps: Deps) -> None:
    global _deps
    _deps = deps


def d() -> Deps:
    assert _deps is not None, "workflow dependencies are not configured"
    return _deps


STEP = dict(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)

# ---- steps: each is checkpointed, so it runs once per workflow even across restarts -------------------------


@DBOS.step()
async def s_now() -> float:
    return time.time()


@DBOS.step(**STEP)
async def s_extract(trip_id: str, text: str) -> dict:
    req = await d().llm.extract(text)
    await d().store.update_trip(trip_id, requirements=req.model_dump(mode="json"))
    await d().store.audit("coordinator", "requirements_extracted", trip_id=trip_id, requirements=req.model_dump(mode="json"))
    return req.model_dump(mode="json")


@DBOS.step()
async def s_status(trip_id: str, status: str, summary: str | None = None, **fields: Any) -> None:
    if summary is not None:
        fields["summary"] = summary
    await d().store.update_trip(trip_id, status=status, **fields)
    await d().store.audit("coordinator", f"status_{status}", trip_id=trip_id, **({"summary": summary} if summary else {}))


@DBOS.step()
async def s_audit(trip_id: str, actor: str, type: str, detail: dict) -> None:
    await d().store.audit(actor, type, trip_id=trip_id, **detail)


@DBOS.step(**STEP)
async def s_research(trip_id: str, req: dict, overrides: dict) -> dict | None:
    proposal = await research(d().specialists, TripRequirements.model_validate(req), overrides)
    await d().store.audit("coordinator", "research_done", trip_id=trip_id, found=proposal is not None,
                          overrides=overrides, **({"destination": proposal["destination"]["name"], "price": proposal["price"]} if proposal else {}))
    return proposal


@DBOS.step(**STEP)
async def s_summarize(proposal: dict) -> str:
    return await d().llm.summarize({k: proposal[k] for k in ("origin", "destination", "flight", "alternatives", "reasoning") if k in proposal})


@DBOS.step(**STEP)
async def s_booking(command: dict, trip_id: str, track: bool = True) -> dict:
    """One call to the booking agent. hold/confirm/cancel are all idempotent, so a retry is safe.

    `track` mirrors the result onto the trip row, so the trip always shows the supplier's state. A rebooking turns it
    off for the cancellation of the superseded booking, which is not the trip's booking any more."""
    state = await d().specialists.booking(BookingCommand.model_validate(command))
    await d().store.audit("booking-agent", f"booking_{command['action']}", trip_id=trip_id,
                          booking_code=state.booking_code, status=state.status)
    if track:
        await d().store.update_trip(trip_id, booking=state.model_dump(mode="json"))  # the trip row mirrors the supplier
    return state.model_dump(mode="json")


@DBOS.step()
async def s_create_approval(trip_id: str, approval_id: str, kind: str, required: int, proposal: dict, summary: str) -> dict:
    p = d().policy
    row = await d().store.create_approval(approval_id=approval_id, trip_id=trip_id, kind=kind, required=required,
                                          proposal=proposal, summary=summary, ttl_seconds=p.approval_ttl_seconds,
                                          escalate_after_seconds=min(p.escalate_after_seconds, p.approval_ttl_seconds))
    return {"id": row["id"], "expires_ts": row["expires_at"].timestamp(), "escalate_ts": row["escalate_at"].timestamp()}


@DBOS.step()
async def s_escalate(approval_id: str) -> None:
    await d().store.escalate_approval(approval_id)


@DBOS.step()
async def s_expire(approval_id: str) -> bool:
    return await d().store.expire_approval(approval_id)


@DBOS.step(**STEP)
async def s_watch(trip_id: str, proposal: dict, booked_price: float) -> None:
    await d().specialists.watch(WatchRequest(
        action="watch", trip_id=trip_id, origin=proposal["origin"], to_iata=proposal["to_iata"], date=proposal["flight"]["date"],
        booked_price=booked_price, currency=proposal.get("currency", "EUR"), callback_url=d().agents.self_url))


@DBOS.step(**STEP)
async def s_unwatch(trip_id: str) -> None:
    await d().specialists.watch(WatchRequest(action="unwatch", trip_id=trip_id))


# ---- waiting for a person ------------------------------------------------------------------------------------


async def wait_for_decision(approval: dict) -> dict:
    """Block, durably, until a human decides, escalating when it takes too long and expiring at the deadline.

    Returns {"decision": "approved" | "rejected" | "edit" | "expired", ...}. A decision that races the expiry wins.
    """
    topic = f"approval:{approval['id']}"
    now = await s_now()
    msg = await DBOS.recv_async(topic, timeout_seconds=max(approval["escalate_ts"] - now, 0))
    if msg is not None:
        return msg
    await s_escalate(approval["id"])
    now = await s_now()
    msg = await DBOS.recv_async(topic, timeout_seconds=max(approval["expires_ts"] - now, 0))
    if msg is not None:
        return msg
    if await s_expire(approval["id"]):
        return {"decision": "expired"}
    msg = await DBOS.recv_async(topic, timeout_seconds=5)  # a decision committed just before we expired it
    return msg if msg is not None else {"decision": "expired"}


def _hold_command(trip_id: str, proposal: dict, key: str) -> dict:
    p = d().policy
    return BookingCommand(action="hold", traveller=proposal["traveller"], origin=proposal["origin"], to_iata=proposal["to_iata"],
                          date=proposal["flight"]["date"], idempotency_key=key,
                          hold_seconds=int(p.approval_ttl_seconds + p.hold_buffer_seconds)).model_dump(mode="json")


def _cmd(action: str, code: str) -> dict:
    return BookingCommand(action=action, booking_code=code).model_dump(mode="json")


# ---- the workflow ----------------------------------------------------------------------------------------------


@DBOS.workflow()
async def trip_workflow(trip_id: str, request_text: str) -> str:
    """Run one trip to the end. Returns its final status."""
    hold: dict | None = None
    try:
        req = await s_extract(trip_id, request_text)

        # 1. research, hold, approve: loops when an approver edits the proposal
        overrides: dict = {}
        attempt = 0
        while True:
            attempt += 1
            await s_status(trip_id, "researching")
            proposal = await s_research(trip_id, req, overrides)
            if proposal is None:
                await s_status(trip_id, "failed", summary="No flight matched the request and its constraints.")
                return "failed"
            hold = await s_booking(_hold_command(trip_id, proposal, f"{trip_id}:hold:{attempt}"), trip_id)
            proposal = {**proposal, "booking_code": hold["booking_code"]}
            summary = await s_summarize(proposal)
            await s_status(trip_id, "holding", proposal=proposal, booking=hold, price=proposal["price"],
                           currency=proposal["currency"], summary=summary)

            need = required_approvals(proposal["price"], d().policy)
            if need == 0:
                await s_audit(trip_id, "policy", "auto_approved", {"price": proposal["price"], "below": d().policy.auto_approve_below})
                decision: dict = {"decision": "approved", "by": ["policy"]}
            else:
                approval = await s_create_approval(trip_id, f"{trip_id}-b{attempt}", "booking", need, proposal, summary)
                await s_status(trip_id, "awaiting_approval")
                decision = await wait_for_decision(approval)

            if decision["decision"] == "approved":
                break
            hold = await s_booking(_cmd("cancel", hold["booking_code"]), trip_id)  # compensate: release the hold
            hold = None
            if decision["decision"] == "edit" and attempt <= d().policy.max_edits:
                overrides = decision.get("edit") or {}
                continue
            final = {"rejected": "rejected", "expired": "expired"}.get(decision["decision"], "failed")
            reason = {"rejected": f"Rejected by {decision.get('by')}: {decision.get('comment') or 'no comment'}",
                      "expired": "Nobody decided in time; the hold was released.",
                      "edit": f"Edited more than {d().policy.max_edits} times; gave up."}.get(decision["decision"], "")
            await s_status(trip_id, final, summary=reason)
            return final

        # 2. confirm the held booking
        booked = await s_booking(_cmd("confirm", hold["booking_code"]), trip_id)
        if BookingState.model_validate(booked).status != "scheduled":
            await s_status(trip_id, "failed", summary=f"The hold was {booked['status']} before it could be confirmed.")
            return "failed"
        hold = None
        await s_status(trip_id, "booked", booking=booked, summary=f"Booked {booked['booking_code']}. {summary}")
        await s_watch(trip_id, proposal, proposal["price"])

        # 3. watch the fare until the monitoring window ends; propose a rebooking when it drops enough
        deadline = await s_now() + d().policy.monitor_seconds
        rebooks = 0
        while True:
            remaining = deadline - await s_now()
            if remaining <= 0:
                break
            msg = await DBOS.recv_async("fare", timeout_seconds=remaining)
            if msg is None:
                break
            event = FareEvent.model_validate(msg)
            if not event.assessment.worth_rebooking:
                await s_audit(trip_id, "fare-watcher", "fare_drop_ignored", {"new_price": event.new_price, "reason": event.assessment.reasoning})
                continue
            rebooks += 1
            booked, proposal = await rebook(trip_id, rebooks, booked, proposal, event)

        await s_unwatch(trip_id)
        await s_status(trip_id, "completed", summary="Monitoring window ended; the booking stands.")
        return "completed"
    except Exception as exc:
        log.exception("trip %s failed", trip_id)
        await s_audit(trip_id, "coordinator", "workflow_error", {"error": f"{type(exc).__name__}: {exc}"})
        if hold is not None:
            try:
                await s_booking(_cmd("cancel", hold["booking_code"]), trip_id)
            except Exception:
                log.exception("could not release the hold %s", hold["booking_code"])
        await s_status(trip_id, "failed", summary=f"{type(exc).__name__}: {exc}")
        return "failed"


async def rebook(trip_id: str, n: int, booked: dict, proposal: dict, event: FareEvent) -> tuple[dict, dict]:
    """The fare dropped: ask for approval to move to the lower fare. Hold the new booking first, confirm it, only then
    cancel the old one, so the traveller is never left without a seat. Returns the (possibly new) booking and proposal."""
    saving = round(event.old_price - event.new_price, 2)
    new_proposal = {**proposal, "price": event.new_price, "flight": {**proposal["flight"], "price": event.new_price},
                    "previous_booking_code": booked["booking_code"], "saving": saving, "drop_pct": event.drop_pct,
                    "kind": "rebook"}
    new_proposal.pop("alternatives", None)
    summary = (f"The fare dropped from {event.old_price} to {event.new_price} {event.currency} (-{event.drop_pct}%). "
               f"{event.assessment.reasoning} Approve to rebook and cancel {booked['booking_code']}.")
    need = required_approvals(event.new_price, d().policy)
    if need == 0:
        await s_audit(trip_id, "policy", "auto_approved", {"kind": "rebook", "price": event.new_price})
        decision: dict = {"decision": "approved", "by": ["policy"]}
    else:
        approval = await s_create_approval(trip_id, f"{trip_id}-r{n}", "rebook", need, new_proposal, summary)
        await s_status(trip_id, "awaiting_approval")
        decision = await wait_for_decision(approval)
    if decision["decision"] != "approved":
        await s_audit(trip_id, "coordinator", "rebook_declined", {"decision": decision["decision"]})
        await s_status(trip_id, "booked")
        return booked, proposal
    await s_status(trip_id, "rebooking")
    new_hold = await s_booking(_hold_command(trip_id, new_proposal, f"{trip_id}:rebook:{n}"), trip_id)
    new_booked = await s_booking(_cmd("confirm", new_hold["booking_code"]), trip_id)
    if BookingState.model_validate(new_booked).status != "scheduled":
        await s_audit(trip_id, "coordinator", "rebook_failed", {"status": new_booked["status"]})
        await s_status(trip_id, "booked")
        return booked, proposal
    await s_booking(_cmd("cancel", booked["booking_code"]), trip_id, False)  # the old booking, not the trip's any more
    new_proposal = {**new_proposal, "booking_code": new_booked["booking_code"]}
    await s_status(trip_id, "booked", booking=new_booked, proposal=new_proposal, price=event.new_price,
                   summary=f"Rebooked to {new_booked['booking_code']}, saving {saving} {event.currency}.")
    await s_unwatch(trip_id)
    await s_watch(trip_id, new_proposal, event.new_price)
    return new_booked, new_proposal
