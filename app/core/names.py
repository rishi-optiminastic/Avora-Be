"""Display-name helpers shared by the invite, login and seed flows.

The real name comes from the verified Better Auth identity (the person's Google
profile, or the name they set at sign-up). When we don't have one yet — e.g. a
pending invite that hasn't been accepted — we derive a readable placeholder from
the email local-part. Keeping both in one place means "is this still a
placeholder?" has a single, trustworthy answer.
"""

from __future__ import annotations

_MAX_NAME_LEN = 120


def clean_display_name(name: str | None) -> str | None:
    """Normalize a name claimed by the auth provider, or None if unusable."""
    if not name:
        return None
    collapsed = " ".join(name.split())
    return collapsed[:_MAX_NAME_LEN] or None


def placeholder_name_from_email(email: str) -> str:
    """The fallback name we synthesize from an email when no real one exists."""
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
    return local.title() if local else "New member"


def is_placeholder_name(full_name: str, work_email: str) -> bool:
    """True when `full_name` is still the auto-generated email placeholder.

    Used to decide whether a real name from the auth provider may overwrite it —
    we never clobber a curated or HR-synced name.
    """
    return full_name == placeholder_name_from_email(work_email)
