"""Moving an employee's work email - roster and sign-in together.

Authorization is the point (CLAUDE.md §9): admin OR HR may move an ordinary
employee; on a privileged account it is a privilege transfer and admin-only;
everyone else gets 403.

The other thing worth proving is that both rows move. `employees.work_email` is
what Avora authorises on and `auth_user.email` is what the person types to sign
in, so a change that moves one and not the other locks them out - and would pass
a test that only ever looked at the API response.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.auth_identity import auth_user
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers


async def _add_hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _add_identity(db: AsyncSession, *, user_id: str, email: str) -> None:
    """A Better Auth account for someone who has actually signed up."""
    await db.execute(
        insert(auth_user).values(
            id=user_id,
            email=email,
            updatedAt=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db.commit()


async def _identity_email(db: AsyncSession, user_id: str) -> str | None:
    result = await db.execute(select(auth_user.c.email).where(auth_user.c.id == user_id))
    return result.scalar_one_or_none()


async def test_admin_and_hr_may_move_an_ordinary_email_but_nobody_else_may(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = await _add_hr(db)
    url = f"/api/v1/employees/{seed.report.id}/work-email"

    # The subject cannot move their own identity key.
    assert (
        await client.patch(
            url, json={"work_email": "self@acme.com"}, headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    # An unrelated employee cannot.
    assert (
        await client.patch(
            url, json={"work_email": "x@acme.com"}, headers=auth_headers(settings, seed.outsider)
        )
    ).status_code == 403
    # Their manager cannot - this is user management, not line management.
    assert (
        await client.patch(
            url, json={"work_email": "y@acme.com"}, headers=auth_headers(settings, seed.manager)
        )
    ).status_code == 403

    hr_resp = await client.patch(
        url, json={"work_email": "Remy.New@Acme.com"}, headers=auth_headers(settings, hr)
    )
    assert hr_resp.status_code == 200
    # Normalised to lower case, because login matches this column exactly.
    assert hr_resp.json()["work_email"] == "remy.new@acme.com"

    admin_resp = await client.patch(
        url, json={"work_email": "remy.newer@acme.com"}, headers=auth_headers(settings, seed.admin)
    )
    assert admin_resp.status_code == 200
    assert admin_resp.json()["work_email"] == "remy.newer@acme.com"


async def test_the_sign_in_address_moves_with_the_roster(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_identity(db, user_id="auth-remy", email="report@corp.test")

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "moved@acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200

    # The whole point: if this still said report@corp.test they could not log in.
    assert await _identity_email(db, "auth-remy") == "moved@acme.com"


async def test_an_employee_who_never_signed_up_still_moves(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """No `auth_user` row is a normal state - invited, never accepted - not an
    error. The roster must still move, and their first sign-in then creates the
    account under the new address."""
    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "never-signed-up@acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["work_email"] == "never-signed-up@acme.com"


async def _add_holder(db: AsyncSession, email: str) -> Employee:
    """Somebody already using the address we are about to ask for."""
    holder = Employee(
        hr_external_id="hr-holder",
        work_email=email,
        full_name="Hank Holder",
        role=Role.EXECUTIVE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(holder)
    await db.commit()
    return holder


async def test_an_address_another_employee_holds_is_a_conflict_that_names_them(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_holder(db, "taken@acme.com")

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "taken@acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 409
    # Named, because whoever is swapping two addresses has to move them one at
    # a time and needs to know who is in the way.
    assert "Hank Holder" in resp.text


async def test_a_held_address_is_rejected_whatever_its_casing(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_holder(db, "taken@acme.com")

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "TAKEN@ACME.COM"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 409


async def test_hr_may_not_move_a_privileged_account_but_admin_may(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """An email change hands the account, and its role, to whoever owns the new
    address. `hr_service._guard_email_change` refuses the HR webhook on a
    privileged account and says it must be done "in Avora by an admin" - so the
    in-Avora path has to draw the same line."""
    hr = await _add_hr(db)
    url = f"/api/v1/employees/{seed.manager.id}/work-email"

    assert (
        await client.patch(
            url, json={"work_email": "mgr@acme.com"}, headers=auth_headers(settings, hr)
        )
    ).status_code == 403

    admin_resp = await client.patch(
        url, json={"work_email": "mgr@acme.com"}, headers=auth_headers(settings, seed.admin)
    )
    assert admin_resp.status_code == 200


async def test_hr_may_not_move_a_payroll_manager_grant_holder(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The grant is privilege without a privileged role - a finance exec reading
    everyone's pay. The role check alone would wave it through."""
    hr = await _add_hr(db)
    seed.report.payroll_manager = True
    await db.commit()

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "finance@acme.com"},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 403


async def test_resending_the_same_address_changes_nothing(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Idempotent, and it must not write an audit row claiming a change that
    did not happen."""
    # In production everyone holds a real address; only the fixture uses `.test`,
    # which `EmailStr` will not accept as input. Move them once, then resend.
    seed.report.work_email = "ordinary@acme.com"
    await db.commit()

    before = await _audit_count(db)
    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "Ordinary@Acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["work_email"] == "ordinary@acme.com"
    assert await _audit_count(db) == before


async def test_the_audit_row_records_both_addresses(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """`AuditLog` stores only actor/action/target, so unless both addresses are
    in the target the row says somebody changed an email and not what it was -
    which answers no question anyone will later ask."""
    from app.models.audit import AuditLog

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "audited@acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "employee.work_email_change")))
        .scalars()
        .one()
    )
    assert row.actor == str(seed.admin.id)
    assert row.target is not None
    assert "report@corp.test->audited@acme.com" in row.target


async def test_unknown_employee_is_404(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    import uuid

    resp = await client.patch(
        f"/api/v1/employees/{uuid.uuid4()}/work-email",
        json={"work_email": "ghost@acme.com"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404


async def test_a_malformed_address_is_rejected(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}/work-email",
        json={"work_email": "not-an-email"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422


async def _audit_count(db: AsyncSession) -> int:
    from sqlalchemy import func

    from app.models.audit import AuditLog

    result = await db.execute(select(func.count()).select_from(AuditLog))
    return int(result.scalar_one())
