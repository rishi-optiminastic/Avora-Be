"""Avora Tasks MCP server.

A Streamable-HTTP MCP endpoint, mounted into the FastAPI app at `/mcp`, that lets
Claude Code create and complete Avora tasks. It is a thin adapter: every tool
authenticates the caller's Personal Access Token to an employee, then calls the
SAME `TaskService` / `EmployeeService` the REST API uses - so all scope and
manager-only authorization runs unchanged (Golden rules #2, #3). The MCP layer
adds no authorization of its own.

Auth: `Authorization: Bearer <PAT>`. The PAT is minted in the dashboard and set
as `AVORA_TOKEN` in each user's MCP config. Role/scope are re-derived from the
employee record, never trusted from the token.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.db.session import SessionFactory
from app.models.employee import Employee
from app.models.task import Task
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.personal_access_token import PersonalAccessTokenRepository
from app.repositories.ping import PingRepository
from app.repositories.task import TaskRepository
from app.repositories.task_comment import TaskCommentRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.task import TaskCommentCreate, TaskCreate, TaskRead, TaskUpdate
from app.services.email_service import EmailService
from app.services.employee_service import EmployeeService
from app.services.llm_service import LlmService
from app.services.notification_service import NotificationService
from app.services.pat_service import PatService
from app.services.task_service import TaskService

_INSTRUCTIONS = (
    "Avora task tools. Use `whoami` to confirm who you are acting as, "
    "`list_teammates` to resolve a person to an assignee_id before assigning, "
    "`create_task` to assign work (managers can assign to others; anyone can "
    "create a task for themselves), `list_my_tasks` and `get_task` to read your "
    "work, and `update_task` / `complete_task` / `add_comment` to progress it."
)

# DNS-rebinding protection guards browser-reachable localhost servers that rely on
# AMBIENT credentials (cookies) from being driven by a malicious web page. This
# endpoint authenticates a Personal Access Token on every request (no ambient auth),
# is not a browser target, and sits behind TLS + a reverse proxy that already fixes
# the host - so the built-in Host allowlist adds no protection here while rejecting
# the real deploy host. Disable that one check; auth remains fully enforced.
_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Serve the MCP endpoint at exactly "/mcp" (see app.main for the root mount). A
# trailing-slash redirect here (e.g. /mcp -> /mcp/) is fatal: some MCP clients
# drop the Authorization header when following the 307, so the server would see
# no bearer token. Matching the path exactly means no redirect ever happens.
mcp = FastMCP(
    "avora-tasks",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=_TRANSPORT_SECURITY,
)


# --------------------------------------------------------------------------- #
# Auth + service construction (mirrors app.core.deps, without FastAPI DI)
# --------------------------------------------------------------------------- #
def _bearer_from_ctx(ctx: Context[Any, Any, Any]) -> str:
    request = ctx.request_context.request
    headers = getattr(request, "headers", None)
    authorization = headers.get("authorization") if headers is not None else None
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ToolError("Missing bearer token. Set AVORA_TOKEN in your Claude Code MCP config.")
    token = str(authorization)[7:].strip()
    if not token:
        raise ToolError("Empty bearer token.")
    return token


async def _authenticate(session: AsyncSession, ctx: Context[Any, Any, Any]) -> CurrentUser:
    token = _bearer_from_ctx(ctx)
    pat = PatService(
        PersonalAccessTokenRepository(session), AuditRepository(session), get_settings()
    )
    employee_id = await pat.resolve_token(token)
    if employee_id is None:
        raise ToolError(
            "Token is invalid, revoked, or expired. Mint a new one in the Avora dashboard."
        )
    employee = await EmployeeRepository(session).get(employee_id)
    if employee is None or not employee.is_active:
        raise ToolError("Your Avora account is inactive.")
    return CurrentUser(
        employee_id=employee.id, role=employee.role, manager_id=employee.manager_id
    )


def _task_service(session: AsyncSession) -> TaskService:
    settings = get_settings()
    return TaskService(
        TaskRepository(session),
        EmployeeRepository(session),
        WorkEntityRepository(session),
        TaskCommentRepository(session),
        AuditRepository(session),
        NotificationService(NotificationRepository(session)),
        EmailService(settings),
        LlmService(settings),
    )


def _employee_service(session: AsyncSession) -> EmployeeService:
    settings = get_settings()
    return EmployeeService(
        EmployeeRepository(session), PingRepository(session), AuditRepository(session), settings
    )


_Handler = Callable[[AsyncSession, CurrentUser], Awaitable[str]]


async def _dispatch(ctx: Context[Any, Any, Any], handler: _Handler) -> str:
    """Open a session, authenticate, run the handler, commit. Domain errors are
    surfaced as clean tool errors; anything unexpected rolls back and propagates."""
    async with SessionFactory() as session:
        try:
            caller = await _authenticate(session, ctx)
            result = await handler(session, caller)
            await session.commit()
            return result
        except ToolError:
            await session.rollback()
            raise
        except AppError as exc:
            await session.rollback()
            raise ToolError(exc.message) from exc
        except PydanticValidationError as exc:
            await session.rollback()
            raise ToolError(_format_validation(exc)) from exc
        except Exception:
            await session.rollback()
            raise


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ToolError(f"{field} must be a valid id.") from exc


def _format_validation(exc: PydanticValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return "Invalid input - " + "; ".join(parts)


def _task_full(task: Task) -> dict[str, Any]:
    return TaskRead.model_validate(task).model_dump(mode="json")


def _task_brief(task: Task) -> dict[str, Any]:
    full = _task_full(task)
    keys = ("id", "title", "status", "priority", "cadence", "due_date", "completion_pct")
    return {k: full[k] for k in keys}


def _employee_brief(emp: Employee) -> dict[str, Any]:
    return {
        "id": str(emp.id),
        "name": emp.full_name,
        "email": emp.work_email,
        "department": emp.department,
        "job_title": emp.job_title,
        "role": emp.role.value,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def whoami(ctx: Context[Any, Any, Any]) -> str:
    """Return the Avora employee this token acts as (id, name, email, role)."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        me = await _employee_service(session).get_self(caller)
        data = _employee_brief(me)
        data["manager_id"] = str(me.manager_id) if me.manager_id else None
        return json.dumps(data)

    return await _dispatch(ctx, _h)


