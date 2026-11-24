"""Google Calendar through a Workspace service account.

Extracted from `MeetingService` (Quick Meet), which proved this path in
production, so Founder Meetings books events with the same tested token
exchange instead of a second copy of it.

How it authorizes: a service account with domain-wide delegation signs a JWT
assertion impersonating a Workspace user and exchanges it for an access token.
There is no per-user OAuth consent, no refresh token to store or encrypt, and
nothing to re-authorize when somebody changes their password - which is why the
spec's `meeting_integration_accounts` table does not exist here.

Scope is per call, deliberately. Event writes need only `calendar.events`, which
is what the service account is already authorized for; FreeBusy needs a broader
read scope. Asking for both on every token would mean a single unauthorized
scope in the Workspace Admin console breaks Quick Meet too, so each operation
asks for exactly what it uses and fails alone.

The SA private key is a secret from Settings only and never reaches a log
(Security rule 5.6); `_describe_pem` reports a key's SHAPE when it will not
parse, never its bytes.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger

# Insert / patch / delete an event and invite people to it.
SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events"
# Read a calendar's busy blocks. Must be added to the service account's
# domain-wide delegation in the Workspace Admin console before availability
# checks will work - `calendar.events` alone is NOT sufficient for FreeBusy.
SCOPE_READONLY = "https://www.googleapis.com/auth/calendar.readonly"

_API_ROOT = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = 10.0

logger = get_logger("app.google_calendar")


class IntegrationError(AppError):
    """An upstream integration (Google/Slack) is unavailable or misconfigured."""

    status_code = 503
    code = "integration_unavailable"
    message = "Meeting integration is unavailable."


class GoogleCalendarClient:
    """Thin, typed wrapper over the Calendar REST API.

    Holds no state beyond settings: a fresh access token is fetched per
    operation. Tokens last an hour and these calls are rare (a handful of
    meetings a day), so caching them would add an invalidation bug for no
    measurable gain.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.google_sa_client_email and self._settings.google_sa_private_key)

    # ---- authorization ---------------------------------------------------- #
    def build_assertion(self, subject: str, scope: str) -> str:
        """Signed JWT the SA exchanges for an access token, impersonating
        `subject` (domain-wide delegation)."""
        now = int(time.time())
        claims = {
            "iss": self._settings.google_sa_client_email,
            "sub": subject,
            "scope": scope,
            "aud": self._settings.google_sa_token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        key = normalise_pem(self._settings.google_sa_private_key)
        return jwt.encode(claims, key, algorithm="RS256")

    async def access_token(self, subject: str, scope: str = SCOPE_EVENTS) -> str:
        # Signing is the likeliest thing to break on a fresh setup - the SA
        # private key is pasted through an env var, so a stray quote or
        # half-unescaped newline leaves a PEM that a non-empty check happily
        # accepts and PyJWT then rejects. Uncaught that surfaced as an opaque
        # 500; as a 503 it reads as "the integration is misconfigured", which is
        # what it is.
        try:
            assertion = self.build_assertion(subject, scope)
        except Exception as exc:
            logger.warning(
                "google_sa_key_unusable",
                extra={
                    "reason": type(exc).__name__,
                    "sa_client_email": self._settings.google_sa_client_email,
                },
            )
            raise IntegrationError(
                "GOOGLE_SA_PRIVATE_KEY is unreadable: "
                f"{describe_pem(self._settings.google_sa_private_key)}. "
                "Paste the private_key value from the service-account JSON, "
                "without wrapping quotes."
            ) from exc
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(self._settings.google_sa_token_uri, data=payload)
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google to authorize.") from exc
        if resp.status_code >= 400:
            # Google's token-endpoint error body carries no secrets - only
            # {"error", "error_description"}. Log it so prod failures are
            # diagnosable instead of an opaque 400 -> 503. The subject is the
            # impersonated user, not a secret.
            logger.warning(
                "google_token_exchange_failed",
                extra={
                    "status": resp.status_code,
                    "google_error": resp.text,
                    "subject": subject,
                    "scope": scope,
                },
            )
            raise IntegrationError("Google rejected the meeting authorization.")
        token = resp.json().get("access_token")
        if not isinstance(token, str):
            raise IntegrationError("Google did not return an access token.")
        return token

    # ---- events ----------------------------------------------------------- #
    async def insert_event(
        self,
        *,
        subject: str,
        calendar_id: str,
        body: dict[str, Any],
        with_meet_link: bool = True,
    ) -> dict[str, Any]:
        """Create an event and let Google email every attendee.

        `sendUpdates=all` is what actually delivers the invitations, so the
        requester and participants get a real calendar entry rather than a row
        in Avora nobody sees.
        """
        token = await self.access_token(subject, SCOPE_EVENTS)
        if with_meet_link:
            body = {
                **body,
                "conferenceData": {
                    "createRequest": {
                        "requestId": uuid.uuid4().hex,
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
            }
        return await self._call(
            "POST",
            f"{_API_ROOT}/calendars/{quote(calendar_id, safe='')}/events",
            token=token,
            params={"conferenceDataVersion": 1, "sendUpdates": "all"},
            json=body,
            failure="Google Calendar could not create the meeting.",
        )

    async def patch_event(
        self,
        *,
        subject: str,
        calendar_id: str,
        event_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Move or amend an existing event in place.

        A patch keeps the same event id, so attendees' existing calendar entry
        moves instead of a second one appearing beside it.
        """
        token = await self.access_token(subject, SCOPE_EVENTS)
        return await self._call(
            "PATCH",
            f"{_API_ROOT}/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}",
            token=token,
            params={"conferenceDataVersion": 1, "sendUpdates": "all"},
            json=body,
            failure="Google Calendar could not update the meeting.",
        )

    async def delete_event(self, *, subject: str, calendar_id: str, event_id: str) -> None:
        """Cancel an event and tell the attendees.

        A 404 or 410 is treated as success: the event is already gone, which is
        the state the caller wanted. Anything else raises, because silently
        swallowing a failed cancellation leaves a meeting on a calendar that
        Avora believes it removed.
        """
        token = await self.access_token(subject, SCOPE_EVENTS)
        url = (
            f"{_API_ROOT}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        )
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(
                    "DELETE",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"sendUpdates": "all"},
                )
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google Calendar.") from exc
        if resp.status_code in (404, 410):
            return
        if resp.status_code >= 400:
            logger.warning(
                "google_event_delete_failed",
                extra={"status": resp.status_code, "google_error": resp.text[:500]},
            )
            raise IntegrationError("Google Calendar could not cancel the meeting.")

    async def _call(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict[str, Any],
        json: dict[str, Any],
        failure: str,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google Calendar.") from exc
        if resp.status_code >= 400:
            # Truncated: Google's error text is a description, not a credential,
            # but it is also unbounded and ends up in `meeting_events.last_error`.
            logger.warning(
                "google_calendar_call_failed",
                extra={"status": resp.status_code, "google_error": resp.text[:500]},
            )
            raise IntegrationError(failure)
        parsed = resp.json()
        if not isinstance(parsed, dict):
            raise IntegrationError("Google Calendar returned an unexpected response.")
        return parsed

    @staticmethod
    def extract_meet_link(event: dict[str, Any]) -> str | None:
        """The video link on an event, or None when it carries no conference.

        Returns None rather than raising: a meeting with no Meet link is still a
        meeting (the CEO may want people in the room), so only Quick Meet - whose
        entire output IS the link - treats its absence as a failure.
        """
        link = event.get("hangoutLink")
        if isinstance(link, str) and link:
            return link
        conference = event.get("conferenceData")
        entry_points = conference.get("entryPoints", []) if isinstance(conference, dict) else []
        for entry in entry_points:
            uri = entry.get("uri")
            if entry.get("entryPointType") == "video" and isinstance(uri, str):
                return uri
        return None


def normalise_pem(raw: str) -> str:
    """Coax a pasted private key into a real PEM.

    The key reaches us through an env-var text box, and the same four mistakes
    happen every time: the JSON's escaped ``\\n`` sequences survive verbatim, the
    surrounding quotes get pasted along with the value, the deploy UI expands the
    escapes into genuine newlines, or it expands only the ``n`` and leaves the
    backslash behind at the end of every line. All four describe the same key, so
    accept all four rather than making somebody guess which one the parser wanted.

    Only formatting is normalised. A truncated or wrong key still fails to parse,
    which is the point.
    """
    key = raw.strip()
    # Paired wrapping quotes, e.g. VALUE="-----BEGIN...". Unpaired quotes are left
    # alone: they are part of a genuinely malformed value, not decoration.
    for quote_char in ('"', "'"):
        if len(key) >= 2 and key.startswith(quote_char) and key.endswith(quote_char):
            key = key[1:-1].strip()
            break
    # Escaped newlines (single-line form) become real ones. A key that already has
    # real newlines is unaffected: there is nothing to replace.
    key = key.replace("\\n", "\n")
    # A backslash left dangling at the end of a line is the half-expanded form:
    # the `n` of each `\n` became a real newline and the backslash stayed. It sits
    # inside the base64 body and breaks the decode, while every marker still looks
    # right, which is why the key reads as valid and is not. Base64 never contains
    # a backslash, so stripping it here is unambiguous.
    lines = (line.rstrip().rstrip("\\").rstrip() for line in key.split("\n"))
    cleaned = "\n".join(line for line in lines if line)
    # Keep an empty value empty, so `describe_pem` can still say "it is empty"
    # rather than reporting a missing BEGIN line on a blank env var.
    return f"{cleaned}\n" if cleaned else ""


def describe_pem(raw: str) -> str:
    """Why a key looks unusable, in terms the person pasting it can act on.

    Deliberately reports SHAPE only - length and which markers are present. The
    key itself is a secret and never reaches a log or an API response.
    """
    key = normalise_pem(raw)
    if not key:
        return "it is empty"
    problems: list[str] = []
    if "-----BEGIN" not in key:
        problems.append("no BEGIN line")
    if "-----END" not in key:
        problems.append("no END line")
    # Count breaks in the CONTENT: normalisation terminates the key with a newline,
    # so testing for one at all would call every single-line key well formed and
    # lose the most actionable message of the set.
    if len(key.strip().split("\n")) == 1:
        problems.append("no line breaks (escaped \\n may have been stripped)")
    if not problems:
        problems.append(f"markers present but the body did not parse ({len(key)} chars)")
    return ", ".join(problems)
