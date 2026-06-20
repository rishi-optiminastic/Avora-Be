"""SQLAlchemy ORM models.

Importing this package registers every model on `Base.metadata` so Alembic
autogenerate can see them.
"""

from app.models.activity import ActivitySample
from app.models.attendance_policy import AttendancePolicy
from app.models.attribution_correction import AttributionCorrection, CorrectionStatus
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.category_rule import CategoryRule
from app.models.compensation import Compensation, PayPeriod
from app.models.device import Device
from app.models.document import DocumentCategory, EmployeeDocument
from app.models.employee import Employee, EmployeeStatus, Role, TrackingMode
from app.models.holiday import Holiday, HolidayType
from app.models.invitation import Invitation, InvitationStatus
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.ping import Ping
from app.models.regularization import Regularization, RegularizationStatus
from app.models.screenshot import OcrStatus, Screenshot
from app.models.target import Target, TargetPeriod, TargetStatus
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.models.work_entity import WorkEntity
from app.models.work_session import WorkSession

__all__ = [
    "ActivitySample",
    "AttendancePolicy",
    "AttributionCorrection",
    "AuditLog",
    "Base",
    "CategoryRule",
    "Compensation",
    "CorrectionStatus",
    "Device",
    "DocumentCategory",
    "Employee",
    "EmployeeDocument",
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
    "PayPeriod",
    "Ping",
    "Regularization",
    "RegularizationStatus",
    "Role",
    "Screenshot",
    "Target",
    "TargetPeriod",
    "TargetStatus",
    "Task",
    "TaskCadence",
    "TaskPriority",
    "TaskStatus",
    "TrackingMode",
    "WorkEntity",
    "WorkSession",
]
