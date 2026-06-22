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


def _bullets(label: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:0 0 4px;font-size:14px;line-height:1.5;color:{_INK};">{i}</li>'
        for i in items
    )
    return (
        f'<div style="margin:0 0 14px;"><div style="font-size:11px;font-weight:600;'
        f"letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};margin:0 0 6px;\">"
        f"{label}</div>"
        f'<ul style="margin:0;padding-left:18px;">{lis}</ul></div>'
    )


def eod_report_email(
    *,
    employee_name: str,
    date_label: str,
    summary: str,
    highlights: EodDraftContent,
) -> tuple[str, str]:
    """Render the (subject, html) End-of-Day report sent to manager + admins."""
    subject = f"End of day · {employee_name} · {date_label}"
    summary_html = "".join(
        f'<p style="margin:0 0 12px;font-size:15px;line-height:1.6;color:{_INK};">{para}</p>'
        for para in summary.split("\n\n")
        if para.strip()
    )
    content = f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  End of day · {date_label}
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {employee_name}
</h1>
{summary_html}
{_bullets("Worked on", highlights.worked_on)}
{_bullets("Completed", highlights.tasks_completed)}
{_bullets("Blockers", highlights.blockers)}"""
    return subject, _layout(
        preheader=f"{employee_name}'s end-of-day summary for {date_label}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )
