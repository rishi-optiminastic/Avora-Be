"""Biometric attendance — HMAC ingest, employee mapping, idempotency, and the
reconciliation read. Mirrors the HR-webhook trust model (CLAUDE §5.1/§9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import compute_hmac_sha256
from app.models.work_session import WorkSession
from tests.conftest import _Seed, auth_headers


def _signed(settings: Settings, body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    sig = compute_hmac_sha256(settings.biometric_webhook_secret, raw)
    return raw, {"X-Biometric-Signature": sig, "Content-Type": "application/json"}


def _batch(external_id: str) -> dict[str, object]:
    return {
        "punches": [
            {"external_id": external_id, "punched_at": "2026-06-15T09:05:00+05:30"},
            {"external_id": external_id, "punched_at": "2026-06-15T18:10:00+05:30"},
        ]
    }


# ---- HMAC trust ------------------------------------------------------------ #


async def test_unsigned_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/attendance/biometric", json=_batch("1042"))
    assert resp.status_code == 401


async def test_bad_signature_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, _ = _signed(settings, _batch("1042"))
    resp = await client.post(
        "/api/v1/attendance/biometric",
        content=raw,
        headers={"X-Biometric-Signature": "deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# ---- ingest + mapping ------------------------------------------------------ #


async def test_ingest_by_biometric_id_creates_session(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    seed.report.biometric_id = "1042"
    await db.commit()

    raw, headers = _signed(settings, _batch("1042"))
    resp = await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] == 2
    assert body["sessions_upserted"] == 1
    assert body["unmatched_external_ids"] == []

    # The biometric punch became the day's attendance (worked ~9h).
    day = await client.get(
        "/api/v1/attendance?date=2026-06-15", headers=auth_headers(settings, seed.admin)
    )
    row = next(r for r in day.json() if r["employee_id"] == str(seed.report.id))
    assert row["login_at"] is not None
    assert row["worked_minutes"] > 480


async def test_ingest_maps_by_email_then_flags_unmatched(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # report has no biometric_id set → match on work_email, plus one bogus id.
    body = {
        "punches": [
            {"external_id": seed.report.work_email, "punched_at": "2026-06-15T09:00:00+05:30"},
            {"external_id": "9999", "punched_at": "2026-06-15T09:00:00+05:30"},
        ]
    }
    raw, headers = _signed(settings, body)
    resp = await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)
    assert resp.status_code == 200
    out = resp.json()
    assert out["matched"] == 1
    assert out["unmatched_external_ids"] == ["9999"]


async def test_ingest_is_idempotent(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    seed.report.biometric_id = "1042"
    await db.commit()

    raw, headers = _signed(settings, _batch("1042"))
    await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)
    raw2, headers2 = _signed(settings, _batch("1042"))
    await client.post("/api/v1/attendance/biometric", content=raw2, headers=headers2)

    count = await db.scalar(
        select(func.count())
        .select_from(WorkSession)
        .where(WorkSession.employee_id == seed.report.id, WorkSession.source == "biometric")
    )
    assert count == 1  # merged into one day session, not duplicated


async def test_in_punches_keep_day_open_then_out_closes(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    seed.report.biometric_id = "1042"
    await db.commit()
    rid = seed.report.id

    async def _clock_out() -> object:
        db.expire_all()  # avoid the identity-map cache; read the DB value
        return await db.scalar(
            select(WorkSession.clock_out_at).where(
                WorkSession.employee_id == rid, WorkSession.source == "biometric"
            )
        )

    # Two morning IN scans, no OUT yet → the day stays OPEN (not closed at 09:05),
    # so the person isn't scored half-day / early-out while still working.
    ins = {
        "punches": [
            {"external_id": "1042", "punched_at": "2026-06-15T09:00:00+05:30", "direction": "in"},
            {"external_id": "1042", "punched_at": "2026-06-15T09:05:00+05:30", "direction": "in"},
        ]
    }
    raw, headers = _signed(settings, ins)
    r1 = await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)
    assert r1.status_code == 200
    sessions = await db.scalar(
        select(func.count())
        .select_from(WorkSession)
        .where(WorkSession.employee_id == seed.report.id, WorkSession.source == "biometric")
    )
    assert sessions == 1
    assert await _clock_out() is None  # open

    # The evening OUT punch (sent on its own, like an incremental sync) closes it.
    out = {
        "punches": [
            {"external_id": "1042", "punched_at": "2026-06-15T18:00:00+05:30", "direction": "out"},
        ]
    }
    raw2, headers2 = _signed(settings, out)
    r2 = await client.post("/api/v1/attendance/biometric", content=raw2, headers=headers2)
    assert r2.status_code == 200
    assert await _clock_out() is not None  # closed at the out punch


async def test_me_today_reflects_biometric_checkin(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    seed.report.biometric_id = "1042"
    await db.commit()
    me = auth_headers(settings, seed.report)

    # No punch today → the navbar timer endpoint returns null.
    r0 = await client.get("/api/v1/attendance/me/today", headers=me)
    assert r0.status_code == 200
    assert r0.json() is None

    # An IN punch right now → open session, timer running (no clock-out).
    body = {
        "punches": [
            {"external_id": "1042", "punched_at": datetime.now(UTC).isoformat(), "direction": "in"}
        ]
    }
    raw, headers = _signed(settings, body)
    post = await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)
    assert post.status_code == 200

    r1 = await client.get("/api/v1/attendance/me/today", headers=me)
    assert r1.status_code == 200
    today = r1.json()
    assert today is not None
    assert today["is_open"] is True
    assert today["clock_out_at"] is None


# ---- reconciliation -------------------------------------------------------- #


async def test_reconciliation_flags_punch_without_activity(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    seed.report.biometric_id = "1042"
    await db.commit()
    raw, headers = _signed(settings, _batch("1042"))
    await client.post("/api/v1/attendance/biometric", content=raw, headers=headers)

    resp = await client.get(
        "/api/v1/attendance/reconciliation?date=2026-06-15",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    report = resp.json()
    row = next(r for r in report["rows"] if r["employee_id"] == str(seed.report.id))
    # Punched in, but no laptop activity seeded → flagged, not silently merged.
    assert row["status"] == "no_activity"
    assert row["biometric_login"] is not None
    assert row["agent_login"] is None


async def test_reconciliation_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.get("/api/v1/attendance/reconciliation?date=2026-06-15")
    assert resp.status_code == 401
