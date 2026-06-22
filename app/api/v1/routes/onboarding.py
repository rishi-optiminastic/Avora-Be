"""Onboarding-config endpoints.

`GET` is open to any authenticated employee (the setup checklist is their own
screen). `PUT` is a full replace, restricted to HR/Admin inside the service.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, OnboardingServiceDep
from app.schemas.onboarding import OnboardingConfigRead, OnboardingConfigUpdate

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/config", response_model=OnboardingConfigRead)
async def get_onboarding_config(
    caller: CurrentUserDep, service: OnboardingServiceDep
) -> OnboardingConfigRead:
    return await service.get_config(caller)


@router.put("/config", response_model=OnboardingConfigRead)
async def update_onboarding_config(
    payload: OnboardingConfigUpdate, caller: CurrentUserDep, service: OnboardingServiceDep
) -> OnboardingConfigRead:
    return await service.update_config(caller, payload)
