"""Onboarding configuration — the single org-wide setup checklist (a singleton).

One row, upserted by the service (mirrors `AttendancePolicy` / `PayrollSettings`).
Holds the welcome copy and the ordered list of onboarding steps every employee
sees in the setup dialog. Steps are stored as JSON so HR/Admin can add, remove
and reorder them freely; the schema layer validates each step's shape, icon and
tile against the allowed sets, and that step links stay internal.

Reads are open to any authenticated employee (the checklist is employee-facing);
only HR/Admin may edit it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# The default checklist seeded on first use — mirrors the original hard-coded
# steps. Kept in sync with the frontend fallback (`features/onboarding`).
DEFAULT_STEPS: list[dict[str, Any]] = [
    {
        "id": "agent",
        "title": "Install the desktop agent",
        "description": "Captures activity & attendance automatically — nothing to log by hand.",
        "action": "Install",
        "icon": "laptop",
        "tile": "mint",
        "required": True,
        "href": "/dashboard/download",
    },
    {
        "id": "profile",
        "title": "Complete your profile",
        "description": "Check your details, role, and the tabs scoped to you.",
        "action": "Open",
        "icon": "users",
        "tile": "violet",
        "required": False,
        "href": "/dashboard/me",
    },
    {
        "id": "org",
        "title": "Explore the org chart",
        "description": "See where you fit, your manager, and your team.",
        "action": "Open",
        "icon": "sitemap",
        "tile": "butter",
        "required": False,
        "href": "/dashboard/workforce/hierarchy",
    },
    {
        "id": "team",
        "title": "Browse the team directory",
        "description": "Meet everyone in the workspace and who does what.",
        "action": "Open",
        "icon": "building",
        "tile": "sky",
        "required": False,
        "href": "/dashboard/workforce/team",
    },
    {
        "id": "tasks",
        "title": "Review your tasks",
        "description": "See what is on your plate and what the team is shipping.",
        "action": "Open",
        "icon": "check",
        "tile": "peach",
        "required": False,
        "href": "/dashboard/goals/tasks",
    },
    {
        "id": "attendance",
        "title": "Check your attendance",
        "description": "Your clock-ins, hours, and the office-hours policy.",
        "action": "Open",
        "icon": "clock",
        "tile": "pink",
        "required": False,
        "href": "/dashboard/time/attendance",
    },
]


class OnboardingConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "onboarding_config"

    # Master switch — when off, the dialog and the reminder banner never show.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    eyebrow: Mapped[str] = mapped_column(String(120), default="Welcome to Avora")
    title: Mapped[str] = mapped_column(String(200), default="Let's get you set up")
    subtitle: Mapped[str] = mapped_column(
        String(400), default="A few quick steps to get the most out of your workspace."
    )
    # Ordered list of step dicts (shape validated in the schema layer).
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
