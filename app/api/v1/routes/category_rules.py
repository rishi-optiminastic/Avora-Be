"""Category-rule endpoints — admin (any) + dept managers (own department)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, status

from app.core.deps import (
    CategoryRuleServiceDep,
    CurrentUserDep,
    IdempotencyKeyHeader,
    IdempotencyServiceDep,
)
from app.schemas.category_rule import CategoryRuleCreate, CategoryRuleRead, CategoryRuleUpdate

router = APIRouter(prefix="/category-rules", tags=["category-rules"])


@router.get("", response_model=list[CategoryRuleRead])
async def list_category_rules(
    caller: CurrentUserDep,
    service: CategoryRuleServiceDep,
) -> list[CategoryRuleRead]:
    return [CategoryRuleRead.model_validate(r) for r in await service.list_visible(caller)]


@router.post("", response_model=CategoryRuleRead, status_code=status.HTTP_201_CREATED)
async def create_category_rule(
    payload: CategoryRuleCreate,
    caller: CurrentUserDep,
    service: CategoryRuleServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    async def _op() -> CategoryRuleRead:
        return CategoryRuleRead.model_validate(await service.create(caller, payload))

    return await idem.run(
        principal_id=caller.employee_id,
        scope="category_rules.create",
        key=idempotency_key,
        request=payload,
        operation=_op,
        success_status=status.HTTP_201_CREATED,
    )


@router.patch("/{rule_id}", response_model=CategoryRuleRead)
async def update_category_rule(
    rule_id: uuid.UUID,
    payload: CategoryRuleUpdate,
    caller: CurrentUserDep,
    service: CategoryRuleServiceDep,
) -> CategoryRuleRead:
    return CategoryRuleRead.model_validate(await service.update(caller, rule_id, payload))


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category_rule(
    rule_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CategoryRuleServiceDep,
) -> None:
    await service.delete(caller, rule_id)
