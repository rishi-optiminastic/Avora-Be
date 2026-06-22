"""Onboarding config request/response schemas (Golden rule #5).

A full-replace `Update` (HR/Admin) and a `Read` (any employee). Step icons and
tiles are validated against the same allowed sets the frontend renders, step ids
must be unique, and links must stay internal (start with `/`) so a customised
step can never be turned into an open redirect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.onboarding_config import OnboardingConfig

# Allowed icon keys — must match the frontend icon registry.
IconKey = Literal[
    "laptop",
    "users",
    "sitemap",
    "building",
    "check",
    "clock",
    "doc",
    "calendar",
    "chat",
    "download",
    "spark",
    "search",
]

# Allowed colour-tile keys — must match the frontend tile palette.
TileKey = Literal["mint", "sage", "butter", "pink", "sky", "violet", "peach"]

_MAX_STEPS = 12


class OnboardingStepSchema(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=240)
    action: str = Field(default="Open", min_length=1, max_length=24)
    icon: IconKey = "check"
    tile: TileKey = "violet"
    required: bool = False
    href: str = Field(default="", max_length=200)

    @field_validator("href")
    @classmethod
    def _internal_href(cls, value: str) -> str:
        """Links must be internal routes (or empty) — never external/protocol."""
        value = value.strip()
        if value and not value.startswith("/"):
            raise ValueError("Step link must be an internal route starting with '/'.")
        return value


class OnboardingConfigUpdate(BaseModel):
    """Full replace of the org onboarding checklist (HR/Admin only)."""

    enabled: bool = True
    eyebrow: str = Field(default="Welcome to Avora", max_length=120)
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=400)
    steps: list[OnboardingStepSchema] = Field(default_factory=list, max_length=_MAX_STEPS)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> OnboardingConfigUpdate:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Step ids must be unique.")
        return self


class OnboardingConfigRead(BaseModel):
    enabled: bool
    eyebrow: str
    title: str
    subtitle: str
    steps: list[OnboardingStepSchema]
    updated_at: datetime

    @classmethod
    def from_model(cls, model: OnboardingConfig) -> OnboardingConfigRead:
        return cls(
            enabled=model.enabled,
            eyebrow=model.eyebrow,
            title=model.title,
            subtitle=model.subtitle,
            steps=[OnboardingStepSchema.model_validate(step) for step in model.steps],
            updated_at=model.updated_at,
        )
