"""SQLAlchemy ORM models.

Importing this package registers every model on `Base.metadata` so Alembic
autogenerate can see them.
"""

from app.models.activity import ActivitySample
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.device import Device
from app.models.employee import Employee, EmployeeStatus, Role, TrackingMode
from app.models.holiday import Holiday, HolidayType
from app.models.invitation import Invitation, InvitationStatus
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.ping import Ping
from app.models.screenshot import OcrStatus, Screenshot
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.models.work_entity import WorkEntity
from app.models.work_session import WorkSession

__all__ = [
    "ActivitySample",
    "AuditLog",
    "Base",
    "Device",
    "Employee",
    "EmployeeStatus",
    "Holiday",
    "HolidayType",
    "Invitation",
    "InvitationStatus",
    "Leave",
    "LeaveComment",
    "LeaveStatus",
    "LeaveType",
    "OcrStatus",
    "Ping",
    "Role",
    "Screenshot",
    "Task",
    "TaskCadence",
    "TaskPriority",
    "TaskStatus",
    "TrackingMode",
    "WorkEntity",
    "WorkSession",
]
