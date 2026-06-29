"""Generic idempotency: run an operation at most once per (caller, scope, key).

Wrap any non-idempotent or expensive handler in :meth:`IdempotencyService.run`.
When the caller sends an ``Idempotency-Key`` header, the first request runs the
operation and stores its serialized response; a replay of the same key returns
the stored response without re-running the work — so a looped/copied request
can't burn an LLM call, double-send an email, or create a duplicate row.

Without a key the call behaves exactly as before (no protection) — the header is
opt-in, so existing clients are unaffected.

Threat handled: the dominant case is a *sequential* replay (a retried or copied
request, a double-click). The claim-before-operate flow plus the DB unique index
also serialize a rare *concurrent* twin: the second request blocks at claim time,
loses, and replays the winner — it never runs the operation twice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.core.exceptions import ConflictError, ValidationError
from app.models.idempotency_key import IdempotencyStatus
from app.repositories.idempotency import IdempotencyRepository

T = TypeVar("T")


class IdempotencyService:
    def __init__(self, repo: IdempotencyRepository) -> None:
        self._repo = repo

    async def run(
        self,
        *,
        principal_id: uuid.UUID,
        scope: str,
        key: str | None,
        request: Any,
        operation: Callable[[], Awaitable[T]],
        success_status: int = 200,
    ) -> Any:
        """Execute ``operation`` once per ``(principal_id, scope, key)``.

        ``request`` is fingerprinted so the same key replayed with a *different*
        body is rejected (422) rather than returning a stale answer for it. With
        ``key=None`` the operation simply runs (no idempotency).
        """
        if key is None:
            return await operation()

        fingerprint = self._fingerprint(request)
        record, is_new = await self._repo.claim(
            principal_id=principal_id, scope=scope, key=key, request_hash=fingerprint
        )

        if not is_new:
            if record.request_hash != fingerprint:
                raise ValidationError(
                    "Idempotency-Key was already used with a different request."
                )
            if record.status != IdempotencyStatus.COMPLETED:
                # The owner is still mid-flight (only observable across separate
                # transactions). Tell the client to retry rather than racing.
                raise ConflictError(
                    "A request with this Idempotency-Key is still being processed."
                )
            return (record.response_json or {}).get("data")

        result = await operation()
        payload = jsonable_encoder(result)
        await self._repo.complete(
            record, response_json={"data": payload}, response_status=success_status
        )
        return result

    @staticmethod
    def _fingerprint(request: Any) -> str:
        """sha256 of the canonical request body — stable across key orderings."""
        data = request.model_dump(mode="json") if isinstance(request, BaseModel) else (
            jsonable_encoder(request)
        )
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
