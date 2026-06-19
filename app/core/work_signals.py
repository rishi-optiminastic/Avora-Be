"""Work attribution matcher — pure, testable, no LLM.

Given a person's on-screen text (OCR + active window + current domain) and the
admin catalog of work entities, score each entity by weighted keyword/domain
overlap and return the best match above a confidence floor, or None ("Unknown").

Matching is deliberately simple and transparent: an entity's distinctive terms
(name, keywords — which subsume aliases/repos/clients/tool names) usually appear
verbatim in the OCR text, so substring/token overlap is both fast and accurate.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]+")
_DOMAIN_WEIGHT = 3
_KEYWORD_WEIGHT = 2
_MIN_SCORE = 3  # below this ⇒ Unknown (one domain hit, or ≥2 keyword hits)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _term_set(*phrases: str) -> set[str]:
    """Split phrases into matchable terms (len ≥ 3 to avoid noise like 'go')."""
    terms: set[str] = set()
    for phrase in phrases:
        for tok in _tokens(phrase):
            if len(tok) >= 3:
                terms.add(tok)
    return terms


@dataclass(frozen=True)
class EntityProfile:
    entity_id: uuid.UUID
    name: str
    keyword_terms: frozenset[str]
    domains: tuple[str, ...]


def build_profile(
    entity_id: uuid.UUID, name: str, keywords: list[str], domains: list[str]
) -> EntityProfile:
    return EntityProfile(
        entity_id=entity_id,
        name=name,
        keyword_terms=frozenset(_term_set(name, *keywords)),
        domains=tuple(d.strip().lower() for d in domains if d.strip()),
    )


@dataclass(frozen=True)
class Match:
    entity_id: uuid.UUID
    name: str
    confidence: int
    matched: list[str]


def _domain_hit(domain: str | None, profile: EntityProfile) -> str | None:
    if not domain:
        return None
    d = domain.lower()
    for pd in profile.domains:
        if d == pd or d.endswith("." + pd):
            return pd
    return None


def _score(
    text_lower: str, text_tokens: set[str], domain: str | None, profile: EntityProfile
) -> tuple[int, list[str]]:
    matched: list[str] = []
    score = 0
    hit = _domain_hit(domain, profile)
    if hit:
        score += _DOMAIN_WEIGHT
        matched.append(hit)
    for term in profile.keyword_terms:
        # len≥4 terms match as substrings (survive 'org/repo' OCR joins); shorter
        # terms require an exact token to avoid false positives.
        if (len(term) >= 4 and term in text_lower) or term in text_tokens:
            score += _KEYWORD_WEIGHT
            matched.append(term)
    return score, matched


def attribute(text: str, domain: str | None, profiles: list[EntityProfile]) -> Match | None:
    """Best work-entity for this text, or None ('Unknown')."""
    text_lower = text.lower()
    text_tokens = _tokens(text)
    scored = []
    for p in profiles:
        s, m = _score(text_lower, text_tokens, domain, p)
        if s > 0:
            scored.append((s, m, p))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_matched, best = scored[0]
    if best_score < _MIN_SCORE:
        return None
    second = scored[1][0] if len(scored) > 1 else 0
    confidence = min(95, 45 + (best_score - _MIN_SCORE) * 10)
    if second and second >= best_score * 0.8:
        confidence -= 20  # ambiguous — a close runner-up
    return Match(best.entity_id, best.name, max(10, confidence), sorted(set(best_matched))[:6])
