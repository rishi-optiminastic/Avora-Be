"""Biometric attendance ingest schemas (Golden rule #5).

The on-prem connector pushes raw device punches; the server re-derives everything
(employee match, local-day bucketing, in/out) and never trusts these as
privilege — only as attendance claims tied to an enrolled employee.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# "auto" lets the connector stay dumb: the server treats the earliest punch of a
# local day as clock-in and the latest as clock-out, regardless of declared dir.
PunchDirection = Literal["in", "out", "auto"]


class BiometricPunch(BaseModel):
    """One device punch. `external_id` matches an employee (biometric_id, else
    hr_external_id, else work_email)."""

    external_id: str = Field(min_length=1, max_length=320)
    punched_at: datetime
    direction: PunchDirection = "auto"


class BiometricPunchBatch(BaseModel):
    punches: list[BiometricPunch] = Field(min_length=1, max_length=5000)


class BiometricIngestResult(BaseModel):
    received: int
    matched: int  # punches that resolved to an employee
    sessions_upserted: int  # distinct employee-days written
    unmatched_external_ids: list[str]  # ids we couldn't map (flagged, not dropped)
