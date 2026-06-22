"""Org-settings schemas — the workspace identity / white-label config exposed to
the dashboard chrome. Read is public to signed-in users; update is Admin-only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.org_settings import OrgSettings


class OrgSettingsRead(BaseModel):
    name: str
    location: str
    product_name: str
    has_logo: bool

    @classmethod
    def from_model(cls, m: OrgSettings) -> OrgSettingsRead:
        return cls(
            name=m.name,
            location=m.location,
            product_name=m.product_name,
            has_logo=m.has_logo,
        )


class OrgSettingsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    product_name: str | None = Field(default=None, min_length=1, max_length=60)
