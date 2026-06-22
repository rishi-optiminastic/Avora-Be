"""Quick Meet schemas (Golden rule #5)."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, field_validator

# Guardrail: a single Quick Meet shouldn't fan out to an unbounded list.
MAX_DEFAULT_INVITEES = 50


class QuickMeetingRead(BaseModel):
    meet_url: str
    slack_posted: bool
    started_by: str
    invited_count: int


class QuickMeetDefaultsRead(BaseModel):
    invitee_emails: list[EmailStr]


class QuickMeetDefaultsUpdate(BaseModel):
    invitee_emails: list[EmailStr]

    @field_validator("invitee_emails")
    @classmethod
    def _dedupe_and_cap(cls, emails: list[EmailStr]) -> list[EmailStr]:
        # Case-insensitive de-dupe, preserve order, and cap the fan-out.
        seen: set[str] = set()
        unique: list[EmailStr] = []
        for email in emails:
            key = email.lower()
            if key not in seen:
                seen.add(key)
                unique.append(email)
        if len(unique) > MAX_DEFAULT_INVITEES:
            raise ValueError(f"At most {MAX_DEFAULT_INVITEES} default invitees are allowed.")
        return unique
