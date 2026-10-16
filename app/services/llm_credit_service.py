"""Remaining credit on the paid LLM key (OpenRouter), for the admin settings card.

External call -> service layer (Layering §4), over `httpx` like `LlmService`.
Reads two OpenRouter endpoints (https://openrouter.ai/docs/api-reference/limits):

- GET /key     -> data.usage, data.limit, data.limit_remaining (null = no cap on
                  the key) and data.effective_budget.remaining_usd.
- GET /credits -> data.total_credits, data.total_usage (account balance). This
                  one needs a management key; with a plain inference key it is
                  refused, which is fine - the figure just stays unknown.

The result is cached per process so an admin opening Settings never fans out
into a provider call each time, and a down provider or missing key reads as
"unknown"/"unconfigured" rather than an error. The key is sent only in the
Authorization header and is never logged or returned (Security 5.6).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.llm_credit import (
    LlmCreditRead,
    OpenRouterCreditsData,
    OpenRouterCreditsReply,
    OpenRouterKeyData,
    OpenRouterKeyReply,
)
from app.services.notification_service import NotificationService

logger = get_logger("app.llm_credit")

PROVIDER = "openrouter"
_TIMEOUT_SECONDS = 5.0
# A good answer is reused for 10 minutes; a failed one for 1, so a provider blip
# clears quickly without every page view retrying it.
_FRESH_SECONDS = 600.0
_FAILED_SECONDS = 60.0
# The card's refresh button bypasses the cache, but not more often than this.
_MIN_REFRESH_SECONDS = 30.0
# Unread low-balance alerts are not repeated within this window (covers a
# process restart forgetting the last state).
_LOW_ALERT_DEDUPE = timedelta(days=7)
_LOW_ALERT_ENTITY = "llm_credit"
_SETTINGS_LINK = "/dashboard/admin/settings"


@dataclass
class CreditCache:
    """One process-wide snapshot plus the last low/not-low state seen, so the
    admins are alerted when the balance FLIPS to low, not on every refresh."""

    snapshot: LlmCreditRead | None = None
    fetched_at: float = 0.0
    was_low: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def age(self) -> float:
        return time.monotonic() - self.fetched_at


_CACHE = CreditCache()


class LlmCreditService:
    def __init__(
        self,
        settings: Settings,
        employees: EmployeeRepository,
        notifications: NotificationService,
        *,
        cache: CreditCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._employees = employees
        self._notifications = notifications
        self._cache = cache or _CACHE
        self._transport = transport

    async def credit_for(self, caller: CurrentUser, *, refresh: bool = False) -> LlmCreditRead:
        """Admin-only (it_admin is normalized to admin). 404 for anyone else so
        the endpoint does not even reveal that a paid key exists (§7)."""
        if not caller.is_admin:
            raise NotFoundError()
        cache = self._cache
        async with cache.lock:
            cached = cache.snapshot
            if cached is not None and not _stale(cached, cache.age(), refresh=refresh):
                return cached
            snapshot = await self._fetch()
            cache.snapshot, cache.fetched_at = snapshot, time.monotonic()
            flipped_low = snapshot.low and not cache.was_low
            if snapshot.status == "ok":
                cache.was_low = snapshot.low
        if flipped_low:
            await self._alert_admins(caller, snapshot)
        return snapshot

    async def _fetch(self) -> LlmCreditRead:
        threshold = self._settings.llm_credit_low_threshold_usd
        checked_at = datetime.now(UTC)
        if not self._settings.openrouter_api_key:
            return LlmCreditRead(
                provider=PROVIDER,
                status="unconfigured",
                low_threshold=threshold,
                checked_at=checked_at,
            )
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as http:
            key = await self._get_key(http)
            if key is None:
                return LlmCreditRead(
                    provider=PROVIDER,
                    status="unknown",
                    low_threshold=threshold,
                    checked_at=checked_at,
                )
            account = await self._get_credits(http)
        remaining = _remaining(key, account)
        return LlmCreditRead(
            provider=PROVIDER,
            status="ok",
            remaining=remaining,
            usage=_usage(key, account),
            limit=_limit(key, account),
            low=remaining is not None and remaining < threshold,
            low_threshold=threshold,
            checked_at=checked_at,
        )

    async def _get_key(self, http: httpx.AsyncClient) -> OpenRouterKeyData | None:
        body = await self._get_json(http, "/key")
        if body is None:
            return None
        try:
            return OpenRouterKeyReply.model_validate(body).data
        except ValidationError:
            logger.warning("llm_credit_bad_reply", extra={"endpoint": "key"})
            return None

    async def _get_credits(self, http: httpx.AsyncClient) -> OpenRouterCreditsData | None:
        body = await self._get_json(http, "/credits")
        if body is None:
            return None
        try:
            return OpenRouterCreditsReply.model_validate(body).data
        except ValidationError:
            logger.warning("llm_credit_bad_reply", extra={"endpoint": "credits"})
            return None

    async def _get_json(self, http: httpx.AsyncClient, path: str) -> object | None:
        """GET one provider endpoint; None on any transport/status/JSON failure.
        Only the status code or error type is logged - never the key."""
        try:
            resp = await http.get(
                f"{self._settings.openrouter_base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {self._settings.openrouter_api_key}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "llm_credit_unreachable", extra={"endpoint": path, "error": type(exc).__name__}
            )
            return None
        if resp.status_code != httpx.codes.OK:
            logger.info("llm_credit_refused", extra={"endpoint": path, "status": resp.status_code})
            return None
        try:
            return resp.json()  # type: ignore[no-any-return]
        except ValueError:
            logger.warning("llm_credit_bad_reply", extra={"endpoint": path})
            return None

    async def _alert_admins(self, caller: CurrentUser, snapshot: LlmCreditRead) -> None:
        """Tell every admin once when the balance drops under the threshold. The
        admin whose page view noticed it is skipped - they are looking at it."""
        body = (
            f"About ${snapshot.remaining or 0.0:,.2f} of OpenRouter credit is left, below the "
            f"${snapshot.low_threshold:,.2f} threshold. EOD drafts stop when it runs out."
        )
        for admin in await self._employees.list_admins():
            await self._notifications.notify(
                recipient_id=admin.id,
                kind=NotificationKind.SYSTEM,
                level=NotificationLevel.WARNING,
                title="LLM API credit is running low",
                body=body,
                link=_SETTINGS_LINK,
                entity_type=_LOW_ALERT_ENTITY,
                actor_id=caller.employee_id,
                dedupe_within=_LOW_ALERT_DEDUPE,
            )


def _stale(snapshot: LlmCreditRead, age: float, *, refresh: bool) -> bool:
    if refresh:
        return age >= _MIN_REFRESH_SECONDS
    return age >= (_FRESH_SECONDS if snapshot.status == "ok" else _FAILED_SECONDS)


def _usage(key: OpenRouterKeyData, account: OpenRouterCreditsData | None) -> float | None:
    if key.usage is not None:
        return key.usage
    return account.total_usage if account is not None else None


def _remaining(key: OpenRouterKeyData, account: OpenRouterCreditsData | None) -> float | None:
    """The tightest known ceiling: the key's own cap, its effective budget, or
    the account balance - whichever runs out first. None when none is known."""
    known: list[float] = []
    if key.limit_remaining is not None:
        known.append(key.limit_remaining)
    if key.effective_budget is not None and key.effective_budget.remaining_usd is not None:
        known.append(key.effective_budget.remaining_usd)
    if account is not None and account.total_credits is not None:
        known.append(account.total_credits - (account.total_usage or 0.0))
    return round(min(known), 4) if known else None


def _limit(key: OpenRouterKeyData, account: OpenRouterCreditsData | None) -> float | None:
    if key.limit is not None:
        return key.limit
    if key.effective_budget is not None and key.effective_budget.limit_usd is not None:
        return key.effective_budget.limit_usd
    return account.total_credits if account is not None else None
