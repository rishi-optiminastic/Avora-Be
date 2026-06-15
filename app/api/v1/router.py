"""Aggregate all v1 routers under a single APIRouter."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    activity,
    attendance,
    devices,
    employees,
    health,
    holidays,
    hr,
    invitations,
    leaves,
    tasks,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(employees.router)
api_router.include_router(activity.router)
api_router.include_router(hr.router)
api_router.include_router(invitations.router)
api_router.include_router(tasks.router)
api_router.include_router(leaves.router)
api_router.include_router(holidays.router)
api_router.include_router(devices.router)
api_router.include_router(attendance.router)
