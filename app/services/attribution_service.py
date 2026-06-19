"""Work-attribution derivation — infer each person's current work entity.

For everyone in the caller's scope (Security rule 5.3), build a "session bag"
from their latest activity (active window + current domain) plus their most
recent OCR'd screenshot text, then match it against the active catalog. The
result is recomputable from raw data and never stored as ground truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.work_signals import EntityProfile, attribute, build_profile
from app.repositories.activity import ActivityRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.attribution import AttributionRead
from app.schemas.auth import CurrentUser

WINDOW_MINUTES = 30  # how far back to gather a person's recent on-screen text


class AttributionService:
    def __init__(
        self,
        activity: ActivityRepository,
        screenshots: ScreenshotRepository,
        entities: WorkEntityRepository,
        employees: EmployeeRepository,
    ) -> None:
        self._activity = activity
        self._screenshots = screenshots
        self._entities = entities
        self._employees = employees

    async def live(self, caller: CurrentUser, now: datetime) -> list[AttributionRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        since = now - timedelta(minutes=WINDOW_MINUTES)

        latest = await self._activity.latest_since(ids, since)
        ocr = await self._screenshots.latest_ocr_text(ids, since)
        profiles: list[EntityProfile] = [
            build_profile(e.id, e.name, list(e.keywords), list(e.domains))
            for e in await self._entities.list_active()
        ]

        result: list[AttributionRead] = []
        for emp in employees:
            sample = latest.get(emp.id)
            parts: list[str] = []
            domain: str | None = None
            if sample is not None:
                if sample.active_window:
                    parts.append(sample.active_window)
                domain = sample.domain
                if domain:
                    parts.append(domain)
            if emp.id in ocr:
                parts.append(ocr[emp.id])

            match = attribute(" ".join(parts), domain, profiles) if (parts and profiles) else None
            result.append(
                AttributionRead(
                    employee_id=emp.id,
                    entity_id=match.entity_id if match else None,
                    entity_name=match.name if match else None,
                    confidence=match.confidence if match else 0,
                    matched=match.matched if match else [],
                )
            )
        return result
