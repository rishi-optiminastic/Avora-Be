"""Branded Avora email templates.

A single inline-styled layout (`_layout`) is the source of truth for how every
Avora email looks — new emails render their body through it so the brand stays
consistent everywhere. There is no inner card: content sits directly on the
paper background. Radii follow the product's rounded-md (8px). The wordmark and
headings ask for Reckless (the product serif) with a Georgia fallback, since
most email clients can't load a custom web font.
"""

from __future__ import annotations

import re
from datetime import date
from html import escape
from typing import TYPE_CHECKING

from app.core.probation_letter_pdf import COMPANY_LEGAL_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from app.schemas.eod import EodDepartmentDigest, EodDepartmentMember, EodDraftContent

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


# The shell's default column width. Wide enough for prose, narrow enough to read
# on a phone without zooming. A template with a real table (the department
# roster) asks for `_WIDE` so its columns are not squeezed into one another.
_WIDTH = 460
_WIDTH_WIDE = 600

# Phone-width adjustments. Every Avora email is fluid already (percentage
# widths, no fixed pixel columns); this trims the chrome so a 360px screen
# spends its width on the content rather than on padding. Gmail, Apple Mail and
# Outlook mobile honour an embedded media query; a client that strips <style>
# still gets the fluid layout, just with desktop padding.
_RESPONSIVE_CSS = """\
@media only screen and (max-width: 600px) {
  .shell { padding: 22px 12px !important; }
  .h1 { font-size: 22px !important; }
  .hero { font-size: 34px !important; }
  .tile { padding: 12px 4px !important; }
  .tile-value { font-size: 20px !important; }
}"""


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
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="x-apple-disable-message-reformatting">
    <style>{_RESPONSIVE_CSS}</style>
  </head>
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


def _eyebrow(text: str) -> str:
    return (
        f'<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:{_MUTED};">{text}</div>'
    )


def _section_label(text: str, color: str = _MUTED) -> str:
    return (
        f'<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:{color};margin:0 0 8px;">{text}</div>'
    )


def _fmt_hours(minutes: int) -> str:
    """Compact worked-time label: '45m' under an hour, else '6.7h'."""
    if minutes < 60:
        return f"{max(0, minutes)}m"
    return f"{minutes / 60:.1f}h"


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


def _stat_cell(value: str, label: str, *, accent: bool = False) -> str:
    """One stat tile in the three-up report-card row."""
    bg = _VIOLET_SOFT if accent else _CARD
    value_color = _VIOLET if accent else _INK
    return (
        f'<td width="33.33%" valign="top" style="padding:0 4px;">'
        f'<div class="tile" style="background:{bg};border:1px solid {_LINE};border-radius:10px;'
        f'padding:14px 8px;text-align:center;">'
        f'<div class="tile-value" style="font-family:{_SERIF};font-size:24px;font-weight:600;'
        f'line-height:1;color:{value_color};font-variant-numeric:tabular-nums;">{value}</div>'
        f'<div style="font-size:10px;font-weight:600;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{_MUTED};margin-top:7px;">{label}</div>'
        f"</div></td>"
    )


def _stat_row(worked_minutes: int, active_pct: int, tasks_done: int) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:18px 0 4px;"><tr>'
        f"{_stat_cell(_fmt_hours(worked_minutes), 'Worked')}"
        f"{_stat_cell(f'{active_pct}%', 'Active', accent=True)}"
        f"{_stat_cell(str(tasks_done), 'Done')}"
        "</tr></table>"
    )


def _meter(pct: int, color: str, *, height: int = 8) -> str:
    """A slim proportion bar - table cells, so Outlook draws it too."""
    pct = max(0, min(100, pct))
    rest = 100 - pct
    fill = (
        f'<td style="width:{pct}%;background:{color};height:{height}px;'
        f'border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>'
        if pct
        else ""
    )
    gap = (
        f'<td style="width:{rest}%;height:{height}px;font-size:0;line-height:0;">&nbsp;</td>'
        if rest
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_VIOLET_SOFT};border-radius:6px;"><tr>{fill}{gap}</tr></table>'
    )


