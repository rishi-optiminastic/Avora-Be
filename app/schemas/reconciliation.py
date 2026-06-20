"""Attendance reconciliation schemas — biometric vs laptop-agent, per day."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# match       — both sources present and login/logout agree within tolerance
# no_punch    — laptop activity but no biometric punch (worked without punching)
# no_activity — biometric punch but no laptop activity (punched, not on laptop)
# time_gap    — both present but login/logout differ beyond tolerance
ReconStatus = Literal["match", "no_punch", "no_activity", "time_gap"]


class ReconciliationRow(BaseModel):
    employee_id: uuid.UUID
    name: str
    department: str | None
    biometric_login: datetime | None
    biometric_logout: datetime | None
    biometric_worked_minutes: int | None
    agent_login: datetime | None
    agent_logout: datetime | None
    agent_worked_minutes: int | None
    status: ReconStatus
    flags: list[str]


class ReconciliationReport(BaseModel):
    date: str  # local office date (YYYY-MM-DD)
    timezone: str
    tolerance_minutes: int
    employees: int
    matched: int
    discrepancies: int
    rows: list[ReconciliationRow]
