"""Branded Avora email templates.

A single inline-styled layout (`_layout`) is the source of truth for how every
Avora email looks — new emails render their body through it so the brand stays
consistent everywhere. There is no inner card: content sits directly on the
paper background. Radii follow the product's rounded-md (8px). The wordmark and
headings ask for Reckless (the product serif) with a Georgia fallback, since
most email clients can't load a custom web font.
"""

from __future__ import annotations

_VIOLET = "#5a48e0"
_VIOLET_SOFT = "#ece9f7"
_INK = "#1a1530"
_MUTED = "#6b6485"
_PAPER = "#f6f4ff"
_LINE = "#e6e2f0"
_SERIF = "'Reckless Neue', Georgia, 'Times New Roman', serif"
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _layout(*, preheader: str, content_html: str) -> str:
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
            You're receiving this because someone invited you to an Avora workspace.
            If you weren't expecting it, you can safely ignore this email.
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
