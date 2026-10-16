"""Remaining credit on the paid LLM API key, for the admin settings card.

Admin-only; the service answers 404 to anyone else so the endpoint does not
reveal that a paid key exists (§7). Never returns or logs the key itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, LlmCreditServiceDep
from app.schemas.llm_credit import LlmCreditRead

router = APIRouter(prefix="/llm-credit", tags=["llm-credit"])


@router.get("", response_model=LlmCreditRead)
async def get_llm_credit(
    caller: CurrentUserDep,
    service: LlmCreditServiceDep,
    refresh: Annotated[bool, Query()] = False,
) -> LlmCreditRead:
    """Remaining credit, usage and limit (USD) on the OpenRouter key, cached for
    ~10 minutes. `refresh=true` re-asks the provider (at most every 30s).
    `status` is `unknown` when the provider is unreachable and `unconfigured`
    when no key is set; neither is an error."""
    return await service.credit_for(caller, refresh=refresh)
