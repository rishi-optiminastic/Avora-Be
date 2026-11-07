"""Branded Avora email templates.

Two inline-styled shells. `_layout` is how every transactional Avora email
looks: no inner card, content directly on the paper background, radii following
the product's rounded-md (8px). `_report_layout` is the end-of-day reports'
shell: a square white card on grey with a dark header band, in the format of
the team's own written status updates. The wordmark and headings ask for
Reckless (the product serif) with a Georgia fallback, since most email clients
can't load a custom web font.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape
from typing import TYPE_CHECKING

from app.core.probation_letter_pdf import COMPANY_LEGAL_NAME

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from app.schemas.eod import (
        EodDepartmentDigest,
        EodDepartmentMember,
        EodDraftContent,
        EodNarrative,
    )

_VIOLET = "#5a48e0"
_VIOLET_SOFT = "#ece9f7"
_INK = "#1a1530"
_MUTED = "#6b6485"
_PAPER = "#f6f4ff"
_LINE = "#e6e2f0"
_CARD = "#ffffff"
# Amber accent — used to make EOD blockers stand out from the violet brand.
_AMBER = "#9a6207"
_AMBER_INK = "#5c3d05"
_AMBER_SOFT = "#fcf3e3"
_AMBER_LINE = "#f0dcb8"
_SERIF = "'Reckless Neue', Georgia, 'Times New Roman', serif"
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

_INVITE_FOOTER = (
    "You're receiving this because someone invited you to an Avora workspace. "
    "If you weren't expecting it, you can safely ignore this email."
)

_ACTIVITY_FOOTER = (
    "You're receiving this because of activity in your Avora workspace. "
    "Manage what reaches your inbox from your dashboard settings."
)

_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _money(minor: int, currency: str) -> str:
    """Render integer minor units as a grouped amount, e.g. 4_620_000 → ₹46,200."""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{minor // 100:,}"


# The shell's column width. Wide enough for prose, narrow enough to read on a
# phone without zooming. The end-of-day reports use their own card shell
# (`_report_layout`) at 600px.
_WIDTH = 460

# Phone-width adjustments. Every Avora email is fluid already (percentage
# widths, no fixed pixel columns); this trims the chrome so a 360px screen
# spends its width on the content rather than on padding. Gmail, Apple Mail and
# Outlook mobile honour an embedded media query; a client that strips <style>
# still gets the fluid layout, just with desktop padding.
_RESPONSIVE_CSS = """\
@media only screen and (max-width: 600px) {
  .shell { padding: 22px 12px !important; }
  .pad { padding-left: 16px !important; padding-right: 16px !important; }
  .title { font-size: 26px !important; }
  .glance-cell { padding: 10px 5px !important; }
  .glance-value { font-size: 20px !important; }
  .glance-label { letter-spacing: 0.04em !important; }
}"""


def _head() -> str:
    """The <head> both shells share. The viewport meta is what makes a phone
    client lay the mail out at its own width instead of scaling a desktop
    column down until the type is unreadable; the style block carries the
    phone-width rules."""
    return f"""\
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="x-apple-disable-message-reformatting">
    <style>{_RESPONSIVE_CSS}</style>
  </head>"""


def _layout(
    *,
    preheader: str,
    content_html: str,
    footer: str = _INVITE_FOOTER,
    max_width: int = _WIDTH,
) -> str:
    """Wrap an email body in the shared Avora shell — no card, on paper.

    The head carries a viewport meta and one media query: without the meta a
    phone client lays the mail out at desktop width and scales it down until the
    type is unreadable, which is exactly what the department rollup used to do."""
    return f"""\
<!doctype html>
<html>
  {_head()}
  <body style="margin:0;padding:0;background:{_PAPER};font-family:{_SANS};-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;">
    <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</span>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAPER};">
      <tr><td align="center" class="shell" style="padding:44px 20px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:{max_width}px;">
          <tr><td style="padding-bottom:22px;border-bottom:1px solid {_LINE};">
            <span style="font-family:{_SERIF};font-size:24px;font-weight:600;color:{_INK};letter-spacing:-0.01em;">Avora</span>
          </td></tr>
          <tr><td style="padding:24px 0 0;">{content_html}</td></tr>
          <tr><td style="padding:26px 0 0;color:{_MUTED};font-size:11.5px;line-height:1.6;">
            {footer}
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _chip(text: str) -> str:
    return (
        f'<span style="display:inline-block;background:{_VIOLET_SOFT};color:{_VIOLET};'
        f'font-size:12px;font-weight:600;padding:3px 10px;border-radius:8px;">{text}</span>'
    )


def _button(label: str, url: str) -> str:
    return (
        f'<a href="{url}" '
        f'style="display:inline-block;background:{_VIOLET};color:#ffffff;text-decoration:none;'
        f'font-weight:600;font-size:14px;padding:13px 26px;border-radius:8px;">{label}</a>'
    )


def invite_email(
    *,
    inviter_name: str,
    role_label: str,
    org_name: str,
    accept_url: str,
    expires_label: str,
) -> tuple[str, str]:
    """Render the (subject, html) for a workspace invitation."""
    subject = f"You're invited to join {org_name} on Avora"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Invitation
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  You've been invited
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  <strong>{inviter_name}</strong> has invited you to join <strong>{org_name}</strong> on Avora.
</p>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:{_MUTED};">
  Your role will be {_chip(role_label)}
</p>
<p style="margin:0 0 24px;">{_button("Accept invitation", accept_url)}</p>
<p style="margin:0;font-size:12px;line-height:1.6;color:{_MUTED};">
  This invite expires {expires_label}. If the button doesn't work, paste this link into your browser:<br />
  <a href="{accept_url}" style="color:{_VIOLET};word-break:break-all;">{accept_url}</a>
</p>"""
    return subject, _layout(
        preheader=f"{inviter_name} invited you to {org_name} on Avora as {role_label}",
        content_html=content,
    )


def _digest_row(
    name: str, net_minor: int, payable_days: float, total_days: int, currency: str
) -> str:
    days = f"{payable_days:g}/{total_days}"
    return (
        f'<tr><td style="padding:9px 0;border-top:1px solid {_LINE};font-size:13px;color:{_INK};">{name}</td>'
        f'<td style="padding:9px 0;border-top:1px solid {_LINE};font-size:12px;color:{_MUTED};text-align:center;">{days}</td>'
        f'<td style="padding:9px 0;border-top:1px solid {_LINE};font-size:13px;color:{_INK};text-align:right;font-variant-numeric:tabular-nums;">'
        f"{_money(net_minor, currency)}</td></tr>"
    )


def payroll_digest_email(
    *,
    month_label: str,
    currency: str,
    lines: list[tuple[str, int, float, int]],
    total_net_minor: int,
) -> tuple[str, str]:
    """Render the (subject, html) for the monthly HR payroll digest.

    Each line is (employee_name, net_minor, payable_days, total_days).
    """
    subject = f"Payroll estimate — {month_label}"
    rows = "".join(
        _digest_row(name, net, payable, total, currency) for name, net, payable, total in lines
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Payroll · {month_label}
</div>
<h1 style="margin:8px 0 4px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {_money(total_net_minor, currency)}
</h1>
<p style="margin:0 0 20px;font-size:13px;line-height:1.6;color:{_MUTED};">
  Total net payroll across {len(lines)} {"employee" if len(lines) == 1 else "employees"},
  prorated by attendance.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
  <tr>
    <td style="font-size:10.5px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:{_MUTED};">Employee</td>
    <td style="font-size:10.5px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:{_MUTED};text-align:center;">Days</td>
    <td style="font-size:10.5px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:{_MUTED};text-align:right;">Net pay</td>
  </tr>
  {rows}
  <tr>
    <td style="padding:11px 0;border-top:2px solid {_INK};font-size:13px;font-weight:600;color:{_INK};">Total</td>
    <td style="border-top:2px solid {_INK};"></td>
    <td style="padding:11px 0;border-top:2px solid {_INK};font-size:14px;font-weight:600;color:{_INK};text-align:right;font-variant-numeric:tabular-nums;">{_money(total_net_minor, currency)}</td>
  </tr>
</table>"""
    return subject, _layout(
        preheader=f"Payroll for {month_label}: {_money(total_net_minor, currency)} across {len(lines)}",
        content_html=content,
        footer="You're receiving this because you're listed as a payroll recipient in Avora.",
    )


