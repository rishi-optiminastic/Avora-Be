"""Device request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ORMModel
from app.schemas.monitoring import CaptureWindowState


class DeviceCreate(BaseModel):
    """Admin/IT enrolls a device for an employee."""

    employee_id: uuid.UUID
    label: str = Field(min_length=1, max_length=256)


class DeviceSelfEnroll(BaseModel):
    """The agent self-enrolls the calling user's own machine on first launch.

    No employee_id — the device is always bound to the authenticated caller
    (Golden rule #2). hostname/os are optional hints used to build the label.
    """

    label: str | None = Field(default=None, max_length=256)
    hostname: str | None = Field(default=None, max_length=256)
    os: str | None = Field(default=None, max_length=128)


class DeviceCreated(BaseModel):
    """Returned on enrollment — carries the raw token, shown exactly once."""

    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    token: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Capture health - the agent's self-report (POST /devices/health).
# --------------------------------------------------------------------------- #
AgentOs = Literal["windows", "darwin", "linux"]
CaptureState = Literal["ok", "failing", "permission_denied", "unknown"]
WindowInfoState = Literal["ok", "empty", "permission_denied", "unknown"]
BrowserUrlState = Literal["ok", "empty", "permission_denied", "unsupported", "unknown"]
HealthStatus = Literal["healthy", "needs_attention", "no_report"]

# Upper bounds keep a hostile agent from writing absurd values (rule 5.4) and
# keep every number inside its column type.
MAX_QUEUE_ITEMS = 10_000_000
MAX_QUEUE_BYTES = 1 << 40  # 1 TiB
MAX_QUEUED_AGE_SECONDS = 366 * 24 * 3600
MAX_LAST_ERROR_CHARS = 300


def _clean_error(value: str | None) -> str | None:
    """Strip control characters and surrounding space; empty becomes None."""
    if value is None:
        return None
    printable = "".join(ch for ch in value if ch.isprintable())
    return printable.strip() or None


class DeviceHealthReport(BaseModel):
    """The agent's capture-health claim. Strict: unknown fields, coerced types
    and out-of-range numbers are all rejected (422)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_version: str = Field(min_length=1, max_length=32)
    os: AgentOs
    screen_capture: CaptureState
    window_info: WindowInfoState
    browser_url: BrowserUrlState
    last_error: str | None = Field(default=None, max_length=MAX_LAST_ERROR_CHARS)
    queue_items: int = Field(ge=0, le=MAX_QUEUE_ITEMS)
    queue_bytes: int = Field(ge=0, le=MAX_QUEUE_BYTES)
    oldest_queued_age_seconds: int | None = Field(default=None, ge=0, le=MAX_QUEUED_AGE_SECONDS)

    @field_validator("last_error")
    @classmethod
    def _sanitize_last_error(cls, value: str | None) -> str | None:
        return _clean_error(value)

    @field_validator("agent_version")
    @classmethod
    def _sanitize_version(cls, value: str) -> str:
        cleaned = _clean_error(value)
        if cleaned is None:
            raise ValueError("agent_version must not be blank")
        return cleaned


class DeviceHealthIssue(BaseModel):
    """One reason a device needs attention: a stable code plus plain words."""

    code: str
    summary: str


class DeviceHealthRead(BaseModel):
    """Latest capture health with the server's verdict. `last_error` is only
    filled for admins/IT; other in-scope readers get null."""

    status: HealthStatus
    issues: list[DeviceHealthIssue]
    agent_version: str | None
    os: str | None
    screen_capture: str | None
    window_info: str | None
    browser_url: str | None
    last_error: str | None
    queue_items: int | None
    queue_bytes: int | None
    oldest_queued_age_seconds: int | None
    reported_at: datetime | None
    attention_since: datetime | None


class DeviceRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    last_sequence: int
    is_revoked: bool
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    health: DeviceHealthRead | None = None
    # Whether anything this device sends is actually being STORED right now, and
    # why not. `last_seen_at` only says the agent is alive: a suppressed upload
    # still marks it alive, so without this an admin reads "reporting" off a
    # device whose every screenshot is being dropped. Filled by DeviceService.
    capture_window: CaptureWindowState | None = None


class DeviceReassign(BaseModel):
    """Admin/IT re-points a device at the employee who actually uses it."""

    employee_id: uuid.UUID
    # When the machine changed hands. Omitted ⇒ leave history alone and only fix
    # what the device captures from now on. Supplied ⇒ also re-file the rows
    # captured from that moment, which is what makes BOTH people's records true.
    history_from: datetime | None = Field(default=None)


class DeviceReassigned(ORMModel):
    """The updated device plus what the re-filing actually touched, so the UI
    can tell the admin exactly what changed rather than claiming success."""

    device: DeviceRead
    previous_employee_id: uuid.UUID
    screenshots_moved: int
    activity_samples_moved: int


class DeviceNudge(BaseModel):
    """Admin/manager nudges an employee to keep/reinstall the agent."""

    employee_id: uuid.UUID
    message: str | None = Field(default=None, max_length=280)


class DeviceNudgeResult(BaseModel):
    """Which channel the nudge went out on (agent on-screen vs notification+email)."""

    channel: Literal["agent", "notification_email"]


class FleetUpdateResult(BaseModel):
    """How many agents were signalled to self-update on demand."""

    updated: int
