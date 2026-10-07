"""Avora Circle scheduler - copies employees' Circle documents in, and fills
the date of birth, gender and profile photo Avora is missing.

Runs as its own process (like `worker/celebrations_scheduler.py`). Each tick it
walks the active employees and copies any Circle document not copied before
(see `CircleDocumentSync`). Each file is committed on its own, so one bad
file or a Circle hiccup never loses the rest of the run. Idempotent by design:
the copy ledger means extra ticks and restarts never duplicate a file.

Does nothing until the Circle link is configured. Env:
  DATABASE_URL                     Postgres URL (asyncpg-style, same as the API).
  CIRCLE_API_URL / CIRCLE_API_SECRET  the Circle link (both required).
  AWS_*                            so copies land in S3 like uploads do.
  CIRCLE_DOCUMENTS_TICK_SECONDS    seconds between runs (default 600 = 10 min).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.audit import AuditRepository
from app.repositories.circle_document_import import CircleDocumentImportRepository
from app.repositories.document import DocumentRepository
from app.repositories.employee import EmployeeRepository
from app.services.circle_client import CircleClient
from app.services.circle_document_sync import CircleDocumentSync, discard_stored
from app.services.circle_profile_sync import CircleProfileSync
from worker.heartbeat import beat

log = logging.getLogger("circle_documents_scheduler")

TICK_SECONDS = float(os.getenv("CIRCLE_DOCUMENTS_TICK_SECONDS", "600"))
HEARTBEAT_ENV = "HEARTBEAT_URL_CIRCLE_DOCUMENTS"


def _build_profile_sync(session: AsyncSession) -> CircleProfileSync:
    settings = get_settings()
    return CircleProfileSync(
        EmployeeRepository(session),
        CircleDocumentImportRepository(session),
        AuditRepository(session),
        settings,
        CircleClient(settings),
    )


def _build_service(session: AsyncSession) -> CircleDocumentSync:
    settings = get_settings()
    return CircleDocumentSync(
        DocumentRepository(session),
        CircleDocumentImportRepository(session),
        AuditRepository(session),
        settings,
        CircleClient(settings),
    )


async def _pending_for(employee_id: uuid.UUID, work_email: str) -> list[dict[str, Any]]:
    async with SessionFactory() as session:
        return await _build_service(session).pending(employee_id, work_email)


async def _copy_one(employee_id: uuid.UUID, work_email: str, entry: dict[str, Any]) -> bool:
    """One document in its own transaction, so a failure loses only this file."""
    async with SessionFactory() as session:
        outcome = await _build_service(session).copy_one(employee_id, work_email, entry)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            # Stored, but the record never committed: don't leave the bytes.
            await discard_stored(outcome.object_key)
            raise
    return outcome.copied


async def copy_for_employee(employee_id: uuid.UUID, work_email: str) -> int:
    """Copy this person's pending documents; a failing file is logged and
    retried next run without blocking the others."""
    copied = 0
    for entry in await _pending_for(employee_id, work_email):
        try:
            copied += await _copy_one(employee_id, work_email, entry)
        except Exception as exc:
            log.warning("copy failed for one document: %s", type(exc).__name__)
    return copied


async def fill_profile_for(employee_id: uuid.UUID) -> list[str]:
    """Date of birth, gender and photo Avora is missing - in its own transaction."""
    async with SessionFactory() as session:
        employee = await EmployeeRepository(session).get(employee_id)
        if employee is None:
            return []
        try:
            filled = await _build_profile_sync(session).fill(employee)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return filled


async def run_once() -> int:
    """One pass over every active employee: copy new documents, then fill
    missing profile details. Returns how many files were copied."""
    async with SessionFactory() as session:
        people = [(e.id, e.work_email) for e in await EmployeeRepository(session).list_all_active()]
    copied = 0
    for employee_id, work_email in people:
        try:
            copied += await copy_for_employee(employee_id, work_email)
        except Exception as exc:  # e.g. Circle down for this person's listing
            log.warning("copy failed for one employee: %s", type(exc).__name__)
        try:
            await fill_profile_for(employee_id)
        except Exception as exc:
            log.warning("profile fill failed for one employee: %s", type(exc).__name__)
    return copied


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not get_settings().circle_configured:
        log.info("Circle link not configured (CIRCLE_API_URL / CIRCLE_API_SECRET); idle.")
    log.info("Avora Circle documents scheduler starting (tick=%.0fs)", TICK_SECONDS)
    try:
        while True:
            try:
                if get_settings().circle_configured:
                    copied = await run_once()
                    if copied:
                        log.info("copied %d document(s) from Circle", copied)
                await beat(HEARTBEAT_ENV)
            except Exception as exc:  # keep the loop alive across transient failures
                # Type only: driver errors can echo bound values (work emails).
                log.warning("tick failed: %s", type(exc).__name__)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
