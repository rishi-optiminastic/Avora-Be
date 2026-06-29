"""IdempotencyKey — a generic, replay-proof record of one client request.

A caller protects a non-idempotent or expensive operation (an LLM call, an email
send, a row create) by sending an `Idempotency-Key` header. The first request
under a key runs the operation and stores its serialized response; any replay of
the same key returns the stored response instead of running the operation again
— a looped curl can't burn LLM money, double-send an email, or create duplicate
rows.

Scope is `(principal_id, scope, idempotency_key)`:
- `principal_id` ties the key to the authenticated caller, so one user's key can
  never resolve (or collide with) another user's record.
- `scope` is the operation name (e.g. "tasks.parse"), so the same key reused on a
  different endpoint is a separate record rather than a false hit.

`request_hash` fingerprints the request body. A key replayed with a *different*
body is a client bug (or an attempt to smuggle a new payload under an old key) —
we reject it (422) rather than silently returning the old answer.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class IdempotencyKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "idempotency_keys"

    # The first inserter of a (principal, scope, key) holds the row until its
    # transaction commits; a concurrent twin blocks on this unique index and then
    # loses with an IntegrityError, re-reads the winner's completed row, and
    # replays it — without ever running the operation a second time.
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_keys_principal_scope_key",
        ),
    )

    # The authenticated caller (employee id). Keys are namespaced per caller.
    principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # Operation name, e.g. "tasks.parse" — keeps the same key distinct per route.
    scope: Mapped[str] = mapped_column(String(64))
    # The client-supplied Idempotency-Key header.
    idempotency_key: Mapped[str] = mapped_column(String(64))
    # sha256 hex of the canonical request body — guards against key reuse with a
    # changed payload.
    request_hash: Mapped[str] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(16), default=IdempotencyStatus.IN_PROGRESS)
    # The stored response, wrapped as {"data": <payload>} so the column is always a
    # JSON object even when the endpoint returns a list. NULL while in progress.
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    response_status: Mapped[int | None] = mapped_column(Integer, default=None)
