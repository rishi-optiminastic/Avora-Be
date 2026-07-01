"""Shared FastAPI dependencies — wiring + auth + scoping.

This is the only place FastAPI's `Depends`/`Request` meet the service layer.
Auth dependencies re-derive identity, role and scope from the DB; they NEVER
trust a client-supplied role, team, or id (Golden rules #1, #2).
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError, RateLimitError
from app.core.ratelimit import FixedWindowRateLimiter
from app.core.security import (
    TokenError,
    decode_better_auth_jwt,
    hash_device_token,
    verify_hmac_sha256,
)
from app.db.session import get_session
from app.models.employee import Role
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.attribution_correction import AttributionCorrectionRepository
from app.repositories.audit import AuditRepository
from app.repositories.browsing_hidden_domain import BrowsingHiddenDomainRepository
from app.repositories.category_rule import CategoryRuleRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.device import DeviceRepository
from app.repositories.document import DocumentRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.envsync import EnvSyncRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.eod_settings import EodSettingsRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.invitation import InvitationRepository
from app.repositories.leave import LeaveRepository
from app.repositories.leave_comment import LeaveCommentRepository
from app.repositories.leave_policy import LeavePolicyRepository
from app.repositories.notification import NotificationRepository
from app.repositories.onboarding_config import OnboardingConfigRepository
from app.repositories.org_settings import OrgSettingsRepository
from app.repositories.payroll_run import PayrollRunRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.payslip import PayslipRepository
from app.repositories.ping import PingRepository
from app.repositories.quick_meet import QuickMeetRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.target import TargetRepository
from app.repositories.task import TaskRepository
from app.repositories.task_comment import TaskCommentRepository
from app.repositories.work_entity import WorkEntityRepository
from app.repositories.work_session import WorkSessionRepository
from app.repositories.workspace_file import WorkspaceFileRepository
from app.schemas.auth import AuthIdentity, CurrentDevice, CurrentUser, EnvPrincipal
from app.services.activity_service import ActivityService
from app.services.agent_nudge_service import AgentNudgeService
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.attribution_correction_service import AttributionCorrectionService
from app.services.attribution_service import AttributionService
from app.services.biometric_service import BiometricService
from app.services.browsing_privacy_service import BrowsingPrivacyService
from app.services.category_rule_service import CategoryRuleService
from app.services.compensation_service import CompensationService
from app.services.dashboard_service import DashboardService
from app.services.device_service import DeviceService
from app.services.document_service import DocumentService
from app.services.email_service import EmailService
from app.services.employee_service import EmployeeService
from app.services.envsync_service import EnvSyncService
from app.services.eod_service import EodService
from app.services.eod_settings_service import EodSettingsService
from app.services.holiday_service import HolidayService
from app.services.hr_service import HRService
from app.services.idempotency import IdempotencyService
from app.services.invitation_service import InvitationService
from app.services.leave_policy_service import LeavePolicyService
from app.services.leave_service import LeaveService
from app.services.llm_service import LlmService
from app.services.meeting_service import MeetingService
from app.services.monitoring_gate import MonitoringGateService
from app.services.monitoring_service import MonitoringService
from app.services.notification_service import NotificationService
from app.services.onboarding_service import OnboardingService
from app.services.org_settings_service import OrgSettingsService
from app.services.payroll_service import PayrollService
from app.services.ping_service import PingService
from app.services.reconciliation_service import ReconciliationService
from app.services.regularization_service import RegularizationService
from app.services.screenshot_service import ScreenshotService
from app.services.target_service import TargetService
from app.services.task_service import TaskService
from app.services.work_entity_service import WorkEntityService
from app.services.work_session_service import WorkSessionService
from app.services.workspace_file_service import WorkspaceFileService

# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Repository providers
# --------------------------------------------------------------------------- #
def get_employee_repo(db: DbDep) -> EmployeeRepository:
    return EmployeeRepository(db)


def get_device_repo(db: DbDep) -> DeviceRepository:
    return DeviceRepository(db)


def get_activity_repo(db: DbDep) -> ActivityRepository:
    return ActivityRepository(db)


def get_screenshot_repo(db: DbDep) -> ScreenshotRepository:
    return ScreenshotRepository(db)


def get_ping_repo(db: DbDep) -> PingRepository:
    return PingRepository(db)


def get_work_entity_repo(db: DbDep) -> WorkEntityRepository:
    return WorkEntityRepository(db)


def get_audit_repo(db: DbDep) -> AuditRepository:
    return AuditRepository(db)


def get_invitation_repo(db: DbDep) -> InvitationRepository:
    return InvitationRepository(db)


def get_idempotency_repo(db: DbDep) -> IdempotencyRepository:
    return IdempotencyRepository(db)


def get_idempotency_service(
    repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repo)],
) -> IdempotencyService:
    return IdempotencyService(repo)


IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]

# Opt-in replay protection. A route reads this header and hands it to
# IdempotencyService.run(...); absent (None) means no protection (back-compat).
# The param name maps to the `Idempotency-Key` request header.
IdempotencyKeyHeader = Annotated[str | None, Header(max_length=64)]


def get_task_repo(db: DbDep) -> TaskRepository:
    return TaskRepository(db)


def get_task_comment_repo(db: DbDep) -> TaskCommentRepository:
    return TaskCommentRepository(db)


def get_target_repo(db: DbDep) -> TargetRepository:
    return TargetRepository(db)


def get_leave_repo(db: DbDep) -> LeaveRepository:
    return LeaveRepository(db)


def get_leave_policy_repo(db: DbDep) -> LeavePolicyRepository:
    return LeavePolicyRepository(db)


def get_notification_repo(db: DbDep) -> NotificationRepository:
    return NotificationRepository(db)


def get_leave_comment_repo(db: DbDep) -> LeaveCommentRepository:
    return LeaveCommentRepository(db)


def get_holiday_repo(db: DbDep) -> HolidayRepository:
    return HolidayRepository(db)


def get_work_session_repo(db: DbDep) -> WorkSessionRepository:
    return WorkSessionRepository(db)


def get_category_rule_repo(db: DbDep) -> CategoryRuleRepository:
    return CategoryRuleRepository(db)


def get_browsing_hidden_domain_repo(db: DbDep) -> BrowsingHiddenDomainRepository:
    return BrowsingHiddenDomainRepository(db)


def get_attribution_correction_repo(db: DbDep) -> AttributionCorrectionRepository:
    return AttributionCorrectionRepository(db)


def get_attendance_policy_repo(db: DbDep) -> AttendancePolicyRepository:
    return AttendancePolicyRepository(db)


def get_regularization_repo(db: DbDep) -> RegularizationRepository:
    return RegularizationRepository(db)


def get_compensation_repo(db: DbDep) -> CompensationRepository:
    return CompensationRepository(db)


def get_onboarding_config_repo(db: DbDep) -> OnboardingConfigRepository:
    return OnboardingConfigRepository(db)


def get_payroll_settings_repo(db: DbDep) -> PayrollSettingsRepository:
    return PayrollSettingsRepository(db)


def get_org_settings_repo(db: DbDep) -> OrgSettingsRepository:
    return OrgSettingsRepository(db)


def get_payroll_run_repo(db: DbDep) -> PayrollRunRepository:
    return PayrollRunRepository(db)


def get_payslip_repo(db: DbDep) -> PayslipRepository:
    return PayslipRepository(db)


def get_document_repo(db: DbDep) -> DocumentRepository:
    return DocumentRepository(db)


def get_workspace_file_repo(db: DbDep) -> WorkspaceFileRepository:
    return WorkspaceFileRepository(db)


def get_envsync_repo(db: DbDep) -> EnvSyncRepository:
    return EnvSyncRepository(db)


# --------------------------------------------------------------------------- #
# Service providers
# --------------------------------------------------------------------------- #
def get_email_service(settings: SettingsDep) -> EmailService:
    return EmailService(settings)


def get_quick_meet_repo(db: DbDep) -> QuickMeetRepository:
    return QuickMeetRepository(db)


def get_meeting_service(
    settings: SettingsDep,
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    quick_meet: Annotated[QuickMeetRepository, Depends(get_quick_meet_repo)],
) -> MeetingService:
    return MeetingService(settings, employees, audit, quick_meet)


def get_employee_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    pings: Annotated[PingRepository, Depends(get_ping_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> EmployeeService:
    return EmployeeService(employees, pings, audit, settings)


def get_hr_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> HRService:
    return HRService(employees, audit)


def get_device_service(
    settings: SettingsDep,
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> DeviceService:
    return DeviceService(settings, devices, employees, audit)


def get_monitoring_service(
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    rules: Annotated[CategoryRuleRepository, Depends(get_category_rule_repo)],
    hidden: Annotated[BrowsingHiddenDomainRepository, Depends(get_browsing_hidden_domain_repo)],
) -> MonitoringService:
    return MonitoringService(activity, employees, rules, hidden)


def get_browsing_privacy_service(
    hidden: Annotated[BrowsingHiddenDomainRepository, Depends(get_browsing_hidden_domain_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> BrowsingPrivacyService:
    return BrowsingPrivacyService(hidden, employees, audit, settings)


def get_org_settings_service(
    orgs: Annotated[OrgSettingsRepository, Depends(get_org_settings_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> OrgSettingsService:
    return OrgSettingsService(orgs, audit, settings)


def get_attendance_policy_service(
    policies: Annotated[AttendancePolicyRepository, Depends(get_attendance_policy_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> AttendancePolicyService:
    return AttendancePolicyService(policies, audit)


def get_onboarding_service(
    config_repo: Annotated[OnboardingConfigRepository, Depends(get_onboarding_config_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> OnboardingService:
    return OnboardingService(config_repo, audit)


OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]


def get_attendance_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    sessions: Annotated[WorkSessionRepository, Depends(get_work_session_repo)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
    regularizations: Annotated[RegularizationRepository, Depends(get_regularization_repo)],
) -> AttendanceService:
    return AttendanceService(employees, activity, sessions, policy, regularizations)


def get_biometric_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    sessions: Annotated[WorkSessionRepository, Depends(get_work_session_repo)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> BiometricService:
    return BiometricService(employees, sessions, policy, audit)


def get_eod_report_repo(db: DbDep) -> EodReportRepository:
    return EodReportRepository(db)


def get_eod_settings_repo(db: DbDep) -> EodSettingsRepository:
    return EodSettingsRepository(db)


def get_eod_settings_service(
    repo: Annotated[EodSettingsRepository, Depends(get_eod_settings_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> EodSettingsService:
    return EodSettingsService(repo, audit, settings)


EodSettingsServiceDep = Annotated[EodSettingsService, Depends(get_eod_settings_service)]


def get_llm_service(settings: SettingsDep) -> LlmService:
    return LlmService(settings)


def get_eod_service(
    reports: Annotated[EodReportRepository, Depends(get_eod_report_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    tasks: Annotated[TaskRepository, Depends(get_task_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    screenshots: Annotated[ScreenshotRepository, Depends(get_screenshot_repo)],
    attendance: Annotated[AttendanceService, Depends(get_attendance_service)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
    llm: Annotated[LlmService, Depends(get_llm_service)],
    email: Annotated[EmailService, Depends(get_email_service)],
    notifications: Annotated[NotificationService, Depends(get_notification_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    eod_settings: Annotated[EodSettingsService, Depends(get_eod_settings_service)],
    settings: SettingsDep,
) -> EodService:
    return EodService(
        reports,
        employees,
        tasks,
        activity,
        screenshots,
        attendance,
        policy,
        llm,
        email,
        notifications,
        audit,
        eod_settings,
        settings,
    )


EodServiceDep = Annotated[EodService, Depends(get_eod_service)]


def get_reconciliation_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    sessions: Annotated[WorkSessionRepository, Depends(get_work_session_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
) -> ReconciliationService:
    return ReconciliationService(employees, sessions, activity, policy)


def get_regularization_service(
    regularizations: Annotated[RegularizationRepository, Depends(get_regularization_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> RegularizationService:
    return RegularizationService(regularizations, employees, policy, audit)


def get_category_rule_service(
    rules: Annotated[CategoryRuleRepository, Depends(get_category_rule_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> CategoryRuleService:
    return CategoryRuleService(rules, employees, audit)


def get_dashboard_service(
    tasks: Annotated[TaskRepository, Depends(get_task_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    leaves: Annotated[LeaveRepository, Depends(get_leave_repo)],
    attendance: Annotated[AttendanceService, Depends(get_attendance_service)],
    monitoring: Annotated[MonitoringService, Depends(get_monitoring_service)],
) -> DashboardService:
    return DashboardService(tasks, employees, activity, entities, leaves, attendance, monitoring)


def get_attribution_correction_service(
    corrections: Annotated[
        AttributionCorrectionRepository, Depends(get_attribution_correction_repo)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> AttributionCorrectionService:
    return AttributionCorrectionService(corrections, employees, entities, audit)


def get_work_session_service(
    sessions: Annotated[WorkSessionRepository, Depends(get_work_session_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> WorkSessionService:
    return WorkSessionService(sessions, audit)


def get_monitoring_gate(
    work_sessions: Annotated[WorkSessionRepository, Depends(get_work_session_repo)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
) -> MonitoringGateService:
    return MonitoringGateService(work_sessions, policy)


def get_screenshot_service(
    screenshots: Annotated[ScreenshotRepository, Depends(get_screenshot_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    settings: SettingsDep,
    gate: Annotated[MonitoringGateService, Depends(get_monitoring_gate)],
) -> ScreenshotService:
    return ScreenshotService(screenshots, employees, settings, gate)


def get_ping_service(
    pings: Annotated[PingRepository, Depends(get_ping_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> PingService:
    return PingService(pings, employees, audit)


def get_work_entity_service(
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> WorkEntityService:
    return WorkEntityService(entities, audit)


def get_attribution_service(
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    screenshots: Annotated[ScreenshotRepository, Depends(get_screenshot_repo)],
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
) -> AttributionService:
    return AttributionService(activity, screenshots, entities, employees)


def get_activity_service(
    settings: SettingsDep,
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    gate: Annotated[MonitoringGateService, Depends(get_monitoring_gate)],
) -> ActivityService:
    return ActivityService(settings, devices, activity, employees, audit, gate)


def get_invitation_service(
    settings: SettingsDep,
    invitations: Annotated[InvitationRepository, Depends(get_invitation_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    email: Annotated[EmailService, Depends(get_email_service)],
) -> InvitationService:
    return InvitationService(settings, invitations, employees, audit, email)


def get_notification_service(
    notifications: Annotated[NotificationRepository, Depends(get_notification_repo)],
) -> NotificationService:
    return NotificationService(notifications)


def get_agent_nudge_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    pings: Annotated[PingRepository, Depends(get_ping_repo)],
    notifications: Annotated[NotificationService, Depends(get_notification_service)],
    email: Annotated[EmailService, Depends(get_email_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> AgentNudgeService:
    return AgentNudgeService(employees, devices, pings, notifications, email, audit)


def get_task_service(
    tasks: Annotated[TaskRepository, Depends(get_task_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    comments: Annotated[TaskCommentRepository, Depends(get_task_comment_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    notifications: Annotated[NotificationService, Depends(get_notification_service)],
    email: Annotated[EmailService, Depends(get_email_service)],
    llm: Annotated[LlmService, Depends(get_llm_service)],
) -> TaskService:
    return TaskService(tasks, employees, entities, comments, audit, notifications, email, llm)


def get_target_service(
    targets: Annotated[TargetRepository, Depends(get_target_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> TargetService:
    return TargetService(targets, employees, audit)


def get_envsync_service(
    repo: Annotated[EnvSyncRepository, Depends(get_envsync_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> EnvSyncService:
    return EnvSyncService(repo, employees, audit, settings)


EnvSyncServiceDep = Annotated[EnvSyncService, Depends(get_envsync_service)]


def get_leave_policy_service(
    policies: Annotated[LeavePolicyRepository, Depends(get_leave_policy_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> LeavePolicyService:
    return LeavePolicyService(policies, audit)


def get_leave_service(
    leaves: Annotated[LeaveRepository, Depends(get_leave_repo)],
    comments: Annotated[LeaveCommentRepository, Depends(get_leave_comment_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    notifications: Annotated[NotificationService, Depends(get_notification_service)],
    email: Annotated[EmailService, Depends(get_email_service)],
    policy: Annotated[LeavePolicyService, Depends(get_leave_policy_service)],
    holidays: Annotated[HolidayRepository, Depends(get_holiday_repo)],
) -> LeaveService:
    return LeaveService(leaves, comments, employees, audit, notifications, email, policy, holidays)


def get_holiday_service(
    holidays: Annotated[HolidayRepository, Depends(get_holiday_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> HolidayService:
    return HolidayService(holidays, audit)


def get_compensation_service(
    compensation: Annotated[CompensationRepository, Depends(get_compensation_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> CompensationService:
    return CompensationService(compensation, employees, audit)


def get_payroll_service(
    settings_repo: Annotated[PayrollSettingsRepository, Depends(get_payroll_settings_repo)],
    runs: Annotated[PayrollRunRepository, Depends(get_payroll_run_repo)],
    payslips: Annotated[PayslipRepository, Depends(get_payslip_repo)],
    compensation: Annotated[CompensationRepository, Depends(get_compensation_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    attendance: Annotated[AttendanceService, Depends(get_attendance_service)],
    policy: Annotated[AttendancePolicyService, Depends(get_attendance_policy_service)],
    leaves: Annotated[LeaveRepository, Depends(get_leave_repo)],
    holidays: Annotated[HolidayRepository, Depends(get_holiday_repo)],
    orgs: Annotated[OrgSettingsRepository, Depends(get_org_settings_repo)],
    email: Annotated[EmailService, Depends(get_email_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> PayrollService:
    return PayrollService(
        settings_repo,
        runs,
        payslips,
        compensation,
        employees,
        attendance,
        policy,
        leaves,
        holidays,
        orgs,
        email,
        audit,
    )


def get_document_service(
    documents: Annotated[DocumentRepository, Depends(get_document_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> DocumentService:
    return DocumentService(documents, employees, audit)


def get_workspace_file_service(
    files: Annotated[WorkspaceFileRepository, Depends(get_workspace_file_repo)],
    entities: Annotated[WorkEntityRepository, Depends(get_work_entity_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    settings: SettingsDep,
) -> WorkspaceFileService:
    return WorkspaceFileService(files, entities, audit, settings)


EmployeeServiceDep = Annotated[EmployeeService, Depends(get_employee_service)]
HRServiceDep = Annotated[HRService, Depends(get_hr_service)]
ActivityServiceDep = Annotated[ActivityService, Depends(get_activity_service)]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]
AgentNudgeServiceDep = Annotated[AgentNudgeService, Depends(get_agent_nudge_service)]
MonitoringServiceDep = Annotated[MonitoringService, Depends(get_monitoring_service)]
BrowsingPrivacyServiceDep = Annotated[BrowsingPrivacyService, Depends(get_browsing_privacy_service)]
AttendancePolicyServiceDep = Annotated[
    AttendancePolicyService, Depends(get_attendance_policy_service)
]
OrgSettingsServiceDep = Annotated[OrgSettingsService, Depends(get_org_settings_service)]
AttendanceServiceDep = Annotated[AttendanceService, Depends(get_attendance_service)]
BiometricServiceDep = Annotated[BiometricService, Depends(get_biometric_service)]
ReconciliationServiceDep = Annotated[ReconciliationService, Depends(get_reconciliation_service)]
RegularizationServiceDep = Annotated[RegularizationService, Depends(get_regularization_service)]
ScreenshotServiceDep = Annotated[ScreenshotService, Depends(get_screenshot_service)]
PingServiceDep = Annotated[PingService, Depends(get_ping_service)]
WorkEntityServiceDep = Annotated[WorkEntityService, Depends(get_work_entity_service)]
AttributionServiceDep = Annotated[AttributionService, Depends(get_attribution_service)]
InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
TargetServiceDep = Annotated[TargetService, Depends(get_target_service)]
LeaveServiceDep = Annotated[LeaveService, Depends(get_leave_service)]
LeavePolicyServiceDep = Annotated[LeavePolicyService, Depends(get_leave_policy_service)]
MeetingServiceDep = Annotated[MeetingService, Depends(get_meeting_service)]
NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]
HolidayServiceDep = Annotated[HolidayService, Depends(get_holiday_service)]
CompensationServiceDep = Annotated[CompensationService, Depends(get_compensation_service)]
PayrollServiceDep = Annotated[PayrollService, Depends(get_payroll_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
WorkspaceFileServiceDep = Annotated[WorkspaceFileService, Depends(get_workspace_file_service)]
WorkSessionServiceDep = Annotated[WorkSessionService, Depends(get_work_session_service)]
CategoryRuleServiceDep = Annotated[CategoryRuleService, Depends(get_category_rule_service)]
DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
AttributionCorrectionServiceDep = Annotated[
    AttributionCorrectionService, Depends(get_attribution_correction_service)
]


# --------------------------------------------------------------------------- #
# Human authentication — Better Auth JWT (Bearer) verified via JWKS.
#
# Better Auth (the Next.js app) authenticates the human and issues a short-lived
# asymmetric JWT. We verify its signature against the published JWKS, then
# re-derive role/scope from OUR employee record (HR is the source of truth) —
# authentication there, authorization here. The token is identity only; we
# never trust a client-supplied role/team/id (Golden rules #1, #2; rule 5.2).
# --------------------------------------------------------------------------- #
@lru_cache
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """Cached JWKS client (keeps the fetched keys warm across requests)."""
    return jwt.PyJWKClient(jwks_url)


def get_jwks_client(settings: SettingsDep) -> jwt.PyJWKClient:
    return _jwks_client(settings.better_auth_jwks_url)


JwksClientDep = Annotated[jwt.PyJWKClient, Depends(get_jwks_client)]


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError()
    token = authorization[7:].strip()
    if not token:
        raise AuthenticationError()
    return token


def _verify_identity(
    settings: Settings, jwks_client: jwt.PyJWKClient, authorization: str | None
) -> AuthIdentity:
    """Verify a Better Auth Bearer JWT and return the identity it asserts.

    Authentication only — no DB, no employee lookup. Both the full user
    dependency and the invite-accept flow build on this.
    """
    token = _bearer_token(authorization)
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token).key
        claims = decode_better_auth_jwt(settings, token, key=signing_key)
    except (TokenError, jwt.PyJWKClientError, jwt.InvalidTokenError) as exc:
        raise AuthenticationError() from exc

    subject = claims.get("sub")
    email = claims.get("email")
    if not isinstance(subject, str) or not isinstance(email, str) or not email:
        raise AuthenticationError()
    name = claims.get("name")
    return AuthIdentity(subject=subject, email=email, name=name if isinstance(name, str) else None)


async def get_auth_identity(
    settings: SettingsDep,
    jwks_client: JwksClientDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthIdentity:
    """Verified identity that need NOT correspond to an employee yet."""
    return _verify_identity(settings, jwks_client, authorization)


AuthIdentityDep = Annotated[AuthIdentity, Depends(get_auth_identity)]


async def get_current_user(
    settings: SettingsDep,
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    jwks_client: JwksClientDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    identity = _verify_identity(settings, jwks_client, authorization)

    # Map the verified identity to OUR employee by work email, then re-derive
    # role/scope from the DB. A Better Auth user with no employee record (e.g. a
    # fresh self-signup) is authenticated but NOT authorized — employees are
    # provisioned by HR or by accepting an admin invite, never by self-signup.
    employee = await employees.get_by_work_email(identity.email)
    if employee is None or not employee.is_active:
        raise AuthenticationError()

    # Backfill a real display name from the provider on login. Guarded inside the
    # repo to only replace an email-derived placeholder, so it runs at most once
    # and never clobbers a curated or HR-synced name.
    await employees.refresh_display_name(employee, identity.name)

    return CurrentUser(
        employee_id=employee.id,
        role=employee.role,
        manager_id=employee.manager_id,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_envsync_principal(
    settings: SettingsDep,
    service: EnvSyncServiceDep,
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    jwks_client: JwksClientDep,
    authorization: Annotated[str | None, Header()] = None,
) -> EnvPrincipal:
    """An Env Sync caller — a Personal Access Token (VSCode extension) OR a Better
    Auth JWT (web dashboard). PAT is tried first (a single indexed DB lookup, no
    network); only if it misses do we verify the bearer as a JWT. Either way the
    employee is re-derived and must be active (Golden rules #1, #2)."""
    if not settings.envsync_enabled:
        raise AuthenticationError()
    token = _bearer_token(authorization)

    employee_id = await service.resolve_token(token)
    if employee_id is not None:
        employee = await employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise AuthenticationError()
        return EnvPrincipal(employee_id=employee.id)

    identity = _verify_identity(settings, jwks_client, authorization)
    employee = await employees.get_by_work_email(identity.email)
    if employee is None or not employee.is_active:
        raise AuthenticationError()
    return EnvPrincipal(employee_id=employee.id)


EnvPrincipalDep = Annotated[EnvPrincipal, Depends(get_envsync_principal)]


def require_admin(user: CurrentUserDep) -> CurrentUser:
    if not user.is_admin:
        raise AuthorizationError()
    return user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


def require_admin_or_hr(user: CurrentUserDep) -> CurrentUser:
    """User-management actions (profile/status/tracking/invites) are admin OR HR.
    NOTE: privilege (role) changes stay admin-only — see EmployeeService.set_role."""
    if user.role not in (Role.ADMIN, Role.HR):
        raise AuthorizationError()
    return user


AdminOrHrDep = Annotated[CurrentUser, Depends(require_admin_or_hr)]


# Per-user write/expensive-call throttles so a copied request can't be replayed
# in a tight loop to spam or to exhaust storage/bandwidth. In-process; back with
# Redis for multi-instance (see ratelimit module).
def _make_user_rate_limit(
    prefix: str, *, max_requests: int, message: str
) -> Callable[[CurrentUser], CurrentUser]:
    """Build a per-user rate-limit dependency keyed `{prefix}:{employee_id}`."""
    limiter = FixedWindowRateLimiter(max_requests=max_requests, window_seconds=60)

    def verify(user: CurrentUserDep) -> CurrentUser:
        if not limiter.allow(f"{prefix}:{user.employee_id}"):
            raise RateLimitError(message)
        return user

    return verify


verify_comment_rate_limit = _make_user_rate_limit(
    "comment", max_requests=15, message="Too many messages — please slow down."
)
CommentRateLimitDep = Annotated[CurrentUser, Depends(verify_comment_rate_limit)]

# Workspace files: cap uploads (S3 cost + DB bloat) and downloads (bandwidth +
# audit-log write amplification) per user.
verify_upload_rate_limit = _make_user_rate_limit(
    "workspace-upload", max_requests=20, message="Too many uploads — please slow down."
)
UploadRateLimitDep = Annotated[CurrentUser, Depends(verify_upload_rate_limit)]

verify_download_rate_limit = _make_user_rate_limit(
    "workspace-download", max_requests=120, message="Too many downloads — please slow down."
)
DownloadRateLimitDep = Annotated[CurrentUser, Depends(verify_download_rate_limit)]


# --------------------------------------------------------------------------- #
# Agent authentication — per-device bearer token + HMAC body signature.
# --------------------------------------------------------------------------- #
_ingest_limiter = FixedWindowRateLimiter(max_requests=10_000)  # reconfigured at startup


def configure_rate_limiter(settings: Settings) -> None:
    global _ingest_limiter
    _ingest_limiter = FixedWindowRateLimiter(max_requests=settings.agent_ingest_rate_per_minute)


async def get_current_device(
    request: Request,
    settings: SettingsDep,
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    authorization: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
) -> CurrentDevice:
    """Authenticate an agent: bearer device token + HMAC over the raw body.

    Order matters: cheap auth checks first, then the HMAC over the exact bytes
    we will parse. Rate limiting is per device, after we know which device.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError()
    raw_token = authorization[7:].strip()
    if not raw_token:
        raise AuthenticationError()

    device = await devices.get_active_by_token_hash(hash_device_token(settings, raw_token))
    if device is None:
        raise AuthenticationError()

    # HMAC over the exact request body the route will validate (rule 5.4).
    body = await request.body()
    if not x_signature or not verify_hmac_sha256(raw_token, body, x_signature):
        raise AuthenticationError()

    # Per-device rate limit.
    if not _ingest_limiter.allow(str(device.id)):
        raise AuthorizationError("Rate limit exceeded.")

    return CurrentDevice(
        device_id=device.id,
        employee_id=device.employee_id,
        last_sequence=device.last_sequence,
    )


CurrentDeviceDep = Annotated[CurrentDevice, Depends(get_current_device)]


# --------------------------------------------------------------------------- #
# HR webhook authentication — HMAC shared secret + optional IP allowlist.
# --------------------------------------------------------------------------- #
def _ip_allowed(allowlist: list[str], client_ip: str | None) -> bool:
    if not allowlist:  # empty = allow all (dev only; warn in prod config check)
        return True
    if client_ip is None:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


async def verify_hr_webhook(
    request: Request,
    settings: SettingsDep,
    x_hr_signature: Annotated[str | None, Header()] = None,
) -> bytes:
    """Verify the HR webhook HMAC + IP allowlist; return the raw body bytes."""
    client_ip = request.client.host if request.client else None
    if not _ip_allowed(settings.hr_ip_allowlist, client_ip):
        raise AuthorizationError("Source not allowed.")

    body = await request.body()
    if not x_hr_signature or not verify_hmac_sha256(
        settings.hr_webhook_secret, body, x_hr_signature
    ):
        raise AuthenticationError()
    return body


async def verify_biometric_webhook(
    request: Request,
    settings: SettingsDep,
    x_biometric_signature: Annotated[str | None, Header()] = None,
) -> bytes:
    """Verify the biometric connector's HMAC + IP allowlist; return raw body.

    Mirrors the HR webhook: the on-prem connector signs the exact body with the
    shared `biometric_webhook_secret`. We never trust the punches' employee ids
    as privilege — they only resolve to an employee for attendance (rule 5.1).
    """
    client_ip = request.client.host if request.client else None
    if not _ip_allowed(settings.biometric_ip_list, client_ip):
        raise AuthorizationError("Source not allowed.")

    body = await request.body()
    if not x_biometric_signature or not verify_hmac_sha256(
        settings.biometric_webhook_secret, body, x_biometric_signature
    ):
        raise AuthenticationError()
    return body
