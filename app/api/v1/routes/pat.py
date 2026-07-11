"""Personal Access Token endpoints - mint / list / revoke.

These are authed by a human JWT (`CurrentUserDep`) ONLY, so a PAT can never mint
another PAT. A caller manages only their own tokens. Routes just parse input,
call the service, and return a schema (Golden rule #5).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, PatServiceDep
from app.schemas.pat import PatCreate, PatCreated, PatRead

router = APIRouter(prefix="/pat", tags=["pat"])


@router.post("/tokens", response_model=PatCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: PatCreate, caller: CurrentUserDep, service: PatServiceDep
) -> PatCreated:
    """Issue a Personal Access Token for Claude Code / the Avora MCP server. The
    raw token is returned ONCE here and never again - only its hash is stored."""
    return await service.issue_token(caller, payload)


@router.get("/tokens", response_model=list[PatRead])
async def list_tokens(caller: CurrentUserDep, service: PatServiceDep) -> list[PatRead]:
    return await service.list_tokens(caller)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID, caller: CurrentUserDep, service: PatServiceDep
) -> None:
    await service.revoke_token(caller, token_id)
