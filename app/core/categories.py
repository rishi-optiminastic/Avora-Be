"""Productivity categorisation of activity (browsing domains + apps).

Two layers:
  * Built-in defaults — a curated domain → category map (suffix match), so
    `mail.google.com` matches a `google.com` rule. Unknown ⇒ NEUTRAL; we never
    guess "unproductive" for an unknown domain.
  * Admin/manager rules — `category_rules` rows that OVERRIDE the defaults, scoped
    org-wide or to a department. A `CategoryResolver` overlays them on the
    defaults (department rule > global rule > built-in default).

Categories follow the PMS spec: productive / neutral / unproductive / idle /
unknown / requires_review. `idle` and `unknown` are activity-state outcomes the
read models assign; rules and the domain map only produce the first three (+ an
optional requires_review flag for sites a manager wants eyes on).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class ProductivityCategory(StrEnum):
    PRODUCTIVE = "productive"
    NEUTRAL = "neutral"
    UNPRODUCTIVE = "unproductive"
    IDLE = "idle"
    UNKNOWN = "unknown"
    REQUIRES_REVIEW = "requires_review"


class MatchType(StrEnum):
    DOMAIN = "domain"  # match against a browsing domain (suffix)
    APP = "app"  # match against the active application name (substring)


# Suffix sets for the built-in defaults. Keep curated and conservative.
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

_UNPRODUCTIVE: frozenset[str] = frozenset(
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


def _suffix_match(domain: str, pattern: str) -> bool:
    return domain == pattern or domain.endswith("." + pattern)


def _matches(domain: str, suffixes: frozenset[str]) -> bool:
    return any(_suffix_match(domain, s) for s in suffixes)


def classify(domain: str | None) -> ProductivityCategory:
    """Built-in default classification (no DB rules)."""
    if not domain:
        return ProductivityCategory.NEUTRAL
    d = domain.lower()
    if _matches(d, _UNPRODUCTIVE):
        return ProductivityCategory.UNPRODUCTIVE
    if _matches(d, _PRODUCTIVE):
        return ProductivityCategory.PRODUCTIVE
    return ProductivityCategory.NEUTRAL


@dataclass(frozen=True)
class CategoryRuleSpec:
    """A resolved rule (decoupled from the ORM): pattern → category, scoped to a
    department or org-wide (department is None)."""

    match_type: MatchType
    pattern: str
    category: ProductivityCategory
    department: str | None


class CategoryResolver:
    """Overlays admin/manager rules on the built-in defaults. Build once per
    request from the active rule set, then classify many domains/apps cheaply."""

    def __init__(self, rules: list[CategoryRuleSpec]) -> None:
        # Department-specific first so they take precedence over org-wide.
        self._rules = sorted(rules, key=lambda r: r.department is None)

    def classify(
        self, *, domain: str | None, app: str | None = None, department: str | None = None
    ) -> ProductivityCategory:
        for scope in (department, None):  # most specific first
            for rule in self._rules:
                if rule.department != scope:
                    continue
                if (
                    rule.match_type is MatchType.DOMAIN
                    and domain
                    and _suffix_match(domain.lower(), rule.pattern.lower())
                ):
                    return rule.category
                if rule.match_type is MatchType.APP and app and rule.pattern.lower() in app.lower():
                    return rule.category
        return classify(domain)
