"""Productivity categorisation of browsing domains.

Phase 2 uses a built-in domain → category map with sensible defaults; an
admin-editable config is a later step. Matching is by registrable-domain
suffix, so `mail.google.com` matches a `google.com` rule. Anything unknown is
NEUTRAL — we never guess "distracting".
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse


class ProductivityCategory(StrEnum):
    PRODUCTIVE = "productive"
    NEUTRAL = "neutral"
    DISTRACTING = "distracting"


# Suffix sets. Keep curated and conservative.
_PRODUCTIVE: frozenset[str] = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "stackoverflow.com",
        "stackexchange.com",
        "developer.mozilla.org",
        "atlassian.net",
        "jira.com",
        "linear.app",
        "notion.so",
        "figma.com",
        "docs.google.com",
        "sheets.google.com",
        "slides.google.com",
        "drive.google.com",
        "vercel.com",
        "render.com",
        "aws.amazon.com",
        "console.cloud.google.com",
        "portal.azure.com",
        "openai.com",
        "chatgpt.com",
        "claude.ai",
        "anthropic.com",
        "confluence.com",
        "asana.com",
        "trello.com",
        "localhost",
    }
)

_DISTRACTING: frozenset[str] = frozenset(
    {
        "youtube.com",
        "netflix.com",
        "primevideo.com",
        "hotstar.com",
        "instagram.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "reddit.com",
        "tiktok.com",
        "twitch.tv",
        "pinterest.com",
        "snapchat.com",
        "9gag.com",
        "spotify.com",
    }
)


def extract_domain(url: str | None) -> str | None:
    """Best-effort registrable host from a URL. Re-derived server-side; we never
    trust a client-supplied domain (Golden rule #1)."""
    if not url:
        return None
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "//" + candidate
    host = urlparse(candidate).hostname
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _matches(domain: str, suffixes: frozenset[str]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def classify(domain: str | None) -> ProductivityCategory:
    if not domain:
        return ProductivityCategory.NEUTRAL
    d = domain.lower()
    if _matches(d, _DISTRACTING):
        return ProductivityCategory.DISTRACTING
    if _matches(d, _PRODUCTIVE):
        return ProductivityCategory.PRODUCTIVE
    return ProductivityCategory.NEUTRAL
