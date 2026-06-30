"""Agent ingest business rules — the hostile-input path (Security rule 5.4).

Responsibilities:
  * Replay protection: reject reused or out-of-order per-device sequences.
  * Server-side receive timestamp: never order/trust by the agent clock.
  * Anomaly flagging: record (never silently drop) skew and impossible values.
HMAC verification and rate limiting happen at the edge (deps/route) before we
get here; this layer assumes the device is authenticated but its DATA is not.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.categories import extract_domain
from app.core.config import Settings
from app.core.exceptions import ReplayError
from app.models.activity import ActivitySample
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.device import DeviceRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.activity import ActivityIngest
from app.schemas.auth import CurrentDevice
from app.services.monitoring_gate import MonitoringGateService


class ActivityService:
    def __init__(
        self,
        settings: Settings,
        devices: DeviceRepository,
        activity: ActivityRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        gate: MonitoringGateService,
    ) -> None:
        self._settings = settings
        self._devices = devices
        self._activity = activity
        self._employees = employees
        self._audit = audit
        self._gate = gate

    async def _advance_device(self, device: CurrentDevice, seq: int, at: datetime) -> None:
        """Keep the per-device sequence monotonic + mark it alive (also after a
        dropped sample, so replay protection holds and the device still looks up)."""
        db_device = await self._devices.get(device.device_id)
        if db_device is not None:
            await self._devices.advance_sequence(db_device, seq)
            await self._devices.touch_last_seen(db_device, at)

    def _flags(self, payload: ActivityIngest, received_at: datetime) -> list[str]:
        flags: list[str] = []
        skew = abs((received_at - payload.client_timestamp).total_seconds())
        if skew > self._settings.agent_max_clock_skew_seconds:
            flags.append("clock_skew")
        if payload.client_timestamp > received_at:
            flags.append("future_timestamp")
        return flags

    async def ingest(self, device: CurrentDevice, payload: ActivityIngest) -> ActivitySample | None:
        # Capture is always on (work mode) — the personal-mode pause was removed.
        # Replay / ordering: a sequence we've already seen, or one that goes
        # backwards, is rejected. Dedup is also enforced by the DB unique
        # constraint on (device_id, sequence) as a backstop.
        if payload.sequence <= device.last_sequence:
            await self._audit.append(
                actor=f"agent:{device.device_id}",
                action="activity.replay_rejected",
                target=f"seq:{payload.sequence}",
            )
            raise ReplayError()

        received_at = datetime.now(UTC)

        # Privacy: stop storing activity once the employee has deliberately checked
        # out for the day (until they clock back in). The device may keep sending;
        # we advance its sequence + mark it alive, but store nothing. WFH/agent-only
        # employees (no formal checkout) are never suppressed. See MonitoringGate.
        if await self._gate.is_checked_out(device.employee_id):
            await self._advance_device(device, payload.sequence, received_at)
            return None

        flags = self._flags(payload, received_at)
        sample = await self._activity.add_sample(
            device_id=device.device_id,
            employee_id=device.employee_id,
            sequence=payload.sequence,
            client_timestamp=payload.client_timestamp,
            active_window=payload.active_window,
            idle_seconds=payload.idle_seconds,
            url=payload.url,
            domain=extract_domain(payload.url),
            page_title=payload.page_title,
            browser=payload.browser,
            flags=flags,
        )

        # Advance the high-water mark + mark the device seen, only after insert.
        await self._advance_device(device, payload.sequence, received_at)
        return sample
