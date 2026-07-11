"""Branded Avora email templates.

A single inline-styled layout (`_layout`) is the source of truth for how every
Avora email looks — new emails render their body through it so the brand stays
consistent everywhere. There is no inner card: content sits directly on the
paper background. Radii follow the product's rounded-md (8px). The wordmark and
headings ask for Reckless (the product serif) with a Georgia fallback, since
most email clients can't load a custom web font.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas.eod import EodDraftContent

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


def _layout(*, preheader: str, content_html: str, footer: str = _INVITE_FOOTER) -> str:
    """Wrap an email body in the shared Avora shell — no card, on paper."""
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{_PAPER};font-family:{_SANS};-webkit-font-smoothing:antialiased;">
    <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</span>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAPER};padding:44px 20px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:460px;">
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
    name: str, net_minor: int, payable_days: float, working_days: int, currency: str
) -> str:
    days = f"{payable_days:g}/{working_days}"
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

    Each line is (employee_name, net_minor, payable_days, working_days).
    """
    subject = f"Payroll estimate — {month_label}"
    rows = "".join(
        _digest_row(name, net, payable, working, currency) for name, net, payable, working in lines
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
    """Render the (subject, html) End-of-Day report sent to manager + admins.

    Report-card layout: a three-up stat row (worked / active / done), an active-
    share bar, the narrative, then Worked-on pills, a Completed checklist, and any
    Blockers in an amber callout. Stats fall back to 0 cleanly when a signal is
    missing, so a quiet day still renders a clean card."""
    subject = f"End of day · {employee_name} · {date_label}"
    active_pct = round(active_minutes / worked_minutes * 100) if worked_minutes > 0 else 0
    summary_html = "".join(
        f'<p style="margin:0 0 12px;font-size:15px;line-height:1.6;color:{_INK};">{para}</p>'
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
{_eyebrow(f"End of day · {date_label}")}
<h1 style="margin:8px 0 0;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {employee_name}
</h1>
{_stat_row(worked_minutes, active_pct, tasks_done)}
{bar_html}
{summary_html}
{_pill_row("Worked on", highlights.worked_on)}
{_check_list("Completed", highlights.tasks_completed)}
{_blocker_block(highlights.blockers)}
{confidence_html}"""
    return subject, _layout(
        preheader=f"{employee_name}'s end-of-day summary for {date_label}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
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


def festival_email(*, festival_name: str, message: str) -> tuple[str, str]:
    return _celebration_email(
        eyebrow="Celebration",
        heading=f"{festival_name} 🎉",
        body=message,
    )
