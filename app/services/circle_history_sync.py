"""Mirror the person's appraisal history and assets from Circle.

Circle wins: the rows here are made to match Circle's list, matched by Circle's
id (new ones added, changed ones updated, ones Circle dropped removed). An
EMPTY list from Circle never wipes what Avora has - it keeps the rows and logs
it, since "nothing" is as likely a Circle hiccup as a real clear-out.

Every applied change is audited as `circle-sync`. Run per employee by
`worker/circle_documents_scheduler.py`; the caller commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.circle_record import LONG_TEXT_LENGTH, SHORT_TEXT_LENGTH
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.circle_record import CircleRecordRepository, RecordValues
from app.services.circle_client import CircleClient
from app.services.circle_document_sync import SYNC_ACTOR
from app.services.circle_values import clipped, parse_date, score

logger = get_logger("app.circle_history")


def appraisal_values(entry: dict[str, Any]) -> RecordValues:
    return {
        "period": clipped(entry.get("period"), SHORT_TEXT_LENGTH),
        "status": clipped(entry.get("status"), SHORT_TEXT_LENGTH),
        "current_salary": clipped(entry.get("current_salary"), SHORT_TEXT_LENGTH),
        "revised_salary": clipped(entry.get("revised_salary"), SHORT_TEXT_LENGTH),
        "effective_date": parse_date(entry.get("effective_date")),
        "score": score(entry.get("score")),
    }


def asset_values(entry: dict[str, Any]) -> RecordValues:
    return {
        "name": clipped(entry.get("name"), LONG_TEXT_LENGTH),
        "asset_type": clipped(entry.get("type"), SHORT_TEXT_LENGTH),
        "serial_number": clipped(entry.get("serial_number"), LONG_TEXT_LENGTH),
        "status": clipped(entry.get("status"), SHORT_TEXT_LENGTH),
        "assigned_date": parse_date(entry.get("assigned_date")),
    }


def keyed(
    entries: list[dict[str, Any]], to_values: Callable[[dict[str, Any]], RecordValues]
) -> dict[str, RecordValues]:
    """Circle's entries by Circle id; one without a usable id cannot be
    tracked across syncs, so it is left out."""
    out: dict[str, RecordValues] = {}
    for entry in entries:
        circle_id = clipped(entry.get("id"), SHORT_TEXT_LENGTH)
        if circle_id:
            out[circle_id] = to_values(entry)
    return out


@dataclass(frozen=True)
class _MirroredList:
    """One list Circle owns: where it comes from and where it lands."""

    kind: str  # "appraisals" / "assets" - also the audit action prefix
    fetch: Callable[[str], Awaitable[list[dict[str, Any]]]]
    to_values: Callable[[dict[str, Any]], RecordValues]
    replace: Callable[[uuid.UUID, Mapping[str, RecordValues]], Awaitable[bool]]
    existing: Callable[[uuid.UUID], Awaitable[Sequence[object]]]


class CircleHistorySync:
    def __init__(
        self, records: CircleRecordRepository, audit: AuditRepository, client: CircleClient
    ) -> None:
        self._audit = audit
        self._lists = (
            _MirroredList(
                "appraisals",
                client.appraisals,
                appraisal_values,
                records.replace_appraisals,
                records.list_appraisals,
            ),
            _MirroredList(
                "assets", client.assets, asset_values, records.replace_assets, records.list_assets
            ),
        )

    async def sync(self, employee: Employee) -> list[str]:
        """Returns which lists changed ("appraisals", "assets")."""
        return [spec.kind for spec in self._lists if await self._sync_list(employee, spec)]

    async def _sync_list(self, employee: Employee, spec: _MirroredList) -> bool:
        try:
            entries = keyed(await spec.fetch(employee.work_email), spec.to_values)
        except NotFoundError:
            return False
        if not entries:
            if await spec.existing(employee.id):
                logger.info(
                    "circle_history_kept", extra={"employee": str(employee.id), "kind": spec.kind}
                )
            return False
        if not await spec.replace(employee.id, entries):
            return False
        await self._audit.append(
            actor=SYNC_ACTOR, action=f"{spec.kind}.circle_sync", target=f"employee:{employee.id}"
        )
        return True
