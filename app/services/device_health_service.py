"""Device capture health: store the agent's report, alert on incidents.

The agent POSTs /devices/health every few minutes and on change. We keep the
latest report on the device row, derive "needs attention" with the pure rules in
`device_health_rules`, and alert admins/IT once per incident:

  healthy -> needs attention   open the incident (atomic), alert admins/IT and
                               give the owner a plain-language fix hint
  needs attention -> healthy   close the incident, one quiet note to admins

A stale report (agent alive, health loop silent) cannot be seen on a report, so
the activity ingest path calls `check_stale` after it marks the device alive.
`FLAP_GUARD` additionally suppresses a repeat while the previous alert is still
unread, so a device flapping every few minutes cannot flood anyone's inbox.

Never log `last_error`: it is agent-supplied free text (Security rule 5.6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.models.device import Device
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.device import DeviceRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentDevice, CurrentUser
from app.schemas.device import DeviceHealthIssue, DeviceHealthRead, DeviceHealthReport
from app.services.device_health_rules import (
    HealthAssessment,
    assess,
    is_report_stale,
    owner_fix_hint,
)
from app.services.notification_service import NotificationService

logger = get_logger("app.device_health")

FLAP_GUARD = timedelta(hours=6)
ADMIN_DEVICES_LINK = "/dashboard/admin/devices"
_ALERT_ENTITY = "device"
_RECOVERY_ENTITY = "device_recovered"
_TITLE_MAX = 160
_BODY_MAX = 500


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_health_read(device: Device, now: datetime, *, include_error: bool) -> DeviceHealthRead:
    """The read shape for one device. `include_error` gates the agent's free-text
    error to admins/IT: it is diagnostic detail, not something every in-scope
    reader needs."""
    verdict = assess(device, now)
    return DeviceHealthRead(
        status=verdict.status,
        issues=[DeviceHealthIssue(code=i.code, summary=i.summary) for i in verdict.issues],
        agent_version=device.health_agent_version,
        os=device.health_os,
        screen_capture=device.health_screen_capture,
        window_info=device.health_window_info,
        browser_url=device.health_browser_url,
        last_error=device.health_last_error if include_error else None,
        queue_items=device.health_queue_items,
        queue_bytes=device.health_queue_bytes,
        oldest_queued_age_seconds=device.health_oldest_queued_age_seconds,
        reported_at=device.health_reported_at,
        attention_since=device.health_attention_since,
    )


def can_see_health_errors(caller: CurrentUser) -> bool:
    # IT_ADMIN is normalized to ADMIN on the caller; both listed for clarity.
    return caller.role in (Role.ADMIN, Role.IT_ADMIN)


class DeviceHealthService:
    def __init__(
        self,
        devices: DeviceRepository,
        employees: EmployeeRepository,
        notifications: NotificationService,
    ) -> None:
        self._devices = devices
        self._employees = employees
        self._notifications = notifications

    async def record_report(self, device: CurrentDevice, report: DeviceHealthReport) -> None:
        """Store the report (stamped with server time) and act on a transition."""
        row = await self._devices.get(device.device_id)
        if row is None or row.is_revoked:
            raise AuthenticationError()
        now = datetime.now(UTC)
        await self._devices.save_health(row, report, now)
        verdict = assess(row, now)
        if verdict.needs_attention:
            await self._open_incident(row, verdict, now)
        else:
            await self._close_incident(row)

    async def check_stale(self, device: Device, now: datetime) -> None:
        """Called on every activity ingest after `last_seen_at` moved. Cheap in
        the common case: two attribute reads, no query."""
        if device.health_attention_since is not None or not is_report_stale(device, now):
            return
        await self._open_incident(device, assess(device, now), now)

    async def _open_incident(
        self, device: Device, verdict: HealthAssessment, now: datetime
    ) -> None:
        if not await self._devices.open_health_incident(device.id, now):
            return  # already open: one alert per incident
        codes = [issue.code for issue in verdict.issues]
        logger.info("device_health_attention", extra={"device_id": str(device.id), "issues": codes})
        owner_name = await self._owner_name(device)
        title = _clip(f"{owner_name}'s {device.label} needs attention", _TITLE_MAX)
        body = _clip("; ".join(issue.summary for issue in verdict.issues) + ".", _BODY_MAX)
        await self._notify_admins(
            device, title=title, body=body, level=NotificationLevel.WARNING, entity=_ALERT_ENTITY
        )
        await self._notifications.notify(
            recipient_id=device.employee_id,
            kind=NotificationKind.DEVICE_HEALTH,
            level=NotificationLevel.WARNING,
            title="Avora needs a quick fix on your laptop",
            body=owner_fix_hint(verdict.issues),
            entity_type=_ALERT_ENTITY,
            entity_id=device.id,
            dedupe_within=FLAP_GUARD,
        )

    async def _close_incident(self, device: Device) -> None:
        if not await self._devices.close_health_incident(device.id):
            return
        logger.info("device_health_recovered", extra={"device_id": str(device.id)})
        owner_name = await self._owner_name(device)
        await self._notify_admins(
            device,
            title=_clip(f"{owner_name}'s {device.label} is capturing again", _TITLE_MAX),
            body=None,
            level=NotificationLevel.INFO,
            entity=_RECOVERY_ENTITY,
        )

    async def _notify_admins(
        self,
        device: Device,
        *,
        title: str,
        body: str | None,
        level: NotificationLevel,
        entity: str,
    ) -> None:
        for admin in await self._employees.list_admins():
            await self._notifications.notify(
                recipient_id=admin.id,
                kind=NotificationKind.DEVICE_HEALTH,
                level=level,
                title=title,
                body=body,
                link=ADMIN_DEVICES_LINK,
                entity_type=entity,
                entity_id=device.id,
                dedupe_within=FLAP_GUARD,
            )

    async def _owner_name(self, device: Device) -> str:
        owner = await self._employees.get(device.employee_id)
        return owner.full_name if owner is not None else "An employee"
