"""Postgres persistence for trips, approvals and the audit log.

The approval is a first-class row, not an in-memory pause: who may approve, what they are approving, how many
approvals it needs, when it expires, and every decision made on it. The audit log is append-only: a trigger
rejects any UPDATE or DELETE, so the history of who decided what cannot be rewritten through the application.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id            text PRIMARY KEY,
    status        text NOT NULL,
    requested_by  text NOT NULL,
    request_text  text NOT NULL,
    requirements  jsonb,
    proposal      jsonb,
    booking       jsonb,
    price         double precision,
    currency      text NOT NULL DEFAULT 'EUR',
    summary       text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS approvals (
    id           text PRIMARY KEY,
    trip_id      text NOT NULL REFERENCES trips(id),
    kind         text NOT NULL,
    status       text NOT NULL,
    required     integer NOT NULL,
    decisions    jsonb NOT NULL DEFAULT '[]',
    proposal     jsonb NOT NULL,
    summary      text NOT NULL DEFAULT '',
    escalated    boolean NOT NULL DEFAULT false,
    expires_at   timestamptz NOT NULL,
    escalate_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status);
CREATE TABLE IF NOT EXISTS audit (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    trip_id      text,
    approval_id  text,
    actor        text NOT NULL,
    type         text NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}'
);
CREATE OR REPLACE FUNCTION audit_is_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'the audit log is append-only'; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_append_only ON audit;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION audit_is_append_only();
"""

TERMINAL_TRIP = {"completed", "rejected", "expired", "cancelled", "failed"}


class DecisionConflict(Exception):
    """The decision cannot be applied (approval already resolved, same approver twice, ...)."""


class Forbidden(Exception):
    """The caller is known but not allowed to do this (not an approver, deciding on their own trip, ...)."""


