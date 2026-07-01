"""SQLAlchemy ORM models.

Importing this package registers every model on `Base.metadata` so Alembic
autogenerate can see them.
"""

from app.models.activity import ActivitySample
from app.models.announcement import Announcement, AnnouncementLevel
from app.models.attendance_policy import AttendancePolicy
from app.models.attribution_correction import AttributionCorrection, CorrectionStatus
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.browsing_hidden_domain import BrowsingHiddenDomain
from app.models.category_rule import CategoryRule
from app.models.compensation import Compensation, PayPeriod
from app.models.device import Device
from app.models.document import DocumentCategory, EmployeeDocument
from app.models.employee import Employee, EmployeeStatus, Role, TrackingMode
from app.models.env_access_token import EnvAccessToken
from app.models.env_project import EnvMemberRole, EnvProject, EnvProjectMember, EnvVersion
from app.models.eod_report import EodReport, EodStatus
from app.models.eod_settings import EodSettings
from app.models.holiday import Holiday, HolidayType
from app.models.idempotency_key import IdempotencyKey, IdempotencyStatus
from app.models.invitation import Invitation, InvitationStatus
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.leave_policy import LeavePolicy
from app.models.notification import Notification, NotificationKind, NotificationLevel
from app.models.onboarding_config import OnboardingConfig
from app.models.org_settings import OrgSettings
from app.models.payroll_run import PayrollRun, PayrollRunSource
from app.models.payroll_settings import PayCycle, PayrollSettings
from app.models.payslip import Payslip, PayslipStatus
from app.models.ping import Ping
from app.models.quick_meet_default import QuickMeetDefault
from app.models.regularization import Regularization, RegularizationStatus
from app.models.screenshot import OcrStatus, Screenshot
from app.models.target import Target, TargetPeriod, TargetStatus
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.models.task_collaborator import TaskCollaborator
from app.models.task_comment import TaskComment
from app.models.work_entity import WorkEntity
from app.models.work_session import WorkSession
from app.models.workspace_file import WorkspaceFile, WorkspaceFileCategory

__all__ = [
    "ActivitySample",
    "Announcement",
    "AnnouncementLevel",
    "AttendancePolicy",
    "AttributionCorrection",
    "AuditLog",
    "Base",
    "BrowsingHiddenDomain",
    "CategoryRule",
    "Compensation",
    "CorrectionStatus",
    "Device",
    "DocumentCategory",
    "Employee",
    "EmployeeDocument",
    "EmployeeStatus",
    "EnvAccessToken",
    "EnvMemberRole",
    "EnvProject",
    "EnvProjectMember",
    "EnvVersion",
    "EodReport",
    "EodSettings",
    "EodStatus",
    "Holiday",
    "HolidayType",
    "IdempotencyKey",
    "IdempotencyStatus",
    "Invitation",
    "InvitationStatus",
    "Leave",
    "LeaveComment",
    "LeavePolicy",
    "LeaveStatus",
    "LeaveType",
    "Notification",
    "NotificationKind",
    "NotificationLevel",
    "OcrStatus",
    "OnboardingConfig",
    "OrgSettings",
    "PayCycle",
    "PayPeriod",
    "PayrollRun",
    "PayrollRunSource",
    "PayrollSettings",
    "Payslip",
    "PayslipStatus",
    "Ping",
    "QuickMeetDefault",
    "Regularization",
    "RegularizationStatus",
    "Role",
    "Screenshot",
    "Target",
    "TargetPeriod",
    "TargetStatus",
    "Task",
    "TaskCadence",
    "TaskCollaborator",
    "TaskComment",
    "TaskPriority",
    "TaskStatus",
    "TrackingMode",
    "WorkEntity",
    "WorkSession",
    "WorkspaceFile",
    "WorkspaceFileCategory",
]