@mcp.tool()
async def list_teammates(
    ctx: Context[Any, Any, Any], search: str | None = None, limit: int = 25
) -> str:
    """List people you can assign tasks to. Optional case-insensitive `search`
    filters by name or email. Use the returned `id` as `assignee_id`."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        capped = max(1, min(limit, 100))
        people, _ = await _employee_service(session).list_for_caller(
            caller, offset=0, limit=capped
        )
        rows = [_employee_brief(p) for p in people]
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["email"].lower()]
        return json.dumps({"teammates": rows, "count": len(rows)})

    return await _dispatch(ctx, _h)


@mcp.tool()
async def create_task(
    ctx: Context[Any, Any, Any],
    title: str,
    assignee_id: str | None = None,
    description: str | None = None,
    priority: str = "medium",
    cadence: str = "one_time",
    due_date: str | None = None,
) -> str:
    """Create a task. Omit `assignee_id` to assign it to yourself; managers/HR/
    admin may pass a teammate's id (from `list_teammates`) to assign to them.
    `priority` is low|medium|high, `cadence` is one_time|daily|weekly|monthly,
    `due_date` is an ISO date like 2026-07-20."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        target = _parse_uuid(assignee_id, "assignee_id") if assignee_id else caller.employee_id
        payload = TaskCreate.model_validate(
            {
                "title": title,
                "assignee_id": str(target),
                "description": description,
                "priority": priority,
                "cadence": cadence,
                "due_date": due_date,
            }
        )
        task = await _task_service(session).create(caller, payload)
        return json.dumps(_task_full(task))

    return await _dispatch(ctx, _h)


@mcp.tool()
async def list_my_tasks(ctx: Context[Any, Any, Any], status: str | None = None) -> str:
    """List tasks assigned to you. Optional `status` is todo|in_progress|blocked|
    done. Returns a brief per task; call `get_task` for the full description."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        payload = TaskUpdate.model_validate({"status": status}) if status else None
        task_status = payload.status if payload else None
        tasks, total = await _task_service(session).list_for_caller(
            caller, offset=0, limit=50, status=task_status, assignee_id=caller.employee_id
        )
        return json.dumps({"tasks": [_task_brief(t) for t in tasks], "count": total})

    return await _dispatch(ctx, _h)


@mcp.tool()
async def get_task(ctx: Context[Any, Any, Any], task_id: str) -> str:
    """Return the full task, including description and expected output."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        task = await _task_service(session).get_for_caller(caller, _parse_uuid(task_id, "task_id"))
        return json.dumps(_task_full(task))

    return await _dispatch(ctx, _h)


@mcp.tool()
async def update_task(
    ctx: Context[Any, Any, Any],
    task_id: str,
    status: str | None = None,
    completion_pct: int | None = None,
    remarks: str | None = None,
    blocked_reason: str | None = None,
) -> str:
    """Update a task you own or collaborate on. `status` is todo|in_progress|
    blocked|done; moving to `blocked` requires `blocked_reason`. Only the fields
    you pass are changed."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        data: dict[str, Any] = {}
        if status is not None:
            data["status"] = status
        if completion_pct is not None:
            data["completion_pct"] = completion_pct
        if remarks is not None:
            data["remarks"] = remarks
        if blocked_reason is not None:
            data["blocked_reason"] = blocked_reason
        if not data:
            raise ToolError("Nothing to update - pass at least one field.")
        if data.get("status") == "blocked" and not blocked_reason:
            raise ToolError("Moving a task to blocked requires a blocked_reason.")
        payload = TaskUpdate.model_validate(data)
        task = await _task_service(session).update(caller, _parse_uuid(task_id, "task_id"), payload)
        return json.dumps(_task_full(task))

    return await _dispatch(ctx, _h)


@mcp.tool()
async def complete_task(
    ctx: Context[Any, Any, Any], task_id: str, remarks: str | None = None
) -> str:
    """Mark a task done (stamps its completion time). Optional closing `remarks`."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        data: dict[str, Any] = {"status": "done"}
        if remarks is not None:
            data["remarks"] = remarks
        payload = TaskUpdate.model_validate(data)
        task = await _task_service(session).update(caller, _parse_uuid(task_id, "task_id"), payload)
        return json.dumps(_task_full(task))

    return await _dispatch(ctx, _h)


@mcp.tool()
async def add_comment(ctx: Context[Any, Any, Any], task_id: str, body: str) -> str:
    """Post a comment on a task you can see (e.g. a summary of the work done)."""

    async def _h(session: AsyncSession, caller: CurrentUser) -> str:
        payload = TaskCommentCreate.model_validate({"body": body})
        comment = await _task_service(session).add_comment(
            caller, _parse_uuid(task_id, "task_id"), payload, idempotency_key=str(uuid.uuid4())
        )
        return json.dumps(
            {"id": str(comment.id), "task_id": str(comment.task_id), "body": comment.body}
        )

    return await _dispatch(ctx, _h)


# Built once so the session manager can be started from the app lifespan.
mcp_app = mcp.streamable_http_app()
