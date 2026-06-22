"""Aggregate all v1 routers under a single APIRouter."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    activity,
    attendance,
    attribution,
    biometric,
    browsing,
    category_rules,
    compensation,
    dashboard,
    devices,
    documents,
    employees,
    eod,
    health,
    holidays,
    hr,
    insights,
    invitations,
    leaves,
    meetings,
    notifications,
    onboarding,
    org,
    payroll,
    pings,
    screenshots,
    targets,
    tasks,
    work_entities,
    workspace_files,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(employees.router)
api_router.include_router(activity.router)
api_router.include_router(hr.router)
api_router.include_router(invitations.router)
api_router.include_router(tasks.router)
api_router.include_router(targets.router)
api_router.include_router(leaves.router)
api_router.include_router(notifications.router)
api_router.include_router(onboarding.router)
api_router.include_router(org.router)
api_router.include_router(holidays.router)
api_router.include_router(devices.router)
api_router.include_router(attendance.router)
api_router.include_router(biometric.router)
api_router.include_router(meetings.router)
api_router.include_router(browsing.router)
api_router.include_router(screenshots.router)
api_router.include_router(pings.router)
api_router.include_router(insights.router)
api_router.include_router(work_entities.router)
api_router.include_router(attribution.router)
api_router.include_router(category_rules.router)
api_router.include_router(compensation.router)
api_router.include_router(payroll.router)
api_router.include_router(documents.router)
api_router.include_router(workspace_files.router)
api_router.include_router(dashboard.router)
api_router.include_router(eod.router)
