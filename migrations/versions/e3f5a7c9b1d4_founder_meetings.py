"""Founder Meetings - requests to meet the CEO, triaged by the Founder's Office

Eight tables and one grant. The problem being solved is administrative: people
ask the CEO for time informally and the Founder's Office becomes a human queue
with no record of who asked, what for, or what was decided.

Shape notes worth knowing before reading the DDL:

* `meeting_approvals` is APPEND-ONLY and separate from `meeting_requests.status`.
  A request can be decided more than once (sent back, re-reviewed, forwarded,
  counter-proposed), so the reviewer-trio-on-the-row shape that reimbursements
  and resignations use would keep only the last decision. The status column is a
  cache of where the history has arrived; these rows are the record.
* `meeting_slots` holds BOTH the requester's alternatives and an approver's
  counter-offers, distinguished by `origin`. That distinction carries a rule:
  a time an approver proposed is bookable only once the requester accepts it.
* `meeting_events` has a UNIQUE constraint on `meeting_request_id`. That
  constraint IS the duplicate-event guard: two concurrent approvals race to
  insert, one wins, and only one Google Calendar event is ever created.
* There is deliberately NO `meeting_integration_accounts` table. Avora reaches
  Calendar through a Workspace service account with domain-wide delegation, so
  there is no per-user OAuth refresh token to store or encrypt.
* `employees.founders_office` is a capability grant like `payroll_manager`,
  kept separate from `assigns_org_wide` because leading the work and
  gatekeeping the founder's day are different jobs held by different people.

Additive only: every table is new and the one added column defaults to false,
so existing code and rows are unaffected.

Revision ID: e3f5a7c9b1d4
Revises: c7e9a1b3d5f8
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3f5a7c9b1d4"
down_revision: str | None = "c7e9a1b3d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy persists StrEnum MEMBER NAMES, so every label below is uppercase.
_CATEGORY = "meetingcategory"
_CATEGORIES = (
    "PROJECT_UPDATE",
    "BUSINESS_STRATEGY",
    "TECHNICAL_DISCUSSION",
    "BUDGET_FINANCE",
    "HR_PEOPLE",
    "CLIENT_ESCALATION",
    "APPROVAL_REQUIRED",
    "PERSONAL_CONFIDENTIAL",
    "OTHER",
)
_PRIORITY = "meetingpriority"
_PRIORITIES = ("NORMAL", "URGENT")
_STATUS = "meetingstatus"
_STATUSES = (
    "SUBMITTED",
    "UNDER_REVIEW",
    "NEEDS_INFORMATION",
    "AWAITING_CEO_APPROVAL",
    "SLOT_PROPOSED",
    "SLOT_ACCEPTED",
    "SCHEDULING",
    "SCHEDULING_FAILED",
    "CONFIRMED",
    "REJECTED",
    "CANCELLED",
    "COMPLETED",
)
_SLOT_ORIGIN = "slotorigin"
_SLOT_ORIGINS = ("REQUESTER_PREFERRED", "REQUESTER_ALTERNATIVE", "FOUNDERS_OFFICE", "CEO")
_SLOT_STATUS = "slotstatus"
_SLOT_STATUSES = ("OPEN", "ACCEPTED", "DECLINED", "SUPERSEDED")
_STAGE = "approvalstage"
_STAGES = ("FOUNDERS_OFFICE", "CEO")
_DECISION = "approvaldecision"
_DECISIONS = ("APPROVED", "FORWARDED", "REJECTED", "INFO_REQUESTED", "SLOT_PROPOSED")
_SYNC = "eventsyncstatus"
_SYNCS = ("PENDING", "CREATED", "UPDATED", "CANCELLED", "FAILED")

_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_CATEGORY, _CATEGORIES),
    (_PRIORITY, _PRIORITIES),
    (_STATUS, _STATUSES),
    (_SLOT_ORIGIN, _SLOT_ORIGINS),
    (_SLOT_STATUS, _SLOT_STATUSES),
    (_STAGE, _STAGES),
    (_DECISION, _DECISIONS),
    (_SYNC, _SYNCS),
)

_NOW = sa.text("now()")


def _stamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in _ENUMS:
        sa.Enum(*labels, name=name).create(bind, checkfirst=True)

    # Referenced with create_type=False throughout: the types are made once
    # above, and letting create_table make them again raises DuplicateObject on
    # Postgres.
    def enum(name: str, labels: tuple[str, ...]) -> postgresql.ENUM:
        return postgresql.ENUM(*labels, name=name, create_type=False)

    # The Founder's Office capability grant. Defaults false, so nobody gains it
    # by this migration running - an admin grants it deliberately.
    op.add_column(
        "employees",
        sa.Column("founders_office", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "meeting_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requester_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category", enum(_CATEGORY, _CATEGORIES), nullable=False, server_default="OTHER"),
        sa.Column("purpose", sa.String(length=2000), nullable=False),
        sa.Column("agenda", sa.String(length=8000), nullable=False),
        sa.Column("expected_outcome", sa.String(length=2000), nullable=True),
        sa.Column(
            "priority", enum(_PRIORITY, _PRIORITIES), nullable=False, server_default="NORMAL"
        ),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("preferred_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("status", enum(_STATUS, _STATUSES), nullable=False, server_default="SUBMITTED"),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("info_request_note", sa.String(length=1000), nullable=True),
        sa.Column("cancel_reason", sa.String(length=1000), nullable=True),
        sa.Column("cancelled_by_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["requester_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cancelled_by_id"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_meeting_requests_requester_id", "meeting_requests", ["requester_id"])
    op.create_index("ix_meeting_requests_category", "meeting_requests", ["category"])
    op.create_index("ix_meeting_requests_priority", "meeting_requests", ["priority"])
    op.create_index("ix_meeting_requests_status", "meeting_requests", ["status"])
    op.create_index("ix_meeting_requests_scheduled", "meeting_requests", ["scheduled_start_at"])
    op.create_index(
        "ix_meeting_requests_status_preferred",
        "meeting_requests",
        ["status", "preferred_start_at"],
    )
    op.create_index(
        "ix_meeting_requests_requester_status", "meeting_requests", ["requester_id", "status"]
    )

    op.create_table(
        "meeting_participants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "meeting_request_id", "employee_id", name="uq_meeting_participants_request_employee"
        ),
    )
    op.create_index(
        "ix_meeting_participants_meeting_request_id",
        "meeting_participants",
        ["meeting_request_id"],
    )
    op.create_index("ix_meeting_participants_employee_id", "meeting_participants", ["employee_id"])

    op.create_table(
        "meeting_slots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "origin",
            enum(_SLOT_ORIGIN, _SLOT_ORIGINS),
            nullable=False,
            server_default="REQUESTER_ALTERNATIVE",
        ),
        sa.Column("proposed_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", enum(_SLOT_STATUS, _SLOT_STATUSES), nullable=False, server_default="OPEN"
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["proposed_by_id"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_meeting_slots_meeting_request_id", "meeting_slots", ["meeting_request_id"])
    op.create_index("ix_meeting_slots_status", "meeting_slots", ["status"])
    op.create_index(
        "ix_meeting_slots_request_status", "meeting_slots", ["meeting_request_id", "status"]
    )

    op.create_table(
        "meeting_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column(
            "content_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        # The in-DB fallback used only when S3 is not configured, mirroring
        # reimbursement receipts and workspace files so local runs need no AWS.
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_meeting_attachments_meeting_request_id",
        "meeting_attachments",
        ["meeting_request_id"],
    )

    # After meeting_slots: `proposed_slot_id` points at it.
    op.create_table(
        "meeting_approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("stage", enum(_STAGE, _STAGES), nullable=False),
        sa.Column("decision", enum(_DECISION, _DECISIONS), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("proposed_slot_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["decided_by_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_slot_id"], ["meeting_slots.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_meeting_approvals_meeting_request_id", "meeting_approvals", ["meeting_request_id"]
    )
    op.create_index("ix_meeting_approvals_decided_by_id", "meeting_approvals", ["decided_by_id"])
    op.create_index(
        "ix_meeting_approvals_request_stage", "meeting_approvals", ["meeting_request_id", "stage"]
    )

    op.create_table(
        "meeting_activity_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_meeting_activity_logs_meeting_request_id",
        "meeting_activity_logs",
        ["meeting_request_id"],
    )
    op.create_index(
        "ix_meeting_activity_logs_request_created",
        "meeting_activity_logs",
        ["meeting_request_id", "created_at"],
    )

    op.create_table(
        "meeting_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_request_id", sa.Uuid(), nullable=False),
        sa.Column("google_event_id", sa.String(length=1024), nullable=True),
        sa.Column("google_calendar_id", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("html_link", sa.String(length=2048), nullable=True),
        sa.Column("meet_url", sa.String(length=2048), nullable=True),
        sa.Column("ical_uid", sa.String(length=1024), nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", enum(_SYNC, _SYNCS), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["meeting_request_id"], ["meeting_requests.id"], ondelete="CASCADE"
        ),
        # The duplicate-event guard: one event per request, enforced by the
        # database rather than by whoever remembers to check.
        sa.UniqueConstraint("meeting_request_id", name="uq_meeting_events_request"),
    )
    op.create_index(
        "ix_meeting_events_meeting_request_id", "meeting_events", ["meeting_request_id"]
    )
    op.create_index("ix_meeting_events_sync_status", "meeting_events", ["sync_status"])
    op.create_index("ix_meeting_events_google_event", "meeting_events", ["google_event_id"])

    op.create_table(
        "meeting_calendar_sync",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("calendar_id", sa.String(length=320), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=True),
        sa.Column("resource_id", sa.String(length=256), nullable=True),
        sa.Column("channel_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_token", sa.String(length=1024), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("calendar_id", name="uq_meeting_calendar_sync_calendar"),
    )

    op.create_table(
        "meeting_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Null until an admin names the CEO. The service reads that as "not set
        # up yet" and refuses requests, rather than queueing them for nobody.
        sa.Column("ceo_employee_id", sa.Uuid(), nullable=True),
        sa.Column("ceo_calendar_id", sa.String(length=320), nullable=False, server_default=""),
        sa.Column(
            "founders_office_may_confirm",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "categories_requiring_ceo",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column(
            "working_hours_start", sa.Time(), nullable=False, server_default=sa.text("'10:00:00'")
        ),
        sa.Column(
            "working_hours_end", sa.Time(), nullable=False, server_default=sa.text("'19:00:00'")
        ),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("booking_window_days", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("min_notice_hours", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("reminder_lead_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["ceo_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("meeting_settings")
    op.drop_table("meeting_calendar_sync")
    op.drop_index("ix_meeting_events_google_event", table_name="meeting_events")
    op.drop_index("ix_meeting_events_sync_status", table_name="meeting_events")
    op.drop_index("ix_meeting_events_meeting_request_id", table_name="meeting_events")
    op.drop_table("meeting_events")
    op.drop_index("ix_meeting_activity_logs_request_created", table_name="meeting_activity_logs")
    op.drop_index("ix_meeting_activity_logs_meeting_request_id", table_name="meeting_activity_logs")
    op.drop_table("meeting_activity_logs")
    op.drop_index("ix_meeting_approvals_request_stage", table_name="meeting_approvals")
    op.drop_index("ix_meeting_approvals_decided_by_id", table_name="meeting_approvals")
    op.drop_index("ix_meeting_approvals_meeting_request_id", table_name="meeting_approvals")
    op.drop_table("meeting_approvals")
    op.drop_index("ix_meeting_attachments_meeting_request_id", table_name="meeting_attachments")
    op.drop_table("meeting_attachments")
    op.drop_index("ix_meeting_slots_request_status", table_name="meeting_slots")
    op.drop_index("ix_meeting_slots_status", table_name="meeting_slots")
    op.drop_index("ix_meeting_slots_meeting_request_id", table_name="meeting_slots")
    op.drop_table("meeting_slots")
    op.drop_index("ix_meeting_participants_employee_id", table_name="meeting_participants")
    op.drop_index("ix_meeting_participants_meeting_request_id", table_name="meeting_participants")
    op.drop_table("meeting_participants")
    op.drop_index("ix_meeting_requests_requester_status", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_status_preferred", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_scheduled", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_status", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_priority", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_category", table_name="meeting_requests")
    op.drop_index("ix_meeting_requests_requester_id", table_name="meeting_requests")
    op.drop_table("meeting_requests")
    op.drop_column("employees", "founders_office")
    bind = op.get_bind()
    for name, _ in _ENUMS:
        sa.Enum(name=name).drop(bind, checkfirst=True)
