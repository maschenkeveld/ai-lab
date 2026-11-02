"""Where conversations live between requests.

Pydantic AI has no checkpointer: an agent run is a function of its message history. So the thread's whole
state — message history, the extracted requirements, and any booking waiting on a human — is stored per
`thread_id` here. A paused run is simply a row with status `awaiting_approval`, which means pending
approvals survive a restart and `GET /pending` is a query, not an in-memory registry.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_core import to_jsonable_python

log = logging.getLogger(__name__)

DATABASE_URL = (
    "postgresql://postgres:postgres@postgres.llm-analytics.svc.cluster.local:5432/llm_analytics?sslmode=disable"
)

AWAITING_APPROVAL = "awaiting_approval"


@dataclass
class ThreadState:
    thread_id: str
    messages: list[ModelMessage] = field(default_factory=list)
    requirements: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    pending: dict[str, Any] | None = None


class Store(Protocol):
    async def load(self, thread_id: str) -> ThreadState | None: ...
    async def save(self, state: ThreadState) -> None: ...
    async def pending(self) -> dict[str, dict[str, Any]]: ...
    async def close(self) -> None: ...


class MemoryStore:
    """Process-local store: fully functional, but lost on restart."""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}

    async def load(self, thread_id: str) -> ThreadState | None:
        return self._threads.get(thread_id)

    async def save(self, state: ThreadState) -> None:
        self._threads[state.thread_id] = state

    async def pending(self) -> dict[str, dict[str, Any]]:
        return {t.thread_id: t.pending for t in self._threads.values() if t.status == AWAITING_APPROVAL and t.pending}

    async def close(self) -> None:
        pass


class PostgresStore:
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS agent_threads (
            thread_id    text PRIMARY KEY,
            messages     jsonb NOT NULL,
            requirements jsonb NOT NULL,
            status       text  NOT NULL,
            pending      jsonb,
            updated_at   timestamptz NOT NULL DEFAULT now()
        )"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, url: str) -> "PostgresStore":
        pool = AsyncConnectionPool(conninfo=url, max_size=10, open=False,
                                   kwargs={"autocommit": True, "prepare_threshold": 0})
        try:
            # wait=True: fail fast and loudly if Postgres is unreachable, instead of retrying forever in the background.
            await pool.open(wait=True, timeout=10)
            async with pool.connection() as conn:
                await conn.execute(cls.SCHEMA)
        except Exception:
            await pool.close()
            raise
        return cls(pool)

    async def load(self, thread_id: str) -> ThreadState | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT messages, requirements, status, pending FROM agent_threads WHERE thread_id = %s", (thread_id,))
            row = await cur.fetchone()
        if row is None:
            return None
        messages, requirements, status, pending = row
        return ThreadState(thread_id, ModelMessagesTypeAdapter.validate_python(messages), requirements, status, pending)

    async def save(self, state: ThreadState) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """INSERT INTO agent_threads (thread_id, messages, requirements, status, pending)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (thread_id) DO UPDATE SET messages = EXCLUDED.messages,
                     requirements = EXCLUDED.requirements, status = EXCLUDED.status,
                     pending = EXCLUDED.pending, updated_at = now()""",
                (state.thread_id, Jsonb(to_jsonable_python(state.messages)), Jsonb(state.requirements),
                 state.status, Jsonb(state.pending) if state.pending is not None else None))

    async def pending(self) -> dict[str, dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT thread_id, pending FROM agent_threads WHERE status = %s AND pending IS NOT NULL ORDER BY updated_at",
                (AWAITING_APPROVAL,))
            return {thread_id: pending for thread_id, pending in await cur.fetchall()}

    async def close(self) -> None:
        await self._pool.close()


async def make_store() -> Store:
    """Postgres if reachable, otherwise an in-memory store (with a loud warning: approvals won't survive a restart)."""
    url = os.getenv("DATABASE_URL", DATABASE_URL)
    try:
        store = await PostgresStore.connect(url)
        log.info("thread store: postgres")
        return store
    except Exception:
        log.warning("Postgres unavailable at %s — using an in-memory thread store", url, exc_info=True)
        return MemoryStore()


__all__ = ["AWAITING_APPROVAL", "MemoryStore", "PostgresStore", "Store", "ThreadState", "make_store"]
