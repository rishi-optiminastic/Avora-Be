"""Application settings.

All configuration — and especially every secret — is loaded here from the
environment. Nothing else in the codebase may read os.environ directly, and no
secret is ever hardcoded (Golden rule #4, Security rule 5.6).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Runtime ----------------------------------------------------------------
    environment: Environment = Environment.LOCAL
    debug: bool = False
    log_level: str = "INFO"

    # Database ---------------------------------------------------------------
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://pms_app:change-me@localhost:5432/pms"),
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # Human auth — Better Auth (Next.js) issues short-lived asymmetric JWTs.
    # We verify them against its JWKS endpoint; we never share a symmetric secret
    # with the client and never accept an HS-downgraded or `none` token (5.2).
    better_auth_jwks_url: str = "http://localhost:3000/api/auth/jwks"
    better_auth_issuer: str = "http://localhost:3000"
    better_auth_audience: str = "http://localhost:3000"
    better_auth_jwt_algorithm: str = "EdDSA"

    # Legacy symmetric primitives — kept for internally-minted tokens and their
    # unit tests. NOT used in the human request path (that is Better Auth JWKS).
    jwt_secret: str = Field(default="change-me", min_length=8)
    jwt_issuer: str = "https://accounts.google.com"
    jwt_audience: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_seconds: int = 900
    google_oidc_client_id: str = "change-me"

    # Session cookie ---------------------------------------------------------
    session_cookie_name: str = "pms_session"
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # Agent ingest -----------------------------------------------------------
    agent_token_pepper: str = Field(default="change-me", min_length=8)
    agent_ingest_rate_per_minute: int = 120
    # Max tolerated skew between an agent's claimed timestamp and server time
    # before the sample is flagged (not dropped) — see Security rule 5.4.
    agent_max_clock_skew_seconds: int = 300

    # HR webhook -------------------------------------------------------------
    hr_webhook_secret: str = Field(default="change-me", min_length=8)
    hr_webhook_ip_allowlist: str = ""

    # Transactional email (SendGrid) + invitations ---------------------------
    sendgrid_api_key: str = Field(default="change-me", min_length=8)
    email_from: str = "no-reply@signalor.ai"
    email_from_name: str = "Avora"
    # Where invite links point (the Next.js app), e.g. {app}/invite/<token>.
    app_base_url: str = "http://localhost:3000"
    invite_token_pepper: str = Field(default="change-me", min_length=8)
    invite_ttl_hours: int = 168  # 7 days

    # Object storage (S3) — screenshot images live here, not in Postgres, so the
    # DB stays small and image reads come straight from S3. When unset the app
    # falls back to storing image bytes in the `screenshots.image` column.
    aws_region: str = ""
    aws_bucket_name: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_url_ttl_seconds: int = 300  # presigned GET lifetime
    s3_key_prefix: str = "screenshots"

    @property
    def s3_enabled(self) -> bool:
        return bool(self.aws_bucket_name and self.aws_region)

    # CORS -------------------------------------------------------------------
    cors_origins: str = "http://localhost:3000"

    @field_validator("cors_origins", "hr_webhook_ip_allowlist")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def hr_ip_allowlist(self) -> list[str]:
        return [ip.strip() for ip in self.hr_webhook_ip_allowlist.split(",") if ip.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    def assert_production_safe(self) -> None:
        """Fail fast on insecure production config (called from the app factory)."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.debug:
            problems.append("DEBUG must be false in production")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        for name, value in (
            ("JWT_SECRET", self.jwt_secret),
            ("AGENT_TOKEN_PEPPER", self.agent_token_pepper),
            ("HR_WEBHOOK_SECRET", self.hr_webhook_secret),
            ("SENDGRID_API_KEY", self.sendgrid_api_key),
            ("INVITE_TOKEN_PEPPER", self.invite_token_pepper),
        ):
            if value == "change-me":
                problems.append(f"{name} still has its placeholder value")
        if self.jwt_algorithm.lower() == "none":
            problems.append("JWT 'none' algorithm is forbidden")
        if problems:
            raise RuntimeError("Insecure production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Import this, never instantiate Settings directly."""
    return Settings()
