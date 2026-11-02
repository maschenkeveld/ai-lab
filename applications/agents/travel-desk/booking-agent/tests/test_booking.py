import pytest
from booking_agent import server

REST = {"booking_code": "ABC234", "status": "held", "flight_number": "KA-AMSVIE", "from": "AMS", "to": "VIE",
        "date": "2026-12-18", "hold_expires_at_ts": 1893456000}


class FakeMcp:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        out = self.responses[name]
        if isinstance(out, Exception):
            raise out
        return out


def use(monkeypatch, **responses) -> FakeMcp:
    fake = FakeMcp(responses)
    monkeypatch.setattr(server, "bookings", fake)
    return fake


HOLD = {"action": "hold", "traveller": "Alice", "origin": "AMS", "to_iata": "VIE", "date": "2026-12-18", "idempotency_key": "t1:hold:1"}


async def test_hold_passes_the_idempotency_key_and_maps_the_booking(monkeypatch):
    fake = use(monkeypatch, hold_booking={"booking": REST})
    out = await server.handle(HOLD)
    assert fake.calls[0][1]["idempotency_key"] == "t1:hold:1"
    assert out["booking_code"] == "ABC234" and out["status"] == "held" and out["origin"] == "AMS" and out["to_iata"] == "VIE"


async def test_a_replayed_hold_is_flagged(monkeypatch):
    use(monkeypatch, hold_booking={"booking": {**REST, "idempotent_replay": True}})
    assert (await server.handle(HOLD))["idempotent_replay"] is True


async def test_confirm_and_cancel(monkeypatch):
    use(monkeypatch, confirm_booking={"booking": {**REST, "status": "scheduled"}}, cancel_booking={"booking": {**REST, "status": "cancelled"}})
    assert (await server.handle({"action": "confirm", "booking_code": "ABC234"}))["status"] == "scheduled"
    assert (await server.handle({"action": "cancel", "booking_code": "ABC234"}))["status"] == "cancelled"


async def test_confirming_an_expired_hold_reports_its_state_instead_of_failing(monkeypatch):
    use(monkeypatch, confirm_booking=RuntimeError("Client error '409 Conflict' for url ..."),
        get_booking={"booking": {**REST, "status": "expired"}})
    assert (await server.handle({"action": "confirm", "booking_code": "ABC234"}))["status"] == "expired"


async def test_other_errors_still_fail(monkeypatch):
    use(monkeypatch, confirm_booking=RuntimeError("connection refused"))
    with pytest.raises(RuntimeError, match="refused"):
        await server.handle({"action": "confirm", "booking_code": "ABC234"})


async def test_incomplete_commands_are_rejected_before_any_call(monkeypatch):
    fake = use(monkeypatch)
    with pytest.raises(ValueError, match="idempotency_key"):
        await server.handle({"action": "hold", "traveller": "Alice", "origin": "AMS", "to_iata": "VIE", "date": "2026-12-18"})
    with pytest.raises(ValueError, match="booking_code"):
        await server.handle({"action": "cancel"})
    assert fake.calls == []