def _active_bar(active_pct: int, worked_minutes: int, active_minutes: int) -> str:
    """The active-share meter with its caption, under the stat tiles."""
    return (
        '<div style="margin:8px 0 18px;">'
        f"{_meter(active_pct, _VIOLET)}"
        f'<div style="font-size:11px;color:{_MUTED};margin-top:6px;">'
        f"{active_minutes} min actively engaged of {worked_minutes} min on the machine"
        "</div></div>"
    )


def _pill(text: str) -> str:
    """A spaced, wrapping pill — like `_chip` but with margin for chip rows."""
    return (
        f'<span style="display:inline-block;background:{_VIOLET_SOFT};color:{_VIOLET};'
        f"font-size:12px;font-weight:600;padding:4px 11px;border-radius:8px;"
        f'margin:0 6px 6px 0;">{text}</span>'
    )


def _pill_row(label: str, items: list[str]) -> str:
    if not items:
        return ""
    pills = "".join(_pill(i) for i in items)
    return f'<div style="margin:0 0 16px;">{_section_label(label)}<div>{pills}</div></div>'


def _check_list(label: str, items: list[str]) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<tr><td valign="top" style="color:{_VIOLET};font-size:14px;font-weight:700;'
        f'padding:0 9px 7px 0;line-height:1.5;">&#10003;</td>'
        f'<td style="font-size:14px;line-height:1.5;color:{_INK};padding:0 0 7px;">{i}</td></tr>'
        for i in items
    )
    return (
        f'<div style="margin:0 0 16px;">{_section_label(label)}'
        f'<table role="presentation" cellpadding="0" cellspacing="0">{rows}</table></div>'
    )


def _blocker_block(items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:0 0 4px;font-size:13.5px;line-height:1.5;color:{_AMBER_INK};">{i}</li>'
        for i in items
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:2px 0 16px;"><tr><td style="background:'
        f'{_AMBER_SOFT};border:1px solid {_AMBER_LINE};border-radius:10px;padding:12px 16px;">'
        f"{_section_label('&#9888; Blockers', _AMBER)}"
        f'<ul style="margin:0;padding-left:18px;">{lis}</ul></td></tr></table>'
    )