def _note_block(note: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 24px;"><tr><td style="background:{_VIOLET_SOFT};'
        f'border-radius:8px;padding:13px 16px;font-size:13.5px;line-height:1.6;color:{_INK};">'
        f"{note}</td></tr></table>"
    )


def payslip_email(
    *,
    employee_name: str,
    month_label: str,
    currency: str,
    net_payable_minor: int,
    pay_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling an employee their payslip is released.

    The PDF rides along as an attachment; this body is the cover note plus a link
    to download it again from My Pay.
    """
    first = employee_name.split()[0] if employee_name.strip() else "there"
    subject = f"Your payslip for {month_label}"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Payslip · {month_label}
</div>
<h1 style="margin:8px 0 4px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {_money(net_payable_minor, currency)}
</h1>
<p style="margin:0 0 18px;font-size:13px;line-height:1.6;color:{_MUTED};">
  Net payable for {month_label}, prorated by your attendance.
</p>
<p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {first}, your payslip for <strong>{month_label}</strong> is ready. The PDF is attached to
  this email, and you can download it any time from My Pay.
</p>
<p style="margin:0 0 4px;">{_button("View My Pay", pay_url)}</p>"""
    return subject, _layout(
        preheader=f"Your {month_label} payslip — {_money(net_payable_minor, currency)} net payable",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def leave_decision_email(
    *,
    employee_name: str,
    approved: bool,
    leave_type_label: str,
    date_range_label: str,
    decided_by: str,
    note: str | None,
    leave_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling an employee their leave was decided."""
    verb = "approved" if approved else "declined"
    subject = f"Your leave was {verb}"
    note_html = _note_block(note) if note else ""
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Leave · {date_range_label}
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Leave {verb}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {employee_name}, <strong>{decided_by}</strong> {verb} your {leave_type_label} leave for
  <strong>{date_range_label}</strong>.
</p>
{note_html}
<p style="margin:0 0 4px;">{_button("View leave", leave_url)}</p>"""
    return subject, _layout(
        preheader=f"{decided_by} {verb} your {leave_type_label} leave for {date_range_label}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def task_assigned_email(
    *,
    employee_name: str,
    task_title: str,
    assigned_by: str,
    due_label: str | None,
    task_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling an employee a task was assigned to them."""
    subject = f"New task: {task_title}"
    due_html = (
        f'<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:{_MUTED};">'
        f"Due {_chip(due_label)}</p>"
        if due_label
        else ""
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Task assigned
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {task_title}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {employee_name}, <strong>{assigned_by}</strong> assigned this task to you.
</p>
{due_html}
<p style="margin:0 0 4px;">{_button("Open task", task_url)}</p>"""
    return subject, _layout(
        preheader=f"{assigned_by} assigned you a task: {task_title}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def leave_request_email(
    *,
    recipient_name: str,
    requester_name: str,
    leave_type_label: str,
    date_range_label: str,
    reason: str | None,
    can_decide: bool,
    leave_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling an approver/manager that someone applied
    for leave.

    `can_decide` splits the ask: approvers get "review it", a reporting manager
    who can only see the request gets told it's for visibility — so nobody is
    sent chasing a button they don't have.
    """
    subject = f"Leave request: {requester_name} — {date_range_label}"
    reason_html = _note_block(escape(reason)) if reason else ""
    ask = (
        "It's waiting on your approval."
        if can_decide
        else "It's with the approvers — this is for your visibility as their manager."
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Leave request
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {escape(requester_name)} applied for leave
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {escape(recipient_name)}, {escape(requester_name)} has applied for
  {_chip(escape(leave_type_label))} on {_chip(escape(date_range_label))}. {ask}
</p>
{reason_html}
<p style="margin:0 0 4px;">{_button("Review the request" if can_decide else "Open leaves", leave_url)}</p>"""
    return subject, _layout(
        preheader=f"{requester_name} applied for {leave_type_label} ({date_range_label}).",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def out_of_office_approved_email(
    *,
    requester_name: str,
    approver_name: str,
    day_label: str,
    location: str,
    reason: str,
    out_of_office_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) confirming an approved out-of-office day.

    Sent to the person who asked, and copied to the HR mailbox: this credits a
    working day that no biometric punch supports, so whoever runs payroll should
    see it without having to go looking for it.
    """
    subject = f"Out of office approved: {requester_name} - {day_label}"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Out of office
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Approved by {escape(approver_name)}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {escape(requester_name)}, your time working away from the office on
  {_chip(escape(day_label))} at {_chip(escape(location))} has been approved, and
  now counts as worked.
</p>
{_note_block(escape(reason))}
<p style="margin:0 0 4px;">{_button("Open out of office", out_of_office_url)}</p>"""
    return subject, _layout(
        preheader=f"{requester_name} was approved to work from {location} ({day_label}).",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def recognition_awarded_email(
    *,
    recipient_name: str,
    awarded_by_name: str,
    badge_label: str,
    note: str,
    recognition_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling someone they were recognised.

    Sent to the recipient alone. The whole company sees the feed, but mailing
    everyone on every badge - ten kinds, weekly - would train people to ignore
    exactly the message that is meant to feel good to receive.
    """
    subject = f"You received {badge_label}"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Recognition
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {escape(badge_label)}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  {escape(recipient_name)}, {escape(awarded_by_name)} recognised your work.
</p>
{_note_block(escape(note))}
<p style="margin:0 0 4px;">{_button("See it on Avora", recognition_url)}</p>"""
    return subject, _layout(
        preheader=f"{awarded_by_name} gave you {badge_label}.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def task_escalated_email(
    *,
    assignee_name: str,
    task_title: str,
    escalated_by: str,
    due_label: str | None,
    blocked_reason: str | None,
    task_url: str,
) -> tuple[str, str]:
    """Render the (subject, html) telling a reporting manager that a task on one
    of their people has been escalated.

    Escaped, unlike the older templates: the task title and blocker text are
    free-form employee input and must never reach the HTML body raw.
    """
    title = escape(task_title)
    subject = f"Escalated: {task_title}"
    due_html = (
        f'<p style="margin:0 0 14px;font-size:14px;line-height:1.6;color:{_MUTED};">'
        f"Due {_chip(escape(due_label))}</p>"
        if due_label
        else ""
    )
    # The blocker is why it stalled — give it the amber treatment so it reads
    # before the button, the same way the EOD email flags blockers.
    reason_html = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 20px;"><tr><td style="background:{_AMBER_SOFT};'
        f"border:1px solid {_AMBER_LINE};border-radius:8px;padding:13px 16px;"
        f'font-size:13.5px;line-height:1.6;color:{_AMBER_INK};">'
        f'<strong style="color:{_AMBER};">Blocked:</strong> {escape(blocked_reason)}'
        f"</td></tr></table>"
        if blocked_reason
        else ""
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_AMBER};">
  Escalation
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {title}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {escape(assignee_name)}, <strong>{escape(escalated_by)}</strong> escalated this task.
  It needs a push — your reporting manager is copied here.
</p>
{due_html}
{reason_html}
<p style="margin:0 0 4px;">{_button("Open task", task_url)}</p>"""
    return subject, _layout(
        preheader=f"{escalated_by} escalated {task_title}.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def forgot_checkout_email(
    *, employee_name: str, day_label: str, checkout_label: str
) -> tuple[str, str]:
    """Render the (subject, html) telling an employee they forgot to check out and
    were auto-checked-out at the given time."""
    subject = f"You were auto-checked-out — {day_label}"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Attendance
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  We checked you out for you
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {employee_name}, it looks like you forgot to check out on {day_label}. We've
  closed your session automatically at {_chip(checkout_label)} — the time your
  computer last showed activity.
</p>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_MUTED};">
  If that's wrong, ask HR to adjust the day. Remember to check out before you leave
  so your hours stay accurate.
</p>"""
    return subject, _layout(
        preheader=f"You forgot to check out on {day_label} — we closed it at {checkout_label}.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def agent_reinstall_email(*, employee_name: str, install_url: str) -> tuple[str, str]:
    """Render the (subject, html) asking an employee to reinstall the desktop
    agent — sent when their agent has stopped reporting."""
    subject = "Reinstall the Avora agent"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Action needed
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Reinstall the Avora agent
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {employee_name}, your Avora agent isn't reporting right now — it may have been
  closed, uninstalled, or disabled. Please reinstall it so your activity keeps syncing.
</p>
<p style="margin:0 0 4px;">{_button("Reinstall the agent", install_url)}</p>"""
    return subject, _layout(
        preheader="Your Avora agent stopped reporting — please reinstall it.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _day_label(value: str) -> str:
    """ "2026-10-13" -> "Tue, 13 Oct". Anything that is not an ISO date comes back
    untouched, so a caller that already passes a human label (or a range) still
    works and a malformed date can never fail an email.

    Formatted here rather than at the call site so every caller gets it: a raw
    "2026-10-13" in a subject line is the kind of detail that makes an internal
    tool feel unfinished in the inbox.
    """
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]}"


def _active_pct(active_minutes: int, worked_minutes: int) -> int:
    """Active share of time on the machine. 0 when nothing was worked - a
    percentage of no time is not 100%, and must not render as a full day."""
    if worked_minutes <= 0:
        return 0
    return round(active_minutes / worked_minutes * 100)


# -- EOD emails: the status-update format ----------------------------------- #
# Both end-of-day emails follow the format of the team's own written status
# update: a square white card on a grey page, a dark header band with an eyebrow
# and a serif title, four pastel figures "at a glance", one plain sentence of
# totals, violet-barred serif section headings with a count on the right, bold
# rows with a square status pill, and a dark numbered box at the end for what
# needs attention. The palette is that mail's, not the dashboard's, so the two
# reports sitting together in an inbox read as one series.

_REPORT_WIDTH = 600
_REPORT_PAGE = "#f3f2f7"
_REPORT_INK = "#1a1825"
_REPORT_BODY = "#5a5670"
_REPORT_META = "#9a95ae"
_REPORT_LABEL = "#7c7795"
_REPORT_RULE = "#eae8f1"
_REPORT_BAR = "#594a9c"
_REPORT_DARK = "#1e1b2e"
_REPORT_DARK_EYEBROW = "#a79dd6"
_REPORT_DARK_META = "#9d96bc"
_REPORT_DARK_NUMBER = "#8f86b8"

# Pastel tones as (background, ink). Four meanings, never decoration: green is
# done, lavender is in progress, orange needs attention, grey is neutral.
_Tone = tuple[str, str]
_TONE_DONE: _Tone = ("#dff1e6", "#1d6540")
_TONE_ACTIVE: _Tone = ("#e5e1f7", "#4a3c8c")
_TONE_ATTENTION: _Tone = ("#fbe6d5", "#8a4a18")
_TONE_NEUTRAL: _Tone = ("#e5e9f0", "#44506b")

_ADMIN_DIGEST_FOOTER = (
    "You're receiving this because you are an admin of this Avora workspace. "
    "Managers receive each person's full report separately; this rollup carries "
    "figures only."
)

_WEEKDAYS_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS_LONG = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _long_day_label(value: str) -> str:
    """ "2026-11-06" -> "Friday, 6 November 2026", for the header band where the
    short form would look clipped. Non-ISO input comes back untouched, like
    `_day_label`."""
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    return f"{_WEEKDAYS_LONG[day.weekday()]}, {day.day} {_MONTHS_LONG[day.month - 1]} {day.year}"


def _fmt_duration(minutes: int) -> str:
    """Clock-style length: '7h 24m', '3h', '45m'. For headline figures, where
    '7.4h' reads like a rate rather than a span of the day."""
    hours, rest = divmod(max(0, minutes), 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    if hours:
        return f"{hours}h"
    return f"{rest}m"


def _count(noun: str, n: int) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _join_names(names: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


# -- Shell ------------------------------------------------------------------ #


def _block(
    inner: str,
    *,
    padding: str = "26px 20px 0",
    anchor: str | None = None,
    background: str | None = None,
) -> str:
    """One full-width row of the card. The side padding carries the `pad` class
    so the phone rule can tighten it; an empty `inner` renders nothing, so a
    caller can list blocks unconditionally."""
    if not inner:
        return ""
    id_attr = f' id="{anchor}"' if anchor else ""
    bg = f"background:{background};" if background else ""
    return f'<tr><td class="pad"{id_attr} style="padding:{padding};{bg}">{inner}</td></tr>'


def _report_layout(*, preheader: str, blocks: Sequence[str], footer: str) -> str:
    """The status-report shell: a square white card on grey, built from `_block`
    rows, with the footer as the card's last row."""
    footer_html = (
        f'<div style="font-size:12px;line-height:1.6;color:{_REPORT_META};">{footer}</div>'
    )
    return f"""\
<!doctype html>
<html>
  {_head()}
  <body style="margin:0;padding:0;background:{_REPORT_PAGE};font-family:{_SANS};-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;">
    <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</span>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_REPORT_PAGE};">
      <tr><td align="center" style="padding:28px 12px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:{_REPORT_WIDTH}px;background:#ffffff;">
          {"".join(blocks)}
          {_block(footer_html, padding="28px 20px 30px")}
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _eyebrow(text: str, color: str, *, tracking: str = "0.14em", padding: str = "0") -> str:
    return (
        f'<div style="font-size:11px;line-height:1;font-weight:700;letter-spacing:{tracking};'
        f'text-transform:uppercase;color:{color};padding:{padding};">{text}</div>'
    )


def _report_header(*, title: str, date_line: str) -> str:
    """The dark band that opens the card: eyebrow, serif title, the date line."""
    return _block(
        _eyebrow("Avora &middot; End of day", _REPORT_DARK_EYEBROW, tracking="0.16em")
        + f'<div class="title" style="font-family:{_SERIF};font-size:30px;line-height:1.2;'
        f'font-weight:400;color:#ffffff;padding-top:12px;">{title}</div>'
        + f'<div style="font-size:14px;line-height:1.5;color:{_REPORT_DARK_META};'
        f'padding-top:8px;">{date_line}</div>',
        padding="34px 20px 26px",
        background=_REPORT_DARK,
    )


def _glance(cells: Sequence[tuple[str, str, _Tone]]) -> str:
    """ "At a glance": the figures in a strip of pastel cells, equal widths, no
    gaps. Serif numbers, small-caps labels in the cell's own ink."""
    width = f"{100 / len(cells):.2f}%"
    tds = "".join(
        f'<td width="{width}" valign="top" style="background:{bg};">'
        f'<div class="glance-cell" style="padding:12px 10px;">'
        f'<div class="glance-value" style="font-family:{_SERIF};font-size:26px;line-height:1;'
        f'font-weight:400;color:{ink};font-variant-numeric:tabular-nums;white-space:nowrap;">'
        f"{value}</div>"
        f'<div class="glance-label" style="font-size:10px;line-height:1.3;font-weight:700;'
        f"letter-spacing:0.08em;text-transform:uppercase;color:{ink};padding-top:6px;"
        f'">{label}</div></div></td>'
        for value, label, (bg, ink) in cells
    )
    return _eyebrow("At a glance", _REPORT_LABEL, padding="0 0 14px") + (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{tds}</tr>'
        "</table>"
    )


def _sentence(text: str) -> str:
    """The one line of totals under the glance strip."""
    return (
        f'<div style="font-size:14px;line-height:1.5;color:{_REPORT_BODY};padding-top:14px;">'
        f"{text}</div>"
    )


def _lead(text: str) -> str:
    return f'<div style="font-size:16px;line-height:1.6;color:{_REPORT_BODY};">{text}</div>'


def _heading(title: str, aside: str) -> str:
    """A section heading: violet bar, serif title, a muted count on the right."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="border-left:3px solid {_REPORT_BAR};padding:1px 0 1px 10px;">'
        f'<div style="font-family:{_SERIF};font-size:19px;line-height:1.3;font-weight:400;'
        f'color:{_REPORT_INK};">{title}</div></td>'
        f'<td align="right" valign="middle" style="font-size:13px;line-height:1;'
        f'color:{_REPORT_META};white-space:nowrap;padding-left:12px;">{aside}</td>'
        "</tr></table>"
    )


def _figures(parts: Sequence[str]) -> str:
    """A muted line of figures under a heading, indented to the title's text."""
    if not parts:
        return ""
    return (
        f'<div style="font-size:13px;line-height:1.5;color:{_REPORT_META};padding:8px 0 0 13px;'
        f'font-variant-numeric:tabular-nums;">{" &middot; ".join(parts)}</div>'
    )


def _pill(label: str, tone: _Tone) -> str:
    """A square status pill, uppercase, in one of the four tones."""
    bg, ink = tone
    return (
        f'<span style="display:inline-block;background:{bg};color:{ink};font-size:10px;'
        f"line-height:1;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;"
        f'padding:4px 8px;white-space:nowrap;">{label}</span>'
    )


def _tag(label: str, tone: _Tone, *, href: str | None = None) -> str:
    """A square tag that keeps its case: a topic, or a department with its
    fraction. With `href` it is a link, same look."""
    bg, ink = tone
    element = "a" if href else "span"
    href_attr = f' href="{href}"' if href else ""
    return (
        f'<{element}{href_attr} style="display:inline-block;background:{bg};color:{ink};'
        f"font-size:11px;line-height:1;font-weight:700;letter-spacing:0.04em;padding:6px 9px;"
        f'margin:0 6px 6px 0;text-decoration:none;white-space:nowrap;">{label}</{element}>'
    )


def _line(text: str, *, color: str = _REPORT_BODY, size: int = 15, top: int = 8) -> str:
    return (
        f'<div style="font-size:{size}px;line-height:1.55;color:{color};padding-top:{top}px;">'
        f"{text}</div>"
    )


def _row(title: str, pill: str, lines: Sequence[str], *, last: bool) -> str:
    """One item: bold title, the status pill on its own line under it, then any
    detail lines. A hairline closes every row but the section's last."""
    padding = "12px 0 0" if last else "12px 0 14px"
    rule = "" if last else f"border-bottom:1px solid {_REPORT_RULE};"
    title_html = (
        f'<div style="font-size:15px;line-height:1.35;font-weight:700;color:{_REPORT_INK};'
        f'padding-bottom:6px;">{title}</div>'
        if title
        else ""
    )
    return f'<tr><td style="padding:{padding};{rule}">{title_html}{pill}{"".join(lines)}</td></tr>'


def _rows(rows: Sequence[str]) -> str:
    if not rows:
        return ""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"{''.join(rows)}</table>"
    )


def _dark_list(eyebrow: str, items: Sequence[str], *, numbered: bool = True) -> str:
    """The dark box that closes a report: an eyebrow and a numbered list of
    what needs attention. Unnumbered for a single all-clear line."""
    rows = ""
    for index, item in enumerate(items):
        top = 16 if index == 0 else 12
        number = (
            f'<td width="26" valign="top" style="padding:{top}px 0 0;font-family:{_SERIF};'
            f'font-size:15px;line-height:1.5;color:{_REPORT_DARK_NUMBER};">{index + 1}</td>'
            if numbered
            else ""
        )
        rows += (
            f'<tr>{number}<td style="padding:{top}px 0 0;font-size:16px;line-height:1.5;'
            f'color:#ffffff;">{item}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_REPORT_DARK};"><tr><td style="padding:24px 20px 22px;">'
        f"{_eyebrow(eyebrow, _REPORT_DARK_EYEBROW)}"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
        "</td></tr></table>"
    )


def _report_button(label: str, url: str) -> str:
    return (
        f'<a href="{url}" style="display:inline-block;background:{_REPORT_BAR};color:#ffffff;'
        f"text-decoration:none;font-size:13px;line-height:1;font-weight:700;"
        f'letter-spacing:0.04em;padding:13px 22px;">{label}</a>'
    )


def _overdue_note(overdue: int) -> str:
    return (
        f', <span style="color:{_TONE_ATTENTION[1]};">{overdue} overdue</span>' if overdue else ""
    )


# -- Personal report (to the reporting manager) ----------------------------- #


def _paragraphs(text: str) -> str:
    # Escaped: the narrative is free text the employee edits. Unescaped, a stray
    # "<" silently swallowed the rest of the paragraph in the manager's inbox -
    # and anything shaped like a tag was injected straight into the message.
    paras = [p for p in text.split("\n\n") if p.strip()]
    if not paras:
        return ""
    body = "".join(
        f'<p style="margin:0 0 10px;font-size:16px;line-height:1.6;color:{_REPORT_INK};">'
        f"{escape(p)}</p>"
        for p in paras
    )
    return f'<div style="padding-top:12px;">{body}</div>'


def eod_report_email(
    *,
    employee_name: str,
    date_label: str,
    summary: str,
    highlights: EodDraftContent,
    worked_minutes: int = 0,
    active_minutes: int = 0,
    tasks_done: int = 0,
    to_self: bool = False,
) -> tuple[str, str]:
    """Render the (subject, html) End-of-Day report: to its author (`to_self`,
    the scheduled send) or, for a preview, about them.

    Header band with the person and the day; four figures at a glance (on the
    machine, active share, done, blocked); the narrative under Summary; each
    completed task as a row with a Done pill; the areas worked on as tags; and
    the blockers in the dark box at the end. Sections appear only when they have
    something in them, so a quiet day is a short email rather than a card full
    of zeros."""
    # `date_label` arrives as the raw local YYYY-MM-DD from the report row.
    day = _day_label(date_label)
    subject = f"Your end of day · {day}" if to_self else f"End of day · {employee_name} · {day}"
    blockers = [escape(b) for b in highlights.blockers]
    if worked_minutes > 0:
        on_machine = _fmt_duration(worked_minutes)
        active = f"{_active_pct(active_minutes, worked_minutes)}%"
        lead = f"{_fmt_duration(active_minutes)} actively engaged of {on_machine} on the machine."
    else:
        on_machine = active = "-"
        lead = "No activity was recorded from the machine today."
    glance = _glance(
        [
            (on_machine, "Worked", _TONE_NEUTRAL),
            (active, "Active", _TONE_ACTIVE),
            (str(tasks_done), "Done", _TONE_DONE),
            (str(len(blockers)), "Blocked", _TONE_ATTENTION),
        ]
    )
    done = [escape(t) for t in highlights.tasks_completed]
    done_rows = [
        _row(title, _pill("Done", _TONE_DONE), [], last=i == len(done) - 1)
        for i, title in enumerate(done)
    ]
    areas = "".join(_tag(escape(a), _TONE_ACTIVE) for a in highlights.worked_on)
    confidence = max(0, min(100, highlights.confidence or 0))
    summary_html = _paragraphs(summary)
    blocks = [
        _report_header(title=escape(employee_name), date_line=escape(_long_day_label(date_label))),
        _block(glance + _sentence(lead), padding="24px 20px 4px"),
        _block(_heading("Summary", "") + summary_html) if summary_html else "",
        _block(_heading("Done", _count("task", len(done))) + _rows(done_rows)) if done else "",
        _block(
            _heading("Worked on", _count("area", len(highlights.worked_on)))
            + f'<div style="padding-top:12px;">{areas}</div>'
        )
        if areas
        else "",
        _block(_dark_list("Blocked", blockers), padding="34px 20px 0") if blockers else "",
        _block(
            f'<div style="font-size:12px;line-height:1.6;color:{_REPORT_META};">'
            f"Signal confidence {confidence}%, inferred from tasks, activity and on-screen "
            "context.</div>",
            padding="22px 20px 0",
        )
        if confidence
        else "",
    ]
    preheader = (
        f"Your end-of-day summary for {escape(day)}"
        if to_self
        else f"{escape(employee_name)}'s end-of-day summary for {escape(day)}"
    )
    return subject, _report_layout(preheader=preheader, blocks=blocks, footer=_ACTIVITY_FOOTER)


# -- Admin rollup (every department, one email) ----------------------------- #

# Coverage band -> (tone, pill label). Lavender for a draft that exists but was
# never reviewed, orange for a report that should exist and does not, grey for
# someone who was legitimately away.
_COVERAGE_PILL: dict[str, tuple[_Tone, str]] = {
    "submitted": (_TONE_DONE, "Reported"),
    "pending": (_TONE_ACTIVE, "Draft pending"),
    "missing": (_TONE_ATTENTION, "No report"),
    "failed": (_TONE_ATTENTION, "Report failed"),
    "absent": (_TONE_NEUTRAL, "Absent"),
}
# The coverage bars: band, bar colour, legend word. The tone's ink, because the
# pastel is invisible as a thin bar on white. Painted healthy, then owed, then
# away, left to right.
_COVERAGE_BAR: tuple[tuple[str, str, str], ...] = (
    ("submitted", _TONE_DONE[1], "reported"),
    ("pending", _TONE_ACTIVE[1], "draft pending"),
    ("failed", _TONE_ATTENTION[1], "report failed"),
    ("missing", _TONE_ATTENTION[1], "no report"),
    ("absent", _TONE_NEUTRAL[1], "absent"),
)
# People who could have reported and have not. Absent is not owed: someone who
# was not at work legitimately has no report, and dunning them for it is how a
# tool starts feeling like surveillance rather than a summary. Mirrors the
# dashboard's "Still owed" row (fe/.../team-eod `_digest.tsx`).
_OWED_BANDS = ("pending", "missing")
# What the "Needs attention" box says about each band, singular and plural.
_ATTENTION_VERBS: tuple[tuple[str, str, str], ...] = (
    ("missing", "has not reported", "have not reported"),
    ("pending", "has a draft that was never reviewed", "have drafts that were never reviewed"),
    ("failed", "had a report fail to generate", "had reports fail to generate"),
)
# How many people to name in the attention box before summarising the rest.
# Across a whole company the list can run long, and past this it stops being read.
_MAX_OWED_NAMED = 12
# How many shipped titles to name per person before summarising the rest. A
# twenty-task day must not turn one roster row into half the email.
_MAX_TASKS_SHOWN = 3


def _department_anchor(department: str) -> str:
    """A stable id for a department's section, so the chart up top can link down."""
    return "dept-" + re.sub(r"[^a-z0-9]+", "-", department.lower()).strip("-")


def _department_link(digest: EodDepartmentDigest) -> str:
    return (
        f'<a href="#{_department_anchor(digest.department)}" style="color:{_REPORT_INK};'
        f'font-weight:600;text-decoration:none;">{escape(digest.department)}</a>'
    )


def _stacked_bar(segments: Sequence[tuple[int, str]], total: int, *, height: int = 10) -> str:
    """A proportion bar built from table cells, so every mail client draws it:
    one cell per non-zero segment, the rest of the width in the track colour."""
    total = max(total, 1)
    cells = ""
    filled = 0
    for count, color in segments:
        if count <= 0:
            continue
        filled += count
        cells += (
            f'<td style="width:{count / total * 100:.3f}%;background:{color};'
            f'height:{height}px;font-size:0;line-height:0;">&nbsp;</td>'
        )
    if filled < total:
        cells += (
            f'<td style="width:{(total - filled) / total * 100:.3f}%;background:{_REPORT_RULE};'
            f'height:{height}px;font-size:0;line-height:0;">&nbsp;</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"<tr>{cells}</tr></table>"
    )


def _coverage_segments(digest: EodDepartmentDigest) -> list[tuple[int, str]]:
    return [(int(getattr(digest, band)), color) for band, color, _ in _COVERAGE_BAR]


def _coverage_words(digest: EodDepartmentDigest) -> str:
    """ "5 reported · 2 absent · 1 no report": the bar in words, non-zero only."""
    return " &middot; ".join(
        f"{getattr(digest, band)} {word}"
        for band, _, word in _COVERAGE_BAR
        if getattr(digest, band)
    )


def _swatch(color: str, label: str) -> str:
    return (
        f'<span style="display:inline-block;margin:0 12px 4px 0;font-size:11px;color:{_REPORT_META};">'
        f'<span style="display:inline-block;width:9px;height:9px;background:{color};'
        f'vertical-align:-1px;"></span> {label}</span>'
    )


def _bar_legend() -> str:
    return "".join(
        _swatch(color, label)
        for color, label in (
            (_TONE_DONE[1], "reported"),
            (_TONE_ACTIVE[1], "draft pending"),
            (_TONE_ATTENTION[1], "no report or failed"),
            (_TONE_NEUTRAL[1], "absent"),
        )
    )


def _chart(rows: Sequence[tuple[str, str, str]]) -> str:
    """A horizontal bar chart as a table: label, bar, value per row."""
    body = "".join(
        f'<tr><td style="padding:5px 12px 5px 0;font-size:13px;line-height:1.3;'
        f'color:{_REPORT_INK};">{label}</td>'
        f'<td width="50%" style="padding:5px 0;vertical-align:middle;">{bar}</td>'
        f'<td align="right" style="padding:5px 0 5px 10px;font-size:12px;color:{_REPORT_META};'
        f'white-space:nowrap;font-variant-numeric:tabular-nums;">{value}</td></tr>'
        for label, bar, value in rows
    )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table>'


def _coverage_chart(digests: Sequence[EodDepartmentDigest]) -> str:
    """Every department as a bar of who reported, owes, or was away, with its
    fraction. The names link down to the sections, so this is also the index."""
    rows = [
        (
            _department_link(d),
            _stacked_bar(_coverage_segments(d), d.member_count),
            f"{d.submitted} / {d.member_count}",
        )
        for d in digests
    ]
    return (
        _eyebrow("Coverage by department", _REPORT_LABEL, padding="0 0 10px")
        + _chart(rows)
        + f'<div style="padding-top:8px;">{_bar_legend()}</div>'
    )


def _shipped_chart(digests: Sequence[EodDepartmentDigest]) -> str:
    """Tasks closed today per department, bars relative to the busiest one.
    Nothing when the day shipped nothing: an empty chart is not information."""
    top = max((d.totals.tasks_done for d in digests), default=0)
    if top <= 0:
        return ""
    rows = [
        (
            _department_link(d),
            _stacked_bar([(d.totals.tasks_done, _TONE_ACTIVE[1])], top),
            _count("task", d.totals.tasks_done),
        )
        for d in digests
        if d.totals.tasks_done
    ]
    return _eyebrow("Tasks shipped", _REPORT_LABEL, padding="0 0 10px") + _chart(rows)


def _shipped_text(tasks: Sequence[str]) -> str:
    """The titles this person shipped, capped. Empty when none."""
    shown = [escape(t) for t in tasks[:_MAX_TASKS_SHOWN]]
    if not shown:
        return ""
    rest = len(tasks) - len(shown)
    text = " &middot; ".join(shown)
    if rest > 0:
        text += f" +{rest} more"
    return text


def _member_parts(member: EodDepartmentMember) -> list[str]:
    """Task figures for one person: what the day closed and what is still on
    the board. Time on the machine is deliberately not here: the admins asked
    for tasks and blockers, not hours."""
    parts: list[str] = []
    if member.tasks_done:
        parts.append(f"{member.tasks_done} done")
    if member.tasks_open:
        parts.append(f"{member.tasks_open} open{_overdue_note(member.tasks_overdue)}")
    return parts


def _member_row(
    member: EodDepartmentMember, *, last: bool, narrative: EodNarrative | None = None
) -> str:
    """One person's row. With `narrative` (only ever passed by the team and
    department rollups, whose recipient is in the person's personal scope) the
    row also carries their own account of the day and their blockers, attributed."""
    tone, label = _COVERAGE_PILL.get(member.coverage, _COVERAGE_PILL["missing"])
    title = escape(member.name)
    if member.job_title:
        title += (
            f' <span style="font-weight:400;color:{_REPORT_META};">&middot; '
            f"{escape(member.job_title)}</span>"
        )
    lines: list[str] = []
    if narrative is not None and narrative.summary.strip():
        lines.extend(
            _line(escape(para), color=_REPORT_INK)
            for para in narrative.summary.split("\n\n")
            if para.strip()
        )
    shipped = _shipped_text(member.tasks)
    if shipped:
        lines.append(_line(f"Shipped: {shipped}", color=_REPORT_INK))
    parts = _member_parts(member)
    if parts:
        lines.append(_line(" &middot; ".join(parts), color=_REPORT_META, size=13, top=4))
    if narrative is not None:
        lines.extend(
            f'<div style="padding-top:8px;">{_pill("Blocked", _TONE_ATTENTION)} '
            f'<span style="font-size:14px;line-height:1.55;color:{_REPORT_INK};">'
            f"{escape(blocker)}</span></div>"
            for blocker in narrative.highlights.blockers
            if blocker.strip()
        )
    return _row(title, _pill(label, tone), lines, last=last)


def _department_parts(digest: EodDepartmentDigest) -> list[str]:
    """What the day closed and what the department still carries."""
    parts: list[str] = []
    if digest.totals.tasks_done:
        parts.append(f"{digest.totals.tasks_done} done")
    if digest.open_tasks:
        parts.append(f"{digest.open_tasks} open{_overdue_note(digest.overdue_tasks)}")
    return parts


def _blocker_rows(blockers: Sequence[str]) -> str:
    """The department's blockers under its roster: a Blocked pill and the text.
    Unattributed on purpose - "what is holding this department up" is
    operational, where "what X is stuck on" is personal."""
    if not blockers:
        return ""
    rows = [
        _row(
            "",
            _pill("Blocked", _TONE_ATTENTION),
            [_line(escape(text), color=_REPORT_INK)],
            last=i == len(blockers) - 1,
        )
        for i, text in enumerate(blockers)
    ]
    return _eyebrow("Blockers", _TONE_ATTENTION[1], padding="20px 0 0") + _rows(rows)


def _coverage_block(digest: EodDepartmentDigest, *, indent: int) -> str:
    """The group's coverage bar with the counts in words under it."""
    return (
        f'<div style="padding:10px 0 0 {indent}px;">'
        f"{_stacked_bar(_coverage_segments(digest), digest.member_count, height=8)}"
        f'<div style="font-size:12px;line-height:1.5;color:{_REPORT_META};padding-top:6px;">'
        f"{_coverage_words(digest)}</div></div>"
    )


def _department_block(digest: EodDepartmentDigest, *, first: bool) -> str:
    members = digest.members
    rows = [_member_row(m, last=i == len(members) - 1) for i, m in enumerate(members)]
    return _block(
        _heading(escape(digest.department), f"{digest.submitted} of {digest.member_count} reported")
        + _coverage_block(digest, indent=13)
        + _figures(_department_parts(digest))
        + _rows(rows)
        + _blocker_rows(digest.blockers),
        padding="26px 20px 0" if first else "30px 20px 0",
        anchor=_department_anchor(digest.department),
    )


def _attention_items(
    digests: Sequence[EodDepartmentDigest], *, overdue: int, labelled: bool = True
) -> list[str]:
    """The lines of the "Needs attention" box: per department, who still owes a
    report and why; then blockers and where; then overdue work. Names stay in
    roster order and are capped, so a company-wide list cannot run forever."""
    items: list[str] = []
    named = 0
    unnamed = 0
    for digest in digests:
        sentences: list[str] = []
        for band, singular, plural in _ATTENTION_VERBS:
            names = [m.name for m in digest.members if m.coverage == band]
            if not names:
                continue
            room = _MAX_OWED_NAMED - named
            if room <= 0:
                unnamed += len(names)
                continue
            shown = [escape(n) for n in names[:room]]
            hidden = len(names) - len(shown)
            named += len(shown)
            who = _join_names(shown) + (f" and {hidden} more" if hidden else "")
            sentences.append(f"{who} {plural if len(names) > 1 else singular}.")
        if sentences and labelled:
            items.append(f"{escape(digest.department)}: {' '.join(sentences)}")
        elif sentences:
            items.extend(sentences)
    if unnamed:
        items.append(f"{_count('more person', unnamed)} still owe a report.")
    raised = [(escape(d.department), len(d.blockers)) for d in digests if d.blockers]
    if raised:
        total = sum(n for _, n in raised)
        where = f" in {_join_names([d for d, _ in raised])}" if labelled else ""
        items.append(f"{_count('blocker', total)} raised{where}.")
    if overdue:
        items.append(
            f"{_count('task', overdue)} {'is' if overdue == 1 else 'are'} overdue"
            f"{' across the company' if labelled else ''}."
        )
    return items


def eod_admin_digest_email(
    *,
    digests: Sequence[EodDepartmentDigest],
    report_date: str,
    dashboard_url: str | None = None,
) -> tuple[str, str]:
    """Render the (subject, html) end-of-day rollup sent to the admins: the whole
    company first, then a section per department.

    One email where there used to be one per department. Ten near-identical
    messages landing at six o'clock answered "how did the day go" no faster
    than one, and the inbox count alone read as noise; the department sections
    are all still here, after the glance figures, a coverage chart that says
    which section needs opening, a tasks-shipped chart, and before a dark box
    that lists what needs a decision.

    Deliberately carries NO narratives and NO time on the machine: coverage,
    the tasks shipped and still open, and each department's blockers. The
    admins asked for tasks and blockers, not hours. A person's own account of
    their day goes to their reporting manager only (see EodService's module
    docstring), so there is no `summary` field anywhere in here to leak one.
    """
    day = _day_label(report_date)
    member_count = sum(d.member_count for d in digests)
    submitted = sum(d.submitted for d in digests)
    owed = sum(d.pending + d.missing for d in digests)
    done = sum(d.totals.tasks_done for d in digests)
    blocker_count = sum(len(d.blockers) for d in digests)
    open_tasks = sum(d.open_tasks for d in digests)
    overdue = sum(d.overdue_tasks for d in digests)
    departments = _count("department", len(digests))

    subject = f"EOD · {day} · {submitted}/{member_count} reported"
    glance = _glance(
        [
            (str(submitted), "Reported", _TONE_DONE),
            (str(done), "Done", _TONE_ACTIVE),
            (str(blocker_count), "Blockers", _TONE_ATTENTION),
            (str(owed), "Still owed", _TONE_NEUTRAL),
        ]
    )
    sentences = [f"{submitted} of {member_count} reported across {departments}."]
    if submitted == 0:
        sentences.append("No reports came in yet.")
    if done:
        sentences.append(f"{_count('task', done)} shipped.")
    if open_tasks:
        tail = f", {overdue} overdue." if overdue else "."
        sentences.append(f"{_count('task', open_tasks)} still open{tail}")
    attention = _attention_items(digests, overdue=overdue)
    all_clear = not attention
    if all_clear:
        attention = ["Everyone expected to report did, and no blockers were raised."]
    blocks = [
        _report_header(
            title="End of Day Report",
            date_line=f"{escape(_long_day_label(report_date))} &middot; All departments",
        ),
        _block(glance + _sentence(" ".join(sentences)), padding="24px 20px 4px"),
        _block(_coverage_chart(digests), padding="22px 20px 0"),
        _block(_shipped_chart(digests), padding="22px 20px 0"),
        _block(
            _lead(
                "Reports below, grouped by department: who reported, what they shipped, "
                "and what is blocking them."
            ),
            padding="18px 20px 0",
        ),
        *(_department_block(d, first=i == 0) for i, d in enumerate(digests)),
        _block(
            _dark_list("Needs attention", attention, numbered=not all_clear), padding="34px 20px 0"
        ),
        # Every other Avora email offers a way back into the product; without
        # this the rollup is a dead end - you can see a department is behind
        # and have nowhere to click.
        _block(_report_button("Open in Avora", dashboard_url)) if dashboard_url else "",
    ]
    return subject, _report_layout(
        preheader=f"{departments} · {submitted} of {member_count} reported · {done} tasks shipped",
        blocks=blocks,
        footer=_ADMIN_DIGEST_FOOTER,
    )


# -- Team / department rollup (one group, with narratives) ------------------ #

_TEAM_DIGEST_FOOTER = (
    "You're receiving this because these people report to you in Avora. Each of "
    "them was also sent their own report."
)
_DEPARTMENT_DIGEST_FOOTER = (
    "You're receiving this because you head the {department} department in Avora. "
    "Each person was also sent their own report."
)


def eod_group_digest_email(
    *,
    kind: str,
    label: str,
    digest: EodDepartmentDigest,
    narratives: Mapping[uuid.UUID, EodNarrative],
    report_date: str,
    dashboard_url: str | None = None,
) -> tuple[str, str]:
    """Render the (subject, html) end-of-day rollup for one group: a manager's
    team (`kind="team"`, `label` is the manager's name) or a department
    (`kind="department"`, `label` is its name).

    Same format as the company rollup, for one group, plus each person's own
    account of their day and their blockers under their row. That is allowed
    here and nowhere else: the recipient is the group's direct manager or its
    department head, who is in every member's personal EOD scope. Only members
    of `digest` are rendered, whatever else `narratives` holds.
    """
    day = _day_label(report_date)
    n, submitted = digest.member_count, digest.submitted
    if kind == "team":
        subject = f"Team EOD · {day} · {submitted}/{n} reported"
        title = "Your team"
        context = f"Team report for {escape(label)}"
        footer = _TEAM_DIGEST_FOOTER
    else:
        subject = f"{label} EOD · {day} · {submitted}/{n} reported"
        title = escape(label)
        context = "Department report"
        footer = _DEPARTMENT_DIGEST_FOOTER.format(department=escape(label))
    done = digest.totals.tasks_done
    owed = digest.pending + digest.missing
    glance = _glance(
        [
            (str(submitted), "Reported", _TONE_DONE),
            (str(done), "Done", _TONE_ACTIVE),
            (str(digest.totals.blockers), "Blockers", _TONE_ATTENTION),
            (str(owed), "Still owed", _TONE_NEUTRAL),
        ]
    )
    sentences = [f"{submitted} of {n} reported."]
    if submitted == 0:
        sentences.append("No reports came in yet.")
    if done:
        sentences.append(f"{_count('task', done)} shipped.")
    if digest.open_tasks:
        tail = f", {digest.overdue_tasks} overdue." if digest.overdue_tasks else "."
        sentences.append(f"{_count('task', digest.open_tasks)} still open{tail}")
    members = digest.members
    rows = [
        _member_row(m, last=i == len(members) - 1, narrative=narratives.get(m.employee_id))
        for i, m in enumerate(members)
    ]
    attention = _attention_items([digest], overdue=digest.overdue_tasks, labelled=False)
    all_clear = not attention
    if all_clear:
        attention = ["Everyone expected to report did, and no blockers were raised."]
    blocks = [
        _report_header(
            title=title, date_line=f"{escape(_long_day_label(report_date))} &middot; {context}"
        ),
        _block(glance + _sentence(" ".join(sentences)), padding="24px 20px 4px"),
        _block(_coverage_block(digest, indent=0), padding="12px 20px 0"),
        _block(_rows(rows), padding="16px 20px 0"),
        _block(
            _dark_list("Needs attention", attention, numbered=not all_clear), padding="34px 20px 0"
        ),
        _block(_report_button("Open in Avora", dashboard_url)) if dashboard_url else "",
    ]
    return subject, _report_layout(
        preheader=f"{submitted} of {n} reported · {done} tasks shipped",
        blocks=blocks,
        footer=footer,
    )


# -- Resignation ------------------------------------------------------------ #


def resignation_submitted_email(
    *,
    recipient_name: str,
    resigner_name: str,
    last_working_label: str,
    reason: str | None,
    url: str,
) -> tuple[str, str]:
    """(subject, html) telling HR/Admin a resignation was submitted to review."""
    subject = f"Resignation submitted — {resigner_name}"
    reason_html = _note_block(reason) if reason else ""
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  People · Resignation
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Resignation to review
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {recipient_name}, <strong>{resigner_name}</strong> has submitted a resignation with a last
  working day of <strong>{last_working_label}</strong>.
</p>
{reason_html}
<p style="margin:0 0 4px;">{_button("Review resignation", url)}</p>"""
    return subject, _layout(
        preheader=f"{resigner_name} submitted a resignation (last day {last_working_label})",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def resignation_decision_email(
    *,
    employee_name: str,
    accepted: bool,
    last_working_label: str,
    decided_by: str,
    note: str | None,
    url: str,
) -> tuple[str, str]:
    """(subject, html) telling an employee their resignation was decided."""
    verb = "accepted" if accepted else "declined"
    subject = f"Your resignation was {verb}"
    note_html = _note_block(note) if note else ""
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  People · Resignation
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Resignation {verb}
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  Hi {employee_name}, <strong>{decided_by}</strong> {verb} your resignation with a last working
  day of <strong>{last_working_label}</strong>.
</p>
{note_html}
<p style="margin:0 0 4px;">{_button("View resignation", url)}</p>"""
    return subject, _layout(
        preheader=f"{decided_by} {verb} your resignation",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


# -- Celebrations (broadcast to the whole team) ----------------------------- #


def _celebration_email(*, eyebrow: str, heading: str, body: str) -> tuple[str, str]:
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  {eyebrow}
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {heading}
</h1>
<p style="margin:0 0 4px;font-size:15px;line-height:1.65;color:{_INK};">{body}</p>"""
    return heading, _layout(preheader=heading, content_html=content, footer=_ACTIVITY_FOOTER)


def birthday_email(*, person_name: str) -> tuple[str, str]:
    return _celebration_email(
        eyebrow="People · Celebration",
        heading=f"🎂 Happy Birthday, {person_name}!",
        body=(
            f"It's <strong>{person_name}</strong>'s birthday today. Wishing you a wonderful year "
            "ahead — from everyone at the team. 🎉"
        ),
    )


def anniversary_email(*, person_name: str, years: int) -> tuple[str, str]:
    label = "year" if years == 1 else "years"
    return _celebration_email(
        eyebrow="People · Work Anniversary",
        heading=f"Happy Work Anniversary — {person_name}",
        body=(
            f"Congratulations <strong>{person_name}</strong> on completing "
            f"<strong>{years} {label}</strong> with us! Thank you for everything you bring to the "
            "team — here's to many more. 🎊"
        ),
    )


def announcement_email(
    *, message: str, posted_by: str, level: str, dashboard_url: str
) -> tuple[str, str]:
    """Render the (subject, html) mirroring an HR announcement to inboxes.

    Escaped — the message is free-form text typed by a person, and it is about to
    be sent to the whole company.
    """
    urgent = level in ("warning", "critical")
    subject = f"{'Important — ' if urgent else ''}Announcement from {posted_by}"
    accent = _AMBER if urgent else _MUTED
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{accent};">
  {"Important" if urgent else "Announcement"}
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {escape(message)}
</h1>
<p style="margin:0 0 20px;font-size:14px;line-height:1.6;color:{_MUTED};">
  Posted by {escape(posted_by)}.
</p>
<p style="margin:0 0 4px;">{_button("Open Avora", dashboard_url)}</p>"""
    return subject, _layout(
        preheader=message[:120],
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def holiday_reminder_email(*, holiday_name: str, day_label: str) -> tuple[str, str]:
    """Render the (subject, html) reminding everyone of tomorrow's holiday.

    Sent to the whole team — people on leave included, since "the office is closed
    tomorrow" is exactly as relevant to someone already away as to someone due in.
    """
    subject = f"Holiday tomorrow — {holiday_name}"
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  Time off
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  🎉 {escape(holiday_name)} — the office is closed tomorrow
</h1>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">
  A reminder that {_chip(escape(day_label))} is a company holiday. No need to clock
  in — enjoy the day.
</p>"""
    return subject, _layout(
        preheader=f"{holiday_name} is tomorrow ({day_label}) — the office is closed.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def festival_email(*, festival_name: str, message: str) -> tuple[str, str]:
    return _celebration_email(
        eyebrow="Celebration",
        heading=f"{festival_name} 🎉",
        body=message,
    )


# -- Probation outcome letters ---------------------------------------------- #
# Wording supplied by HR (Optiminastic probation templates). Keep the copy as
# written: it is the company's language, not ours. Only the structure around it
# is Avora's, and every interpolated value is escaped.

# One legal name for the emails and the PDF letters they carry.
_COMPANY = COMPANY_LEGAL_NAME
_GLASSDOOR = "https://www.glassdoor.co.in"

_PROBATION_FOOTER = (
    f"Regards,<br/>HR Team<br/>{_COMPANY}<br/><br/>"
    "Sent from Avora, the Optiminastic people workspace."
)


def _para(text: str) -> str:
    return f'<p style="margin:0 0 14px;font-size:15px;line-height:1.65;color:{_INK};">{text}</p>'


def _bullet(label: str, body: str) -> str:
    return (
        f'<li style="margin:0 0 8px;font-size:14.5px;line-height:1.6;color:{_INK};">'
        f"<strong>{label}:</strong> {body}</li>"
    )


def probation_confirmation_email(
    *,
    employee_name: str,
    job_title: str,
    effective_label: str,
    has_letter: bool,
) -> tuple[str, str]:
    """(subject, html) confirming someone's employment after probation."""
    name, title = escape(employee_name), escape(job_title)
    effective = escape(effective_label)
    # Only claim an attachment when one is actually going out. An email that says
    # "attached" with nothing attached is worse than one that stays quiet.
    letter = (
        _para(
            "Your official Probation Confirmation Letter is attached to this email for "
            "your reference and records."
        )
        if has_letter
        else ""
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{
        _MUTED
    };">
  People · Probation
</div>
<h1 style="margin:8px 0 16px;font-family:{
        _SERIF
    };font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Your probation is complete
</h1>
{_para(f"Dear {name},")}
{_para("We are delighted to share some good news with you!")}
{
        _para(
            f"Following the successful completion of your probation period, we are pleased to confirm "
            f"your position as <strong>{title}</strong> at {_COMPANY}, effective <strong>{effective}</strong>."
        )
    }
{
        _para(
            "Over the course of your probation, we have had the opportunity to see your contribution, "
            "commitment, and potential as part of the Optiminastic team. Reaching this milestone is not "
            "just the completion of your probation period - it marks the beginning of the next phase of "
            "your journey with us."
        )
    }
{
        _para(
            "We truly appreciate the effort you have put into your role so far, and we look forward to "
            "seeing you continue to learn, take greater ownership, grow professionally, and contribute "
            "to the larger goals of the organisation."
        )
    }
<h2 style="margin:24px 0 10px;font-family:{
        _SERIF
    };font-size:18px;line-height:1.3;font-weight:600;color:{_INK};">
  What changes after your confirmation
</h2>
{
        _para(
            "With effect from your confirmation date, the following employment terms and benefits will "
            "become applicable to you, in accordance with the Company's policies:"
        )
    }
<ul style="margin:0 0 14px;padding-left:20px;">
  {_bullet("Notice Period", "Your applicable notice period will now be 30 days.")}
  {
        _bullet(
            "Leave Eligibility",
            "You will now be eligible for the applicable paid leave benefits as per the "
            "Company's Leave Policy.",
        )
    }
  {
        _bullet(
            "Health Insurance",
            "You will become eligible for the Company's health insurance benefits, subject to "
            "the applicable policy terms and enrolment guidelines.",
        )
    }
  {
        _bullet(
            "Other Benefits",
            "Any other benefits applicable to confirmed employees will be governed by the "
            "prevailing Company policies.",
        )
    }
</ul>
{letter}
{
        _para(
            "We hope your journey with Optiminastic continues to be one filled with opportunities, "
            "learning, meaningful work, new responsibilities, and shared successes. As we grow as an "
            "organisation, we hope you will grow with us and play an increasingly important role in "
            "what we build together."
        )
    }
{
        _para(
            "We would also love to hear about your experience of working with Optiminastic so far. If "
            f'you would like to share your feedback, you can do so on <a href="{_GLASSDOOR}" '
            f'style="color:{_VIOLET};">Glassdoor</a>.'
        )
    }
{
        _para(
            "Once again, congratulations on successfully completing your probation and becoming a "
            "confirmed member of the Optiminastic team!"
        )
    }
{
        _para(
            "Thank you for your hard work, commitment, and contribution. We are excited to have you "
            "continue this journey with us."
        )
    }"""
    return (
        "Congratulations - your probation is confirmed",
        _layout(
            preheader=f"Your position as {title} is confirmed, effective {effective}",
            content_html=content,
            footer=_PROBATION_FOOTER,
        ),
    )


def probation_extension_email(
    *,
    employee_name: str,
    new_end_label: str,
    has_letter: bool,
) -> tuple[str, str]:
    """(subject, html) telling someone their probation has been extended."""
    name, new_end = escape(employee_name), escape(new_end_label)
    letter = (
        _para(
            "Please find the Probation Period Extension Letter attached, outlining the "
            "details and expectations during the extended period."
        )
        if has_letter
        else ""
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{
        _MUTED
    };">
  People · Probation
</div>
<h1 style="margin:8px 0 16px;font-family:{
        _SERIF
    };font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  Your probation has been extended
</h1>
{_para(f"Dear {name},")}
{
        _para(
            "Following the review of your performance during your probation period, we would like to "
            f"inform you that your probation has been extended until <strong>{new_end}</strong>."
        )
    }
{
        _para(
            "Your performance will be reviewed periodically during this extended period, and a final "
            "decision regarding your employment status will be communicated upon completion of the "
            "extended probation period."
        )
    }
{letter}
{
        _para(
            "We encourage you to use this extended period as an opportunity to work on the identified "
            "areas and demonstrate your capabilities."
        )
    }
{
        _para(
            "Should you have any questions or require any guidance, please feel free to reach out to "
            "your reporting manager or HR."
        )
    }
{_para("We look forward to your progress and continued efforts.")}"""
    return (
        "Your probation period has been extended",
        _layout(
            preheader=f"Your probation is extended until {new_end}",
            content_html=content,
            footer=_PROBATION_FOOTER,
        ),
    )
