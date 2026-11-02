"""The orchestrator's A2A face.

Other agents (and the demo) reach the orchestrator with `message/send`: a text part carrying the trip request,
optionally a `data` part {"prompt": ..., "thread_id": ...}. The A2A `contextId` is the conversation, so it is
used as the thread id unless one is given. The task ends `completed` with the trip summary as a data artifact,
or `input-required` when a booking is waiting for a human — decide it with `POST /approve` (see server.py).
"""
import uuid
from dataclasses import dataclass
from typing import Any

from fasta2a.schema import Artifact, Message, Part, TaskIdParams, TaskSendParams
from fasta2a.worker import Worker

from .service import AwaitingApproval, TripService
from .store import AWAITING_APPROVAL


@dataclass
class TripWorker(Worker[list]):
    service: TripService | None = None  # set once the thread store is connected (server lifespan)

    async def run_task(self, params: TaskSendParams) -> None:
        task = await self.storage.load_task(params["id"])
        if task is None:
            raise ValueError(f"Task {params['id']} not found")
        task_id, context_id = task["id"], task["context_id"]
        await self.storage.update_task(task_id, state="working")
        await self.publish_status(task_id, context_id, "working")

        prompt, thread_id = request_from(task.get("history", []), default_thread=context_id)
        try:
            summary = await self.service.run_trip(prompt, thread_id)
        except AwaitingApproval:
            note = "A booking on this thread is waiting for a decision; POST /approve first."
            await self.storage.update_task(task_id, state="input-required", new_messages=[agent_text(note)])
            return
        artifact = Artifact(artifact_id=str(uuid.uuid4()), name="result",
                            parts=[Part(data={"result": {"thread_id": thread_id, **summary}})])
        state = "input-required" if summary["status"] == AWAITING_APPROVAL else "completed"
        await self.storage.update_task(task_id, state=state, new_artifacts=[artifact],
                                       new_messages=[agent_text(summary["answer"])])

    async def cancel_task(self, params: TaskIdParams) -> None:
        pass

    def build_message_history(self, history: list[Message]) -> list:
        return []

    def build_artifacts(self, result: Any) -> list[Artifact]:
        return []


def agent_text(text: str) -> Message:
    return Message(role="agent", parts=[Part(text=text)], message_id=str(uuid.uuid4()))


def request_from(history: list[Message], default_thread: str) -> tuple[str, str]:
    """The trip request and thread id in the last user message."""
    texts: list[str] = []
    thread_id = default_thread
    for message in reversed(history):
        if message["role"] != "user":
            continue
        for part in message["parts"]:
            if "text" in part:
                texts.append(part["text"])
            elif "data" in part:
                texts.append(str(part["data"].get("prompt", "")))
                thread_id = str(part["data"].get("thread_id") or thread_id)
        break
    return "\n".join(t for t in reversed(texts) if t), thread_id
