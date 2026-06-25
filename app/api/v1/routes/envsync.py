"""Env Sync endpoints — the backend for the "Avora Env Sync" App Store app and
its VSCode extension.

Wire-compatible with the original `envsync` Django backend (same field names,
same 409 `{detail, head}` body) so the existing extension works unchanged. Every
read/write is membership-scoped in the service (Security rule 5.3). Routes only
parse input, call the service, and return a schema (Golden rule #5)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, WebSocket, WebSocketDisconnect, status

from app.core.config import get_settings
from app.core.deps import EnvPrincipalDep, EnvSyncServiceDep
from app.core.security import hash_device_token
from app.db.session import SessionFactory
from app.repositories.envsync import EnvSyncRepository
from app.schemas.envsync import (
    CollaboratorAdd,
    CollaboratorRead,
    EnvHistoryRead,
    EnvironmentRead,
    EnvProjectCreate,
    EnvProjectRead,
    EnvPush,
    EnvVersionRead,
    TokenCreate,
    TokenCreated,
    TokenRead,
)
from app.services.envsync_realtime import hub

router = APIRouter(prefix="/envsync", tags=["envsync"])


# --- projects ------------------------------------------------------------- #
@router.get("/projects", response_model=list[EnvProjectRead])
async def list_projects(
    caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> list[EnvProjectRead]:
    return await service.list_projects(caller)


@router.post("/projects", response_model=EnvProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: EnvProjectCreate, caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> EnvProjectRead:
    return await service.create_project(caller, payload)


# --- collaborators -------------------------------------------------------- #
@router.get("/projects/{project_id}/collaborators", response_model=list[CollaboratorRead])
async def list_collaborators(
    project_id: uuid.UUID, caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> list[CollaboratorRead]:
    return await service.list_collaborators(caller, project_id)


@router.post(
    "/projects/{project_id}/collaborators",
    response_model=CollaboratorRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_collaborator(
    project_id: uuid.UUID,
    payload: CollaboratorAdd,
    caller: EnvPrincipalDep,
    service: EnvSyncServiceDep,
) -> CollaboratorRead:
    return await service.add_collaborator(caller, project_id, payload)


@router.delete(
    "/projects/{project_id}/collaborators/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_collaborator(
    project_id: uuid.UUID,
    employee_id: uuid.UUID,
    caller: EnvPrincipalDep,
    service: EnvSyncServiceDep,
) -> None:
    await service.remove_collaborator(caller, project_id, employee_id)


# --- environments & versions --------------------------------------------- #
@router.get("/projects/{project_id}/environments", response_model=list[EnvironmentRead])
async def list_environments(
    project_id: uuid.UUID, caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> list[EnvironmentRead]:
    return await service.list_environments(caller, project_id)


@router.get("/projects/{project_id}/env", response_model=EnvVersionRead)
async def get_env(
    project_id: uuid.UUID,
    caller: EnvPrincipalDep,
    service: EnvSyncServiceDep,
    environment: Annotated[str, Query(min_length=1, max_length=50)] = "default",
) -> EnvVersionRead:
    return await service.get_env(caller, project_id, environment)


@router.post(
    "/projects/{project_id}/env",
    response_model=EnvVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def push_env(
    project_id: uuid.UUID,
    payload: EnvPush,
    caller: EnvPrincipalDep,
    service: EnvSyncServiceDep,
    response: Response,
) -> EnvVersionRead:
    """Push a new version. 201 on a new version, 200 on a no-op (identical
    content), 409 `{detail, head}` if the remote head moved past `base_version`."""
    version, created = await service.push_env(caller, project_id, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return version


@router.get("/projects/{project_id}/env/history", response_model=list[EnvHistoryRead])
async def get_history(
    project_id: uuid.UUID,
    caller: EnvPrincipalDep,
    service: EnvSyncServiceDep,
    environment: Annotated[str, Query(min_length=1, max_length=50)] = "default",
) -> list[EnvHistoryRead]:
    return await service.get_history(caller, project_id, environment)


# --- personal access tokens ----------------------------------------------- #
@router.post("/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: TokenCreate, caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> TokenCreated:
    """Issue a Personal Access Token for the VSCode extension. The raw token is
    returned ONCE here and never again — only its hash is stored."""
    return await service.issue_token(caller, payload)


@router.get("/tokens", response_model=list[TokenRead])
async def list_tokens(caller: EnvPrincipalDep, service: EnvSyncServiceDep) -> list[TokenRead]:
    return await service.list_tokens(caller)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID, caller: EnvPrincipalDep, service: EnvSyncServiceDep
) -> None:
    await service.revoke_token(caller, token_id)


# --- live updates (best-effort WebSocket) --------------------------------- #
@router.websocket("/ws/projects/{project_id}")
async def envsync_ws(websocket: WebSocket, project_id: uuid.UUID) -> None:
    """Push notifications for a project. Auth is a Personal Access Token in the
    `?token=` query param; on any push the client receives `env.updated` (without
    content — it re-fetches over REST). Unauthorized closes with 4401."""
    settings = get_settings()
    token = websocket.query_params.get("token")
    if not settings.envsync_enabled or not token:
        await websocket.close(code=4401)
        return

    # Short-lived session for the auth check only — we never hold a DB connection
    # open for the socket's lifetime.
    async with SessionFactory() as session:
        repo = EnvSyncRepository(session)
        env_token = await repo.get_active_by_token_hash(hash_device_token(settings, token))
        if env_token is None or await repo.member_for(project_id, env_token.employee_id) is None:
            await websocket.close(code=4401)
            return

    await websocket.accept()
    hub.add(project_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # ignore inbound; keeps the socket open
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(project_id, websocket)