def eod_report_email(
    *,
    employee_name: str,
    date_label: str,
    summary: str,
    highlights: EodDraftContent,
    worked_minutes: int = 0,
    active_minutes: int = 0,
    tasks_done: int = 0,
) -> tuple[str, str]:
    """Render the (subject, html) End-of-Day report sent to the reporting manager.

    Report-card layout: a three-up stat row (worked / active / done), an active-
    share bar, the narrative, then Worked-on pills, a Completed checklist, and any
    Blockers in an amber callout. Stats fall back to 0 cleanly when a signal is
    missing, so a quiet day still renders a clean card."""
    # `date_label` arrives as the raw local YYYY-MM-DD from the report row.
    day = _day_label(date_label)
    subject = f"End of day · {employee_name} · {day}"
    active_pct = _active_pct(active_minutes, worked_minutes)
    # Escaped: the narrative is free text the employee edits, and the highlight
    # lists come back from the model. Unescaped, a stray "<" silently swallowed
    # the rest of the paragraph in the manager's inbox - and anything shaped like
    # a tag was injected straight into the message body.
    summary_html = "".join(
        f'<p style="margin:0 0 12px;font-size:15px;line-height:1.6;color:{_INK};">'
        f"{escape(para)}</p>"
        for para in summary.split("\n\n")
        if para.strip()
    )
    bar_html = _active_bar(active_pct, worked_minutes, active_minutes) if worked_minutes > 0 else ""
    confidence = max(0, min(100, highlights.confidence or 0))
    confidence_html = (
        f'<div style="margin:20px 0 0;padding-top:14px;border-top:1px solid {_LINE};'
        f'font-size:11px;color:{_MUTED};">Signal confidence {confidence}% — '
        "inferred from tasks, activity, and on-screen context.</div>"
        if confidence
        else ""
    )
    content = f"""\
{_eyebrow(f"End of day · {escape(day)}")}
<h1 class="h1" style="margin:8px 0 0;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {escape(employee_name)}
</h1>
{_stat_row(worked_minutes, active_pct, tasks_done)}
{bar_html}
{summary_html}
{_pill_row("Worked on", [escape(i) for i in highlights.worked_on])}
{_check_list("Completed", [escape(i) for i in highlights.tasks_completed])}
{_blocker_block([escape(i) for i in highlights.blockers])}
{confidence_html}"""
    return subject, _layout(
        preheader=f"{escape(employee_name)}'s end-of-day summary for {escape(day)}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


# -- EOD admin rollup (every department, one email) ------------------------- #
# Submission-rate bands. The same thresholds and hex values the dashboard uses
# (fe/.../department-eod `bandColor`), so a department that reads amber on screen
# reads amber in the inbox. A flat brand colour here made 0% and 100% look
# identical at a glance, which is the one thing the number is for.
_BAND_GOOD = "#1f9d55"
_BAND_WATCH = "#e0a106"
_BAND_POOR = "#e5556a"
_BAND_NONE = "#a0a0ac"


def _band_color(pct: int) -> str:
    if pct >= 75:
        return _BAND_GOOD
    if pct >= 55:
        return _BAND_WATCH
    if pct > 0:
        return _BAND_POOR
    return _BAND_NONE


# Coverage band → (background, ink, label). Mirrors the product's coverage
# pastels (fe/features/eod/types.ts EOD_COVERAGE_META) in literal hex, because
# an email client cannot resolve a CSS custom property.
_COVERAGE_EMAIL: dict[str, tuple[str, str, str]] = {
    "submitted": ("#e4f0e8", "#1f4d2e", "Submitted"),
    "pending": (_AMBER_SOFT, _AMBER_INK, "Pending"),
    "absent": ("#fbe9ec", "#7a2424", "Absent"),
    "failed": ("#fbe9ec", "#7a2424", "Failed"),
    "missing": ("#efedf4", _MUTED, "No report"),
}
# Healthy → attention, left to right: the order the coverage bar paints in.
_COVERAGE_ORDER = ("submitted", "pending", "failed", "absent", "missing")
# People who could have reported and have not. Absent is not owed: someone who
# was not at work legitimately has no report, and dunning them for it is how a
# tool starts feeling like surveillance rather than a summary. Mirrors the
# dashboard's "Still owed" row (fe/.../team-eod `_digest.tsx`).
_OWED_BANDS = ("pending", "missing")
# How many owed names to print before summarising the rest. Across a whole
# company the list can run long, and past this it stops being read.
_MAX_OWED_NAMED = 12
# How many shipped titles to name per person before summarising the rest. A
# twenty-task day must not turn one roster row into half the email.
_MAX_TASKS_SHOWN = 3


def _coverage_pill(coverage: str) -> str:
    bg, ink, label = _COVERAGE_EMAIL.get(coverage, _COVERAGE_EMAIL["missing"])
    return (
        f'<span style="display:inline-block;background:{bg};color:{ink};font-size:10px;'
        f'font-weight:600;padding:2px 7px;border-radius:6px;white-space:nowrap;">{label}</span>'
    )


def _coverage_counts(rollups: Iterable[EodDepartmentDigest]) -> dict[str, int]:
    """Per-band head counts summed over `rollups` - one department, or every
    department for the company-wide bar."""
    counts = dict.fromkeys(_COVERAGE_ORDER, 0)
    for rollup in rollups:
        for band in _COVERAGE_ORDER:
            counts[band] += int(getattr(rollup, band))
    return counts


def _coverage_bar(counts: Mapping[str, int], member_count: int) -> str:
    """Proportion bar over the people counted, healthy → attention, left to
    right. Table cells rather than divs so Outlook renders it."""
    total = member_count or 1
    cells = "".join(
        f'<td style="width:{counts[band] / total * 100:.4f}%;background:{_COVERAGE_EMAIL[band][0]};'
        f'height:8px;font-size:0;line-height:0;">&nbsp;</td>'
        for band in _COVERAGE_ORDER
        if counts[band] > 0
    )
    if not cells:
        cells = '<td style="width:100%;background:#efedf4;height:8px;font-size:0;line-height:0;">&nbsp;</td>'
    legend = "".join(
        f'<span style="display:inline-block;margin:0 12px 0 0;font-size:11px;color:{_MUTED};">'
        f'<span style="display:inline-block;width:8px;height:8px;border-radius:8px;'
        f'background:{_COVERAGE_EMAIL[band][0]};"></span> '
        f'<strong style="color:{_INK};">{counts[band]}</strong> {_COVERAGE_EMAIL[band][2].lower()}</span>'
        for band in _COVERAGE_ORDER
        if counts[band] > 0
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-radius:6px;overflow:hidden;margin:0 0 9px;"><tr>{cells}</tr></table>'
        f"<div>{legend}</div>"
    )


def _hero(pct: int, caption: str) -> str:
    """The one number you can read across the room, and what it counts."""
    return (
        '<div style="margin:14px 0 4px;">'
        f'<span class="hero" style="font-family:{_SERIF};font-size:40px;font-weight:600;'
        f"line-height:1;color:{_band_color(pct)};font-variant-numeric:tabular-nums;"
        f'">{pct}%</span>'
        f'<span style="font-size:12.5px;color:{_MUTED};margin-left:8px;">{caption}</span></div>'
    )


def _effort_block(
    *, worked_minutes: int, active_minutes: int, tasks_done: int, submitted: int
) -> str:
    """The three-up effort tiles, or a plain note when there is no effort to show.

    A day where nobody reported would otherwise render "0m worked, 0% active,
    0 done" - three measurements of zero, which reads as "they sat there doing
    nothing" rather than "no reports came in". The roster says `-` on every
    row; the headline has to agree.
    """
    if worked_minutes == 0 and submitted == 0:
        return (
            f'<div style="margin:18px 0 4px;padding:14px 16px;background:{_PAPER};'
            f"border:1px solid {_LINE};border-radius:10px;font-size:13px;"
            f'color:{_MUTED};">No reports came in, so there is nothing to total yet.</div>'
        )
    return _stat_row(worked_minutes, _active_pct(active_minutes, worked_minutes), tasks_done)


def _overdue_note(overdue: int) -> str:
    return (
        f', <span style="color:{_BAND_POOR};font-weight:600;">{overdue} overdue</span>'
        if overdue
        else ""
    )


def _workload_line(open_tasks: int, overdue_tasks: int, scope: str) -> str:
    """The standing workload, under the effort tiles.

    The tiles answer "what did today produce"; this answers "what is still
    waiting", which is the question a rollup of finished work otherwise leaves
    open. `scope` names whose: "the team" or "the company".
    """
    if not open_tasks:
        return ""
    return (
        f'<div style="margin:10px 0 0;font-size:12.5px;color:{_MUTED};">'
        f'<span style="font-weight:600;color:{_INK};">{open_tasks}</span> '
        f"still open across {scope}{_overdue_note(overdue_tasks)}</div>"
    )


def _still_owed(members: Iterable[EodDepartmentMember]) -> str:
    """The people who still owe a report, named.

    Resolves the question the roster alone leaves open: in a department of six
    you can spot the gaps, in a company of forty you cannot. Deliberately a
    summary line, not a re-sort of the roster: ordering people by who failed to
    report turns a daily digest into a leaderboard of shame, and alphabetical
    stays predictable week to week.
    """
    owed = [m.name for m in members if m.coverage in _OWED_BANDS]
    if not owed:
        return ""
    named = ", ".join(escape(n) for n in owed[:_MAX_OWED_NAMED])
    rest = len(owed) - _MAX_OWED_NAMED
    if rest > 0:
        named += f" and {rest} more"
    return (
        f'<div style="margin:10px 0 0;padding:9px 12px;background:{_PAPER};'
        f'border-radius:8px;font-size:12px;color:{_MUTED};">'
        f'<span style="font-weight:600;color:{_INK};">Still owed</span> &middot; {named}</div>'
    )


def _role_label(job_title: str | None) -> str:
    """The person's designation, muted, beside their name. Reading across six
    departments an admin needs to know who someone is."""
    if not job_title:
        return ""
    return (
        f'<span style="color:{_MUTED};font-size:11.5px;font-weight:400;"> '
        f"&middot; {escape(job_title)}</span>"
    )


def _shipped_line(tasks: list[str]) -> str:
    """The titles this person shipped, under their name. Empty when none."""
    if not tasks:
        return ""
    shown = [escape(t) for t in tasks[:_MAX_TASKS_SHOWN]]
    rest = len(tasks) - len(shown)
    text = " &middot; ".join(shown)
    if rest > 0:
        text += f' <span style="color:{_MUTED};">+{rest} more</span>'
    return (
        f'<div style="margin:3px 0 0;font-size:11.5px;line-height:1.45;'
        f'color:{_MUTED};">{text}</div>'
    )


def _figures_line(parts: list[str], margin: str) -> str:
    return (
        f'<div style="margin:{margin};font-size:11.5px;line-height:1.45;color:{_MUTED};'
        f'font-variant-numeric:tabular-nums;">{" &middot; ".join(parts)}</div>'
        if parts
        else ""
    )


def _member_figures(member: EodDepartmentMember) -> str:
    """One muted line of the day's figures under a person's name.

    This replaces the six-column table the rollup used to be. At phone width
    those columns never fit, so the client scaled the whole email down until
    nothing in it could be read; a line of labelled figures wraps instead of
    shrinking. Nothing is printed for a measurement that was not taken - never
    "0h", which reads as a measurement rather than an absence. Open and overdue
    come from the task board, not the report, so they show for everyone.
    """
    parts: list[str] = []
    if member.worked_minutes > 0:
        parts.append(f"{_fmt_hours(member.worked_minutes)} on the machine")
        parts.append(f"{member.active_pct}% active")
    if member.tasks_done:
        parts.append(f"{member.tasks_done} done")
    if member.tasks_open:
        parts.append(f"{member.tasks_open} open{_overdue_note(member.tasks_overdue)}")
    return _figures_line(parts, "3px 0 0")


def _roster_rows(members: Sequence[EodDepartmentMember]) -> str:
    """One row per person: name and designation with the coverage pill beside
    them, the day's figures and what they shipped underneath. Two cells only,
    so the row keeps its shape at 320px."""
    rows = ""
    for member in members:
        flag = (
            f' <span style="color:{_AMBER};font-weight:600;">&#9888;{member.blockers}</span>'
            if member.blockers
            else ""
        )
        rows += (
            f'<tr><td style="padding:9px 10px 9px 0;border-top:1px solid {_LINE};'
            f'font-size:13px;color:{_INK};vertical-align:top;">'
            f'<span style="font-weight:600;">{escape(member.name)}</span>'
            f"{_role_label(member.job_title)}{flag}"
            f"{_member_figures(member)}{_shipped_line(member.tasks)}</td>"
            f'<td style="padding:9px 0;border-top:1px solid {_LINE};text-align:right;'
            f'white-space:nowrap;vertical-align:top;">{_coverage_pill(member.coverage)}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:6px 0 0;">{rows}</table>'
    )


def _department_anchor(department: str) -> str:
    """A stable id for a department's section, so the index can link down."""
    return "dept-" + re.sub(r"[^a-z0-9]+", "-", department.lower()).strip("-")


def _department_index(digests: Sequence[EodDepartmentDigest]) -> str:
    """Every department on one line: name, a meter, the figure. It answers
    "which department do I need to look at" before any roster is read."""
    rows = ""
    for digest in digests:
        pct = round(digest.submission_rate * 100)
        rows += (
            f'<tr><td style="padding:7px 10px 7px 0;border-top:1px solid {_LINE};font-size:13px;">'
            f'<a href="#{_department_anchor(digest.department)}" style="color:{_INK};'
            f'font-weight:600;text-decoration:none;">{escape(digest.department)}</a></td>'
            f'<td width="34%" style="padding:7px 10px 7px 0;border-top:1px solid {_LINE};'
            f'vertical-align:middle;">{_meter(pct, _band_color(pct), height=6)}</td>'
            f'<td style="padding:7px 0;border-top:1px solid {_LINE};text-align:right;'
            f"white-space:nowrap;font-size:12.5px;color:{_MUTED};"
            f'font-variant-numeric:tabular-nums;">'
            f'<span style="font-weight:600;color:{_band_color(pct)};">{pct}%</span> '
            f"{digest.submitted}/{digest.member_count}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 6px;">{rows}</table>'
    )


def _department_figures(digest: EodDepartmentDigest) -> str:
    """One muted line under a department's heading: what the day produced and
    what the department still carries. Says so in words when nobody reported."""
    totals = digest.totals
    parts: list[str] = []
    if totals.worked_minutes > 0:
        parts.append(f"{_fmt_hours(totals.worked_minutes)} on machines")
        parts.append(f"{_active_pct(totals.active_minutes, totals.worked_minutes)}% active")
    elif digest.submitted == 0:
        parts.append("No reports came in")
    if totals.tasks_done:
        parts.append(f"{totals.tasks_done} done")
    if digest.open_tasks:
        parts.append(f"{digest.open_tasks} open{_overdue_note(digest.overdue_tasks)}")
    return _figures_line(parts, "4px 0 12px")


def _department_block(digest: EodDepartmentDigest) -> str:
    """One department's section: heading with its figure, the coverage bar, who
    still owes, the roster, and the department's blockers."""
    pct = round(digest.submission_rate * 100)
    blockers = _blocker_block([escape(b) for b in digest.blockers])
    blockers_html = f'<div style="margin:14px 0 0;">{blockers}</div>' if blockers else ""
    return (
        f'<div id="{_department_anchor(digest.department)}" '
        f'style="margin:26px 0 0;padding:18px 0 0;border-top:2px solid {_LINE};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:baseline;"><h2 style="margin:0;font-family:{_SERIF};'
        f'font-size:19px;line-height:1.2;font-weight:600;color:{_INK};">'
        f"{escape(digest.department)}</h2></td>"
        f'<td style="vertical-align:baseline;text-align:right;white-space:nowrap;">'
        f'<span style="font-family:{_SERIF};font-size:22px;font-weight:600;'
        f'color:{_band_color(pct)};font-variant-numeric:tabular-nums;">{pct}%</span>'
        f'<span style="font-size:12px;color:{_MUTED};margin-left:6px;">'
        f"{digest.submitted} of {digest.member_count} reported</span></td>"
        "</tr></table>"
        f"{_department_figures(digest)}"
        f"{_coverage_bar(_coverage_counts([digest]), digest.member_count)}"
        f"{_still_owed(digest.members)}"
        f"{_roster_rows(digest.members)}"
        f"{blockers_html}"
        "</div>"
    )


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
    are all still here, after an index that says which of them needs opening.

    Deliberately carries NO narratives: the anchor submission rate, the day's
    summed effort, a coverage bar, each roster with its figures, and each
    department's blockers. A person's own account of their day goes to their
    reporting manager only (see EodService's module docstring), so there is no
    `summary` field anywhere in here to leak one.
    """
    day = _day_label(report_date)
    member_count = sum(d.member_count for d in digests)
    submitted = sum(d.submitted for d in digests)
    # Per day, the same basis every rollup uses (`EodRollup.submission_rate`),
    # so the company figure and the department figures can never disagree.
    possible = sum(d.member_count * d.working_days for d in digests)
    pct = round(sum(d.submitted_days for d in digests) / possible * 100) if possible else 0
    worked = sum(d.totals.worked_minutes for d in digests)
    active = sum(d.totals.active_minutes for d in digests)
    done = sum(d.totals.tasks_done for d in digests)
    open_tasks = sum(d.open_tasks for d in digests)
    overdue = sum(d.overdue_tasks for d in digests)
    everyone = [m for d in digests for m in d.members]
    departments = f"{len(digests)} department{'s' if len(digests) != 1 else ''}"

    subject = f"EOD · {day} · {submitted}/{member_count} reported"
    # Every other Avora email offers a way back into the product; without this
    # the rollup is a dead end - you can see a department is behind and have
    # nowhere to click.
    open_html = (
        f'<p style="margin:22px 0 0;">{_button("Open in Avora", dashboard_url)}</p>'
        if dashboard_url
        else ""
    )
    content = f"""\
{_eyebrow(f"End of day &middot; {escape(day)}")}
<h1 class="h1" style="margin:8px 0 0;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  All departments
</h1>
{_hero(pct, f"{submitted} of {member_count} reported &middot; {departments}")}
{_effort_block(worked_minutes=worked, active_minutes=active, tasks_done=done, submitted=submitted)}
{_workload_line(open_tasks, overdue, "the company")}
<div style="margin:18px 0 16px;">{_section_label("Coverage")}{_coverage_bar(_coverage_counts(digests), member_count)}{_still_owed(everyone)}</div>
{_section_label("Departments")}
{_department_index(digests)}
{"".join(_department_block(d) for d in digests)}
{open_html}"""
    return subject, _layout(
        preheader=f"{departments} · {submitted} of {member_count} reported · {done} tasks shipped",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
        # Wider than the default shell: a roster row carries a name, a title and
        # a pill side by side, and at 460px they crowd each other on desktop.
        max_width=_WIDTH_WIDE,
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
