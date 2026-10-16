"""Emails for forms: "you have a form to fill in" and HR's "these joiners
have no buddy". Built on the shared Avora email shell."""

from __future__ import annotations

from html import escape

from app.services.email_templates import (
    _ACTIVITY_FOOTER,
    _INK,
    _MUTED,
    _SERIF,
    _button,
    _chip,
    _layout,
)


def _heading(eyebrow: str, title: str) -> str:
    return f"""\
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{_MUTED};">
  {escape(eyebrow)}
</div>
<h1 style="margin:8px 0 16px;font-family:{_SERIF};font-size:26px;line-height:1.2;font-weight:600;color:{_INK};">
  {escape(title)}
</h1>"""


def _paragraph(html: str) -> str:
    return f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK};">{html}</p>'


def form_assigned_email(
    *,
    recipient_name: str,
    form_title: str,
    subject_name: str | None,
    due_label: str | None,
    form_url: str,
) -> tuple[str, str]:
    """(subject, html) asking someone to fill in a form."""
    about = f" about <strong>{escape(subject_name)}</strong>" if subject_name else ""
    due_html = (
        f'<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:{_MUTED};">'
        f"Please answer by {_chip(escape(due_label))}</p>"
        if due_label
        else ""
    )
    content = (
        _heading("Your feedback", form_title)
        + _paragraph(
            f"Hi {escape(recipient_name)}, we'd value a few minutes of your honest "
            f"feedback{about}. It goes to HR only."
        )
        + due_html
        + f'<p style="margin:0 0 4px;">{_button("Open the form", form_url)}</p>'
    )
    return f"Your feedback: {form_title}", _layout(
        preheader=f"A few minutes of your feedback: {escape(form_title)}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def missing_buddy_email(*, joiner_names: list[str], forms_url: str) -> tuple[str, str]:
    """(subject, html) telling HR some joiners have no buddy, so their buddy
    feedback form cannot go out."""
    items = "".join(f'<li style="margin:0 0 6px;">{escape(name)}</li>' for name in joiner_names)
    count = len(joiner_names)
    noun = "joiner has" if count == 1 else "joiners have"
    content = (
        _heading("Buddy feedback", f"{count} {noun} no buddy")
        + _paragraph(
            "Their buddy feedback form is due, but no buddy is on file. Assign one in "
            "Circle and the form goes out on the next run."
        )
        + f'<ul style="margin:0 0 20px;padding-left:20px;font-size:15px;color:{_INK};">{items}</ul>'
        + f'<p style="margin:0 0 4px;">{_button("Open forms", forms_url)}</p>'
    )
    return f"{count} {noun} no buddy", _layout(
        preheader="A buddy feedback form can't go out until a buddy is assigned.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def missing_manager_email(*, names: list[str], forms_url: str) -> tuple[str, str]:
    """(subject, html) telling HR some probationers have no reporting manager,
    so their manager probation evaluation cannot go out."""
    items = "".join(f'<li style="margin:0 0 6px;">{escape(name)}</li>' for name in names)
    count = len(names)
    noun = "probationer has" if count == 1 else "probationers have"
    content = (
        _heading("Probation review", f"{count} {noun} no reporting manager")
        + _paragraph(
            "Their probation evaluation form is due, but no reporting manager is on file. "
            "Set one and the form goes out to them on the next run."
        )
        + f'<ul style="margin:0 0 20px;padding-left:20px;font-size:15px;color:{_INK};">{items}</ul>'
        + f'<p style="margin:0 0 4px;">{_button("Open forms", forms_url)}</p>'
    )
    return f"{count} {noun} no reporting manager", _layout(
        preheader="A probation evaluation can't go out until a reporting manager is set.",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )


def hr_pool_form_email(
    *, form_title: str, subject_name: str | None, due_label: str | None, form_url: str
) -> tuple[str, str]:
    """(subject, html) telling HR and admins a shared probation review form is
    open. One of them fills it in; it leaves everyone's list once submitted."""
    about = f" about <strong>{escape(subject_name)}</strong>" if subject_name else ""
    due_html = (
        f'<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:{_MUTED};">'
        f"Please answer by {_chip(escape(due_label))}</p>"
        if due_label
        else ""
    )
    content = (
        _heading("HR review", form_title)
        + _paragraph(
            f"The HR probation review{about} is open. Anyone in HR can fill it in; "
            "once one of you submits it, it leaves everyone's list."
        )
        + due_html
        + f'<p style="margin:0 0 4px;">{_button("Open the form", form_url)}</p>'
    )
    return f"HR review: {form_title}", _layout(
        preheader=f"An HR probation review is open: {escape(form_title)}",
        content_html=content,
        footer=_ACTIVITY_FOOTER,
    )
