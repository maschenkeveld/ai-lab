"""The workflow, offline: real DBOS on a temporary SQLite file, a stub model instead of an LLM."""
import json
import os
import tempfile

import pytest
from dbos import DBOS, DBOSClient, SetWorkflowID
from pydantic_ai.models.test import TestModel

TMP = tempfile.mkdtemp()
os.environ["DURABLE_DIR"] = TMP  # before main is imported: it reads this at import time

import main  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def runtime():
    main.launch()
    yield
    DBOS.destroy()


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    stub = TestModel(custom_output_args={"destination": "Brussels", "reason": "cheap and close"})
    monkeypatch.setattr(main, "planning_model", lambda: stub)


def start(wid: str, ttl: float = 30):
    os.environ["APPROVAL_TTL_SECONDS"] = str(ttl)
    with SetWorkflowID(wid):
        handle = DBOS.start_workflow(main.trip_workflow, "anything")
    for _ in range(100):  # wait until it is holding a flight and waiting for the human
        if DBOS.get_event(wid, "status", timeout_seconds=0.1) == "awaiting_approval":
            break
    return handle


def record() -> list[dict]:
    return [json.loads(line) for line in main.HOLDS.read_text().splitlines()]


def test_an_approved_trip_is_confirmed():
    handle = start("t-approve")
    assert DBOS.get_event("t-approve", "status", timeout_seconds=0) == "awaiting_approval"
    DBOSClient(system_database_url=main.DB_URL).send("t-approve", "approve", topic="approval")
    result = handle.get_result()
    assert result["outcome"] == "booked" and result["destination"] == "Brussels"
    assert [e["kind"] for e in record() if e["code"] == result["code"]] == ["hold", "confirm"]


def test_a_denied_trip_releases_its_hold_and_never_confirms():
    handle = start("t-deny")
    DBOSClient(system_database_url=main.DB_URL).send("t-deny", "too expensive", topic="approval")
    result = handle.get_result()
    assert result == {"outcome": "denied", "code": result["code"], "reason": "too expensive"}
    kinds = [e["kind"] for e in record() if e["code"] == result["code"]]
    assert kinds == ["hold", "release"]


def test_nobody_deciding_expires_the_trip_and_releases_the_hold():
    handle = start("t-expire", ttl=1)
    result = handle.get_result()
    assert result["outcome"] == "expired"
    assert [e["kind"] for e in record() if e["code"] == result["code"]] == ["hold", "release"]


def test_every_step_is_recorded_so_a_restart_can_skip_it():
    handle = start("t-steps")
    DBOSClient(system_database_url=main.DB_URL).send("t-steps", "approve", topic="approval")
    handle.get_result()
    steps = [s["function_name"] for s in DBOSClient(system_database_url=main.DB_URL).list_workflow_steps("t-steps")]
    assert steps[:2] == ["plan_trip", "hold_flight"] and "confirm_flight" in steps
