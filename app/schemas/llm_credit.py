"""LLM provider credit schemas.

`LlmCreditRead` is the admin-facing view. The `OpenRouter*` models parse the
provider's replies defensively: every field is optional and unknown fields are
ignored, so a changed or partial reply degrades to "unknown" instead of a 500.
The API key itself never appears in any of these shapes (Security 5.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# ok          - the provider answered (remaining may still be None: no cap set)
# unknown     - the provider could not be reached or answered with garbage
# unconfigured - no API key is set, so there is nothing to ask about
LlmCreditStatus = Literal["ok", "unknown", "unconfigured"]


class LlmCreditRead(BaseModel):
    provider: str
    status: LlmCreditStatus
    currency: Literal["USD"] = "USD"
    remaining: float | None = None
    usage: float | None = None
    limit: float | None = None
    low: bool = False
    low_threshold: float
    checked_at: datetime


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OpenRouterBudget(_Lenient):
    limit_usd: float | None = None
    spend_usd: float | None = None
    remaining_usd: float | None = None


class OpenRouterKeyData(_Lenient):
    """`data` of GET /api/v1/key. `limit`/`limit_remaining` are null when the
    key has no credit cap of its own."""

    usage: float | None = None
    limit: float | None = None
    limit_remaining: float | None = None
    effective_budget: OpenRouterBudget | None = None


class OpenRouterKeyReply(_Lenient):
    data: OpenRouterKeyData | None = None


class OpenRouterCreditsData(_Lenient):
    """`data` of GET /api/v1/credits (account-wide purchased vs used)."""

    total_credits: float | None = None
    total_usage: float | None = None


class OpenRouterCreditsReply(_Lenient):
    data: OpenRouterCreditsData | None = None
