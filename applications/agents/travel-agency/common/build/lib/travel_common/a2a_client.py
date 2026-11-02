"""Call another agent over A2A: send a message, poll the task, return its typed result.

`message/send` is asynchronous — it returns a `submitted` task — so `call_agent` polls `tasks/get` until
the task reaches a final state. Calls go through agentgateway, so every hop is routed and observable
there, and httpx instrumentation (see otel.py) propagates the trace context.
"""
import asyncio
import time
import uuid
from typing import Any, TypeVar, overload

import httpx
from fasta2a.client import A2AClient
from fasta2a.schema import Message, Part
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_FINAL_FAILURE = {"failed", "canceled", "rejected", "auth-required"}


class A2AError(Exception):
    """The remote agent failed, rejected the task, or didn't answer in time."""


@overload
async def call_agent(url: str, payload: BaseModel | dict[str, Any], result_type: type[T], **kw: Any) -> T: ...
@overload
async def call_agent(url: str, payload: BaseModel | dict[str, Any], result_type: None = None, **kw: Any) -> dict[str, Any]: ...


async def call_agent(
    url: str,
    payload: BaseModel | dict[str, Any],
    result_type: type[T] | None = None,
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.25,
    http_client: httpx.AsyncClient | None = None,
) -> T | dict[str, Any]:
    """Send `payload` to the agent at `url` and return its structured result.

    `url` is the agent's A2A base URL (e.g. "http://agentgateway.lab/a2a/flight-finder").
    """
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout)
    try:
        a2a = A2AClient(url, http_client=client)
        message = Message(role="user", parts=[Part(data=data)], message_id=str(uuid.uuid4()))
        sent = await a2a.send_message(message)
        if "error" in sent:
            raise A2AError(f"{url}: {sent['error']}")
        task = _as_task(sent["result"])

        deadline = time.monotonic() + timeout
        while task["status"]["state"] in ("submitted", "working"):
            if time.monotonic() > deadline:
                raise A2AError(f"{url}: task {task['id']} still {task['status']['state']} after {timeout}s")
            await asyncio.sleep(poll_interval)
            got = await a2a.get_task(task["id"])
            if "error" in got:
                raise A2AError(f"{url}: {got['error']}")
            task = _as_task(got["result"])

        state = task["status"]["state"]
        if state in _FINAL_FAILURE or state == "input-required":
            raise A2AError(f"{url}: task {task['id']} ended in state '{state}'{_cause(task)}")
        result = _result_from(task)
        return result_type.model_validate(result) if result_type else result
    finally:
        if owns_client:
            await client.aclose()


def _as_task(result: Any) -> Any:
    # The server may wrap the task as {"task": {...}} (A2A 1.0) or return it bare.
    return result["task"] if isinstance(result, dict) and "task" in result else result


def _result_from(task: Any) -> dict[str, Any]:
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts", []):
            if "data" in part:
                data = part["data"]
                return data["result"] if isinstance(data, dict) and "result" in data else data
    raise A2AError(f"task {task['id']} completed without a structured result")


def _cause(task: Any) -> str:
    """The agent's last text message on a failed task (see DataAwareWorker.run_task), if it left one."""
    for message in reversed(task.get("history") or []):
        if message.get("role") in ("agent", "ROLE_AGENT"):
            for part in message.get("parts", []):
                if "text" in part:
                    return f": {part['text']}"
    return ""
