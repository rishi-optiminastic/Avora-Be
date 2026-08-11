"""Test harness.

Runs against a disposable in-memory SQLite database (no network, no real
external calls — Testing §9). Auth dependencies are satisfied by minting real
session JWTs and signing real HMACs, so the security path is exercised end to
end rather than mocked away.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import jwt
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Environment, Settings
from app.core.deps import get_db, get_email_service, get_jwks_client, get_settings
from app.core.security import (
    compute_hmac_sha256,
    generate_device_token,
    hash_device_token,
)
from app.main import create_app
from app.models import Base, Device, Employee, EmployeeStatus, Role
from app.models.attendance_policy import AttendancePolicy
from app.models.work_session import WorkSession

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# A throwaway EdDSA keypair standing in for Better Auth's JWKS. Tests sign
# tokens with the private half; the JWKS client dependency is overridden to
# hand the public half to the verifier — so the real signature path runs with
# no network (Testing §9).
_TEST_SIGNING_KEY = Ed25519PrivateKey.generate()
_TEST_PRIVATE_PEM = _TEST_SIGNING_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_TEST_PUBLIC_PEM = (
    _TEST_SIGNING_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


class _FakeSigningKey:
    def __init__(self, key: str) -> None:
        self.key = key


class _FakeJwksClient:
    """Stand-in for jwt.PyJWKClient that returns the test public key, no fetch."""

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(_TEST_PUBLIC_PEM)


class _FakeEmailService:
    """No-op email service for tests — never touches the network (Testing §9).

    Every send is appended to the class-level `outbox` so a test can assert WHO
    was mailed. The `client` fixture clears it per test; a class attribute (not
    an instance one) is what makes that work, since the DI override builds a
    fresh instance per resolution.
    """

    outbox: ClassVar[list[tuple[str, str]]] = []

    def _record(self, kwargs: dict[str, object], method: str) -> None:
        to = kwargs.get("to")
        if isinstance(to, str):
            type(self).outbox.append((method, to))

    async def send(self, *, to: str, subject: str, html: str) -> None:
        return None

    async def send_invite(self, **kwargs: object) -> None:
        return None

    async def send_leave_decision(self, **kwargs: object) -> None:
        return None

    async def send_leave_request(self, **kwargs: object) -> None:
        self._record(kwargs, "leave_request")

    async def send_task_escalated(self, **kwargs: object) -> None:
        self._record(kwargs, "task_escalated")

    async def send_agent_reinstall(self, **kwargs: object) -> None:
        return None

    async def send_task_assigned(self, **kwargs: object) -> None:
        return None

    async def send_payslip(self, **kwargs: object) -> None:
        return None

    async def send_resignation_submitted(self, **kwargs: object) -> None:
        return None

    async def send_resignation_decision(self, **kwargs: object) -> None:
        return None

    async def send_birthday(self, **kwargs: object) -> None:
        return None

    async def send_anniversary(self, **kwargs: object) -> None:
        return None

    async def send_festival(self, **kwargs: object) -> None:
        return None


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        environment=Environment.LOCAL,
        debug=True,
        database_url="postgresql+asyncpg://unused:unused@localhost/unused",  # overridden
        better_auth_issuer="https://auth.test",
        better_auth_audience="pms-test-audience",
        better_auth_jwt_algorithm="EdDSA",
        jwt_secret="test-jwt-secret-value",
        jwt_issuer="https://accounts.google.com",
        jwt_audience="test-audience",
        jwt_algorithm="HS256",
        agent_token_pepper="test-agent-pepper-value",
        hr_webhook_secret="test-hr-secret-value",
        envsync_fernet_key=Fernet.generate_key().decode(),
        cookie_secure=False,
        # Force the in-DB image path; never touch real S3 in tests.
        aws_region="",
        aws_bucket_name="",
        aws_access_key_id="",
        aws_secret_access_key="",
    )


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[object]:
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: object) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_jwks_client] = lambda: _FakeJwksClient()
    app.dependency_overrides[get_email_service] = lambda: _FakeEmailService()
    _FakeEmailService.outbox.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def seed(db: AsyncSession, settings: Settings):
    """Seed an org: an admin, a manager, and two employees (one a report)."""

    admin = Employee(
        hr_external_id="hr-admin",
        work_email="admin@corp.test",
        full_name="Ada Admin",
        role=Role.ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    manager = Employee(
        hr_external_id="hr-manager",
        work_email="manager@corp.test",
        full_name="Max Manager",
        role=Role.MANAGER,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add_all([admin, manager])
    await db.flush()

    report = Employee(
        hr_external_id="hr-report",
        work_email="report@corp.test",
        full_name="Remy Report",
        role=Role.EMPLOYEE,
        manager_id=manager.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    outsider = Employee(
        hr_external_id="hr-outsider",
        work_email="outsider@corp.test",
        full_name="Olive Outsider",
        role=Role.EMPLOYEE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add_all([report, outsider])
    await db.flush()

    # A device enrolled to the report, with a known raw token.
    raw_token = generate_device_token()
    device = Device(
        employee_id=report.id,
        label="report-laptop",
        token_hash=hash_device_token(settings, raw_token),
        last_sequence=0,
    )
    db.add(device)
    await db.commit()

    return _Seed(
        admin=admin,
        manager=manager,
        report=report,
        outsider=outsider,
        device=device,
        device_raw_token=raw_token,
    )


# Helpers ------------------------------------------------------------------- #
class _Seed:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)
        self.admin: Employee = kw["admin"]  # type: ignore[assignment]
        self.manager: Employee = kw["manager"]  # type: ignore[assignment]
        self.report: Employee = kw["report"]  # type: ignore[assignment]
        self.outsider: Employee = kw["outsider"]  # type: ignore[assignment]
        self.device: Device = kw["device"]  # type: ignore[assignment]
        self.device_raw_token: str = kw["device_raw_token"]  # type: ignore[assignment]


async def allow_capture(db: AsyncSession, employee_id: object) -> None:
    """Open the monitoring capture window for an employee so ingest is stored:
    pin the org to an all-working-days week and open a work session. Monitoring is
    captured only during an open session on a working day (MonitoringGateService)."""
    db.add(AttendancePolicy(working_days_per_week=7))
    db.add(
        WorkSession(
            employee_id=employee_id,
            clock_in_at=datetime.now(UTC) - timedelta(hours=1),
            source="dashboard",
        )
    )
    await db.commit()


def bearer_for_email(
    settings: Settings, email: str, *, subject: str | None = None, name: str | None = None
) -> dict[str, str]:
    """Mint a Better Auth-shaped EdDSA JWT for any email (need not be an employee)."""
    now = datetime.now(UTC)
    claims = {
        "sub": subject or f"ba-{email}",
        "email": email,
        "iss": settings.better_auth_issuer,
        "aud": settings.better_auth_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    if name is not None:
        claims["name"] = name
    token = jwt.encode(claims, _TEST_PRIVATE_PEM, algorithm="EdDSA")
    return {"Authorization": f"Bearer {token}"}


def auth_headers(settings: Settings, employee: Employee) -> dict[str, str]:
    """Authorization header for an existing employee (identity only: sub + email)."""
    return bearer_for_email(settings, employee.work_email, subject=employee.hr_external_id)


def hr_headers(settings: Settings, body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    sig = compute_hmac_sha256(settings.hr_webhook_secret, raw)
    return raw, {"X-HR-Signature": sig, "Content-Type": "application/json"}


def agent_headers(raw_token: str, body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    sig = compute_hmac_sha256(raw_token, raw)
    return raw, {
        "Authorization": f"Bearer {raw_token}",
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