class NotFound(Exception):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @classmethod
    async def connect(cls, url: str) -> "Store":
        pool = AsyncConnectionPool(url, min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row, "autocommit": True})
        await pool.open(wait=True, timeout=60)
        async with pool.connection() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, *args: Any) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchone()

    async def _all(self, sql: str, *args: Any) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchall()

    async def _run(self, sql: str, *args: Any) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return cur.rowcount

    # ---- audit -----------------------------------------------------------------------------------------

    async def audit(self, actor: str, type: str, *, trip_id: str | None = None, approval_id: str | None = None,
                    **detail: Any) -> None:
        await self._run("INSERT INTO audit (trip_id, approval_id, actor, type, detail) VALUES (%s, %s, %s, %s, %s)",
                        trip_id, approval_id, actor, type, Jsonb(detail))

    async def audit_log(self, trip_id: str | None = None, limit: int = 200) -> list[dict]:
        if trip_id:
            return await self._all("SELECT * FROM audit WHERE trip_id = %s ORDER BY id DESC LIMIT %s", trip_id, limit)
        return await self._all("SELECT * FROM audit ORDER BY id DESC LIMIT %s", limit)

    # ---- trips -----------------------------------------------------------------------------------------

    async def create_trip(self, trip_id: str, requested_by: str, request_text: str) -> dict:
        await self._run("INSERT INTO trips (id, status, requested_by, request_text) VALUES (%s, 'researching', %s, %s) "
                        "ON CONFLICT (id) DO NOTHING", trip_id, requested_by, request_text)
        await self.audit(requested_by, "trip_requested", trip_id=trip_id, request=request_text)
        return await self.get_trip(trip_id)

    async def get_trip(self, trip_id: str) -> dict:
        row = await self._one("SELECT * FROM trips WHERE id = %s", trip_id)
        if row is None:
            raise NotFound(trip_id)
        return row

    async def list_trips(self) -> list[dict]:
        return await self._all("SELECT * FROM trips ORDER BY created_at DESC LIMIT 100")

    async def update_trip(self, trip_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = %s" for k in fields)
        values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        await self._run(f"UPDATE trips SET {cols}, updated_at = now() WHERE id = %s", *values, trip_id)

    # ---- approvals -------------------------------------------------------------------------------------

    async def create_approval(self, *, trip_id: str, kind: str, required: int, proposal: dict, summary: str,
                              ttl_seconds: float, escalate_after_seconds: float, approval_id: str | None = None) -> dict:
        approval_id = approval_id or new_id("apr")
        t0 = now()
        await self._run(
            "INSERT INTO approvals (id, trip_id, kind, status, required, proposal, summary, expires_at, escalate_at) "
            "VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            approval_id, trip_id, kind, required, Jsonb(proposal), summary,
            t0 + timedelta(seconds=ttl_seconds), t0 + timedelta(seconds=escalate_after_seconds))
        await self.audit("system", "approval_requested", trip_id=trip_id, approval_id=approval_id,
                         kind=kind, required=required, price=proposal.get("price"))
        return await self.get_approval(approval_id)

    async def get_approval(self, approval_id: str) -> dict:
        row = await self._one("SELECT * FROM approvals WHERE id = %s", approval_id)
        if row is None:
            raise NotFound(approval_id)
        return row

    async def list_approvals(self, status: str | None = None, trip_id: str | None = None) -> list[dict]:
        clauses, args = [], []
        if status:
            clauses.append("status = %s")
            args.append(status)
        if trip_id:
            clauses.append("trip_id = %s")
            args.append(trip_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self._all(f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT 100", *args)

    async def decide(self, approval_id: str, approver: str, decision: str, comment: str = "",
                     edit: dict | None = None) -> tuple[dict, dict | None]:
        """Apply one human decision, atomically. Returns (approval, message-for-the-workflow or None).

        The message is None while a two-approver booking still waits for its second approver. Raises DecisionConflict
        if the approval is no longer pending or the approver already decided on it.
        """
        approver = approver.strip()
        if not approver:
            raise DecisionConflict("an approver name is required")
        if decision not in ("approve", "reject", "edit"):
            raise DecisionConflict(f"unknown decision '{decision}'")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute("SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval_id,))
                approval = await cur.fetchone()
                if approval is None:
                    raise NotFound(approval_id)
                if approval["status"] != "pending":
                    raise DecisionConflict(f"approval is already {approval['status']}")
                decisions = list(approval["decisions"])
                if any(d["approver"].lower() == approver.lower() for d in decisions):
                    raise DecisionConflict(f"{approver} has already decided on this approval")
                entry = {"approver": approver, "decision": decision, "comment": comment, "at": now().isoformat()}
                if decision == "edit":
                    entry["edit"] = edit or {}
                decisions.append(entry)

                message: dict | None
                if decision == "reject":
                    status, message = "rejected", {"decision": "rejected", "by": approver, "comment": comment}
                elif decision == "edit":
                    status, message = "superseded", {"decision": "edit", "by": approver, "edit": edit or {}, "comment": comment}
                else:
                    approved_by = [d["approver"] for d in decisions if d["decision"] == "approve"]
                    if len(approved_by) >= approval["required"]:
                        status, message = "approved", {"decision": "approved", "by": approved_by}
                    else:
                        status, message = "pending", None
                resolved = "now()" if status != "pending" else "NULL"
                await conn.execute(f"UPDATE approvals SET status = %s, decisions = %s, resolved_at = {resolved} WHERE id = %s",
                                   (status, Jsonb(decisions), approval_id))
                await conn.execute(
                    "INSERT INTO audit (trip_id, approval_id, actor, type, detail) VALUES (%s, %s, %s, %s, %s)",
                    (approval["trip_id"], approval_id, approver, f"approval_{decision}", Jsonb({"comment": comment, **({"edit": edit} if edit else {})})))
        return await self.get_approval(approval_id), message

    async def expire_approval(self, approval_id: str) -> bool:
        """Mark a still-pending approval expired. False if a decision got there first."""
        changed = await self._run("UPDATE approvals SET status = 'expired', resolved_at = now() "
                                  "WHERE id = %s AND status = 'pending'", approval_id)
        if changed:
            row = await self.get_approval(approval_id)
            await self.audit("system", "approval_expired", trip_id=row["trip_id"], approval_id=approval_id)
        return bool(changed)

    async def escalate_approval(self, approval_id: str) -> None:
        changed = await self._run("UPDATE approvals SET escalated = true WHERE id = %s AND status = 'pending' AND NOT escalated",
                                  approval_id)
        if changed:
            row = await self.get_approval(approval_id)
            await self.audit("system", "approval_escalated", trip_id=row["trip_id"], approval_id=approval_id,
                             note="no decision yet; flagged for attention")

    async def cancel_approvals(self, trip_id: str, status: str = "cancelled") -> None:
        await self._run("UPDATE approvals SET status = %s, resolved_at = now() WHERE trip_id = %s AND status = 'pending'",
                        status, trip_id)
