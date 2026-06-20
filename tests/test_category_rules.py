"""Category-rule authorization + the resolver overriding built-in defaults."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.categories import CategoryResolver, CategoryRuleSpec, MatchType, ProductivityCategory
from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def test_resolver_overrides_builtin_default() -> None:
    # youtube is unproductive by default; a global rule can flip it to productive
    # (e.g. a media team), and a department rule beats the global one.
    rules = [
        CategoryRuleSpec(MatchType.DOMAIN, "youtube.com", ProductivityCategory.PRODUCTIVE, None),
        CategoryRuleSpec(
            MatchType.DOMAIN, "youtube.com", ProductivityCategory.UNPRODUCTIVE, "Engineering"
        ),
    ]
    resolver = CategoryResolver(rules)
    assert resolver.classify(domain="youtube.com") is ProductivityCategory.PRODUCTIVE
    assert (
        resolver.classify(domain="youtube.com", department="Engineering")
        is ProductivityCategory.UNPRODUCTIVE
    )
    # Unknown domain still falls back to the built-in default (neutral).
    assert resolver.classify(domain="example.com") is ProductivityCategory.NEUTRAL


async def test_rules_require_manager_or_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {"match_type": "domain", "pattern": "figma.com", "category": "productive"}
    # An individual contributor cannot list or create.
    assert (
        await client.get("/api/v1/category-rules", headers=auth_headers(settings, seed.report))
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/category-rules", json=body, headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    # Admin can create an org-wide rule.
    created = await client.post(
        "/api/v1/category-rules", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert created.status_code == 201, created.text
    assert created.json()["category"] == "productive"


async def test_idle_category_cannot_be_assigned_by_rule(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/category-rules",
        json={"match_type": "domain", "pattern": "x.com", "category": "idle"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422
