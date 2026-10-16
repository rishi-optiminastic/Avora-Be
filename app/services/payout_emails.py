"""Email flagging payout mismatches to HR and payroll managers.

Built on the shared Avora email shell. It carries counts only - never names,
amounts per person or bank details - so a forwarded or misdelivered mail leaks
nothing; the detail lives behind login on the review page.
"""

from __future__ import annotations

from html import escape

from app.services.email_templates import (
    _ACTIVITY_FOOTER,
    _INK,
    _MUTED,
    _SERIF,
    _button,
    _layout,
)

_KIND_LABELS = {
    "paid_not_expected": "Paid but not expected",
    "expected_not_paid": "Expected but not paid",
    "amount_mismatch": "Amount differs",
    "duplicate_payment": "Paid more than once",
    "unmatched_row": "Payment we could not match",
    "account_mismatch": "Paid to a different account than on file",
}


def _count_line(label: str, count: int) -> str:
    return (
        f'<tr><td style="padding:6px 0;font-size:14px;color:{_INK};">{escape(label)}</td>'
        f'<td align="right" style="padding:6px 0;font-size:14px;font-weight:600;color:{_INK};">'
        f"{count}</td></tr>"
    )


def payout_mismatch_email(
    *, month_label: str, counts: dict[str, int], review_url: str
) -> tuple[str, str]:
    """(subject, html) telling HR the payout file disagrees with payroll."""
    total = sum(counts.values())
    noun = "mismatch" if total == 1 else "mismatches"
    rows = "".join(
        _count_line(_KIND_LABELS.get(kind, kind), count)
        for kind, count in counts.items()
        if count > 0
    )
    content = (
        f'<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:{_MUTED};">Payout check</div>'
        f'<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;'
        f'font-weight:600;color:{_INK};">{total} {noun} in {escape(month_label)}</h1>'
        f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">'
        "The bank payout file accounts uploaded does not match what payroll released. "
        "Review each line and resolve it with a note.</p>"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 22px;">{rows}</table>'
        f'<p style="margin:0 0 4px;">{_button("Review the payout", review_url)}</p>'
    )
    subject = f"Payout check: {total} {noun} in {month_label}"
    return subject, _layout(
        preheader=escape(subject), content_html=content, footer=_ACTIVITY_FOOTER
    )
