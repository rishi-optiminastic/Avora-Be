"""Onboarding configuration — business logic.

Reads are open to any authenticated employee (the checklist is employee-facing).
Edits are HR/Admin only and audited. The singleton is materialised with the
default checklist on first access.
"""

from __future__ import annotations

from app.core.exceptions import AuthorizationError
from app.models.employee import Role
from app.models.onboarding_config import OnboardingConfig
from app.repositories.audit import AuditRepository
from app.repositories.onboarding_config import OnboardingConfigRepository
from app.schemas.auth import CurrentUser
from app.schemas.onboarding import OnboardingConfigRead, OnboardingConfigUpdate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class OnboardingService:
    def __init__(self, config_repo: OnboardingConfigRepository, audit: AuditRepository) -> None:
        self._config_repo = config_repo
        self._audit = audit

    async def get_config_model(self) -> OnboardingConfig:
        return await self._config_repo.get() or await self._config_repo.create_default()

    async def get_config(self, caller: CurrentUser) -> OnboardingConfigRead:
        # Readable by any authenticated employee — the checklist is their screen.
        del caller
        return OnboardingConfigRead.from_model(await self.get_config_model())

    async def update_config(
        self, caller: CurrentUser, payload: OnboardingConfigUpdate
    ) -> OnboardingConfigRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        config = await self.get_config_model()
        config.enabled = payload.enabled
        config.eyebrow = payload.eyebrow
        config.title = payload.title
        config.subtitle = payload.subtitle
        config.steps = [step.model_dump() for step in payload.steps]
        config.updated_by = caller.employee_id
        await self._config_repo.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="onboarding.config.update",
            target="onboarding",
        )
        return OnboardingConfigRead.from_model(config)
