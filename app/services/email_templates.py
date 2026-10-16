"""Branded Avora email templates.

A single inline-styled layout (`_layout`) is the source of truth for how every
Avora email looks — new emails render their body through it so the brand stays
consistent everywhere. There is no inner card: content sits directly on the
paper background. Radii follow the product's rounded-md (8px). The wordmark and
headings ask for Reckless (the product serif) with a Georgia fallback, since
most email clients can't load a custom web font.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import TYPE_CHECKING

from app.core.probation_letter_pdf import COMPANY_LEGAL_NAME

if TYPE_CHECKING:
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
_WIDTH_WIDE = 560


def _layout(
    *,
    preheader: str,
    content_html: str,
    footer: str = _INVITE_FOOTER,
    max_width: int = _WIDTH,
) -> str:
    """Wrap an email body in the shared Avora shell — no card, on paper."""
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{_PAPER};font-family:{_SANS};-webkit-font-smoothing:antialiased;">
    <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</span>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAPER};padding:44px 20px;">
      <tr><td align="center">
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
        f'<div style="background:{bg};border:1px solid {_LINE};border-radius:10px;'
        f'padding:14px 8px;text-align:center;">'
        f'<div style="font-family:{_SERIF};font-size:24px;font-weight:600;line-height:1;'
        f'color:{value_color};font-variant-numeric:tabular-nums;">{value}</div>'
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


def _active_bar(active_pct: int, worked_minutes: int, active_minutes: int) -> str:
    """A slim violet progress bar for active share — email-safe (table cells)."""
    pct = max(0, min(100, active_pct))
    rest = max(0, 100 - pct)
    fill = (
        f'<td style="width:{pct}%;background:{_VIOLET};height:8px;'
        f'border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>'
    )
    gap = f'<td style="width:{rest}%;font-size:0;line-height:0;">&nbsp;</td>' if rest else ""
    return (
        '<div style="margin:8px 0 18px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_VIOLET_SOFT};border-radius:6px;"><tr>{fill}{gap}</tr></table>'
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
<h1 style="margin:8px 0 0;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
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


# -- EOD department rollup (to admins) -------------------------------------- #
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


def _coverage_pill(coverage: str) -> str:
    bg, ink, label = _COVERAGE_EMAIL.get(coverage, _COVERAGE_EMAIL["missing"])
    return (
        f'<span style="display:inline-block;background:{bg};color:{ink};font-size:10px;'
        f'font-weight:600;padding:2px 7px;border-radius:6px;white-space:nowrap;">{label}</span>'
    )


def _coverage_bar(digest: EodDepartmentDigest) -> str:
    """Proportion bar over the department, healthy → attention, left to right.
    Table cells rather than divs so Outlook renders it."""
    bands = (
        ("submitted", digest.submitted),
        ("pending", digest.pending),
        ("failed", digest.failed),
        ("absent", digest.absent),
        ("missing", digest.missing),
    )
    total = digest.member_count or 1
    cells = "".join(
        f'<td style="width:{count / total * 100:.4f}%;background:{_COVERAGE_EMAIL[band][0]};'
        f'height:8px;font-size:0;line-height:0;">&nbsp;</td>'
        for band, count in bands
        if count > 0
    )
    if not cells:
        cells = '<td style="width:100%;background:#efedf4;height:8px;font-size:0;line-height:0;">&nbsp;</td>'
    legend = "".join(
        f'<span style="display:inline-block;margin:0 12px 0 0;font-size:11px;color:{_MUTED};">'
        f'<span style="display:inline-block;width:8px;height:8px;border-radius:8px;'
        f'background:{_COVERAGE_EMAIL[band][0]};"></span> '
        f'<strong style="color:{_INK};">{count}</strong> {_COVERAGE_EMAIL[band][2].lower()}</span>'
        for band, count in bands
        if count > 0
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-radius:6px;overflow:hidden;margin:0 0 9px;"><tr>{cells}</tr></table>'
        f"<div>{legend}</div>"
    )


def _workload_line(digest: EodDepartmentDigest) -> str:
    """The department's standing workload, under the effort tiles.

    The tiles answer "what did today produce"; this answers "what is still
    waiting", which is the question a rollup of finished work otherwise leaves
    open.
    """
    if not digest.open_tasks:
        return ""
    late = (
        f' &middot; <span style="color:{_BAND_POOR};font-weight:600;">'
        f"{digest.overdue_tasks} overdue</span>"
        if digest.overdue_tasks
        else ""
    )
    return (
        f'<div style="margin:10px 0 0;font-size:12.5px;color:{_MUTED};">'
        f'<span style="font-weight:600;color:{_INK};">{digest.open_tasks}</span> '
        f"still open across the team{late}</div>"
    )


def _still_owed(digest: EodDepartmentDigest) -> str:
    """The people who still owe a report, named.

    Resolves the question the roster alone leaves open: in a department of six
    you can spot the gaps, in one of twenty you cannot. Mirrors the dashboard's
    "Still owed" row (fe/.../team-eod `_digest.tsx`), including which bands count
    - PENDING and MISSING only. Absent is not owed: someone who was not at work
    legitimately has no report, and dunning them for it is how a tool starts
    feeling like surveillance rather than a summary.

    Deliberately a summary line, not a re-sort of the roster: ordering people by
    who failed to report turns a daily digest into a leaderboard of shame, and
    alphabetical stays predictable week to week.
    """
    owed = [m.name for m in digest.members if m.coverage in ("pending", "missing")]
    if not owed:
        return ""
    return (
        f'<div style="margin:10px 0 0;padding:9px 12px;background:{_PAPER};'
        f'border-radius:8px;font-size:12px;color:{_MUTED};">'
        f'<span style="font-weight:600;color:{_INK};">Still owed</span> &middot; '
        f"{', '.join(escape(n) for n in owed)}</div>"
    )


def _roster_head(label: str) -> str:
    """A right-aligned numeric column header."""
    return (
        f'<td style="padding:0 0 5px 12px;font-size:9.5px;font-weight:600;'
        f"letter-spacing:0.1em;text-transform:uppercase;color:{_MUTED};"
        f'text-align:right;white-space:nowrap;">{label}</td>'
    )


def _roster_cell(value: str) -> str:
    """A right-aligned numeric cell. Tabular figures so the column lines up."""
    return (
        f'<td style="padding:9px 0 9px 12px;border-top:1px solid {_LINE};'
        f"text-align:right;font-size:12.5px;color:{_INK};white-space:nowrap;"
        f'vertical-align:top;font-variant-numeric:tabular-nums;">{value}</td>'
    )


# How many shipped titles to name per person before summarising the rest. A
# twenty-task day must not turn one roster row into half the email.
_MAX_TASKS_SHOWN = 3


def _role_label(job_title: str | None) -> str:
    """The person's designation, muted, beside their name. Reading across six
    departments an admin needs to know who someone is; the field was already on
    the schema and rendered nowhere."""
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


def _open_cell(member: EodDepartmentMember) -> str:
    """What is still on this person's plate, with the late share called out.

    Separate from "Done" on purpose: that counts what the day closed, this
    counts what it left open. A department can ship well and still be falling
    behind, and only the two numbers side by side show it.
    """
    if not member.tasks_open:
        return "&ndash;"
    if not member.tasks_overdue:
        return str(member.tasks_open)
    return (
        f'{member.tasks_open}<span style="color:{_BAND_POOR};font-size:11px;">'
        f" &#9679;{member.tasks_overdue}</span>"
    )


def _roster_rows(digest: EodDepartmentDigest) -> str:
    """One line per person: name, coverage, hours on the machine, active share
    and tasks shipped.

    The active share gets its OWN labelled column rather than riding along inside
    the hours cell: an unlabelled "82%" sitting next to "8.2h" is a number the
    reader has to guess at. A dash wherever there is nothing to show - never
    "0h", which reads as a measurement rather than an absence.
    """
    rows = ""
    for member in digest.members:
        reported = member.worked_minutes > 0
        flag = (
            f' <span style="color:{_AMBER};font-weight:600;">&#9888;{member.blockers}</span>'
            if member.blockers
            else ""
        )
        rows += (
            f'<tr><td style="padding:9px 14px 9px 0;border-top:1px solid {_LINE};'
            f'font-size:13px;color:{_INK};vertical-align:top;">'
            f"{escape(member.name)}{_role_label(member.job_title)}{flag}"
            f"{_shipped_line(member.tasks)}</td>"
            f'<td style="padding:9px 0 9px 10px;border-top:1px solid {_LINE};'
            f'text-align:right;white-space:nowrap;vertical-align:top;">'
            f"{_coverage_pill(member.coverage)}</td>"
            + _roster_cell(_fmt_hours(member.worked_minutes) if reported else "&ndash;")
            + _roster_cell(f"{member.active_pct}%" if reported else "&ndash;")
            + _roster_cell(str(member.tasks_done) if member.tasks_done else "&ndash;")
            + _roster_cell(_open_cell(member))
            + "</tr>"
        )
    header = (
        f'<tr><td style="padding:0 0 5px;font-size:9.5px;font-weight:600;'
        f'letter-spacing:0.1em;text-transform:uppercase;color:{_MUTED};">Who</td>'
        '<td style="padding:0 0 5px;"></td>'
        + _roster_head("Hours")
        + _roster_head("Active")
        + _roster_head("Done")
        + _roster_head("Open")
        + "</tr>"
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 18px;">{header}{rows}</table>'
    )


def _effort_block(digest: EodDepartmentDigest) -> str:
    """The three-up effort tiles, or a plain note when there is no effort to show.

    A department where nobody reported would otherwise render "0m worked, 0%
    active, 0 done" - three measurements of zero, which reads as "they sat there
    doing nothing" rather than "no reports came in". Its own roster says `-` on
    every row; the headline has to agree.
    """
    if digest.totals.worked_minutes == 0 and digest.submitted == 0:
        return (
            f'<div style="margin:18px 0 4px;padding:14px 16px;background:{_PAPER};'
            f"border:1px solid {_LINE};border-radius:10px;font-size:13px;"
            f'color:{_MUTED};">No reports came in, so there is nothing to total '
            "for this department yet.</div>"
        )
    return _stat_row(
        digest.totals.worked_minutes,
        _active_pct(digest.totals.active_minutes, digest.totals.worked_minutes),
        digest.totals.tasks_done,
    )


def eod_department_digest_email(
    *, digest: EodDepartmentDigest, dashboard_url: str | None = None
) -> tuple[str, str]:
    """Render the (subject, html) per-department End-of-Day rollup sent to admins.

    Deliberately carries NO narratives: the anchor submission rate, the day's
    summed effort, a coverage bar, the roster with each person's hours and tasks,
    and the department's blockers. A person's own account of their day goes to
    their reporting manager only (see EodService's module docstring), so there is
    no `summary` field anywhere in here to leak one.
    """
    department = escape(digest.department)
    single_day = digest.from_date == digest.to_date
    # A multi-day rollup must say so in BOTH places. Naming only `to_date` made a
    # seven-day digest look like one Tuesday's numbers, which is the kind of
    # mislabel someone acts on.
    date_label = (
        _day_label(digest.to_date)
        if single_day
        else f"{_day_label(digest.from_date)} &rarr; {_day_label(digest.to_date)}"
    )
    subject_date = (
        _day_label(digest.to_date)
        if single_day
        else f"{_day_label(digest.from_date)} - {_day_label(digest.to_date)}"
    )
    pct = round(digest.submission_rate * 100)
    subject = (
        f"EOD · {digest.department} · {digest.submitted}/{digest.member_count} reported · "
        f"{subject_date}"
    )
    blockers_html = _blocker_block([escape(b) for b in digest.blockers]) if digest.blockers else ""
    # Every other Avora email offers a way back into the product; without this
    # the rollup is a dead end - you can see a department is behind and have
    # nowhere to click.
    open_html = (
        f'<p style="margin:4px 0 0;">{_button("Open in Avora", dashboard_url)}</p>'
        if dashboard_url
        else ""
    )
    content = f"""\
{_eyebrow(f"Department rollup &middot; {date_label}")}
<h1 style="margin:8px 0 0;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {department}
</h1>
<div style="margin:14px 0 4px;">
  <span style="font-family:{_SERIF};font-size:40px;font-weight:600;line-height:1;color:{_band_color(pct)};font-variant-numeric:tabular-nums;">{pct}%</span>
  <span style="font-size:12.5px;color:{_MUTED};margin-left:8px;">
    {digest.submitted} of {digest.member_count} reported
  </span>
</div>
{_effort_block(digest)}
{_workload_line(digest)}
<div style="margin:18px 0 16px;">{_section_label("Coverage")}{_coverage_bar(digest)}{_still_owed(digest)}</div>
{_section_label("Roster")}
{_roster_rows(digest)}
{blockers_html}
{open_html}"""
    return subject, _layout(
        preheader=(
            f"{digest.department}: {digest.submitted} of {digest.member_count} reported, "
            f"{digest.totals.tasks_done} tasks shipped"
        ),
        content_html=content,
        footer=_ACTIVITY_FOOTER,
        # Wider than the default shell: this is the one EOD email with a real
        # five-column table in it, and at 460px the numbers crowd each other.
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
