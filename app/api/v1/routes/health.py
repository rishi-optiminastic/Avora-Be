"""Liveness / readiness probes. No auth, no sensitive data."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core.deps import DbDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness: the process is up."""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
async def readyz(db: DbDep) -> HealthResponse:
    """Readiness: we can reach the database."""
    await db.execute(text("SELECT 1"))
    return HealthResponse(status="ready")
