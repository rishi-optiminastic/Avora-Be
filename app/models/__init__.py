"""SQLAlchemy ORM models.

Importing this package registers every model on `Base.metadata` so Alembic
autogenerate can see them.
"""

from app.models.activity import ActivitySample
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.device import Device
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.holiday import Holiday, HolidayType
from app.models.invitation import Invitation, InvitationStatus
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.ping import Ping
from app.models.screenshot import Screenshot
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus

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
    "Ping",
    "Role",
    "Screenshot",
    "Task",
    "TaskCadence",
    "TaskPriority",
    "TaskStatus",
]
