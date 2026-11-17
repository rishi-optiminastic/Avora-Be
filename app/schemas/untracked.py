"""What the untracked-work detector would raise, for a reviewer.

A preview response, never a write. It carries the EVIDENCE beside each
decision, because the question somebody is answering with this is "do I trust
it enough to switch it on", and that cannot be answered from a list of titles.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class FoundRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    entity_id: uuid.UUID
    entity_name: str
    #: How many separate samples named this entity.
    sightings: int
    #: The matcher's best confidence, 10-95. Anything under 45 never gets here.
    confidence: int
    #: The exact title the task would carry.
    title: str


class PreviewRead(BaseModel):
    #: Whether the sweep is actually switched on. A preview showing ten
    #: findings while this is false means ten tasks nobody is getting.
    enabled: bool
    people_considered: int
    would_create: int
    #: Candidates the per-person cap discarded, so the cap's cost is visible.
    held_back: int
    #: Why the catalog may be underperforming - unmatchable entities,
    #: departments matching nobody. The actionable half of this response.
    notes: list[str] = Field(default_factory=list)
    found: list[FoundRead] = Field(default_factory=list)
