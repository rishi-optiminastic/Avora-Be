"""Load the department CPD assessment papers into Learning, as DRAFTS.

One paper per department, transcribed from HR's "All Dept Assessment - CPD"
document: Marketing, Tech, Finance, Game Development, HR, 3D Designer and
Motion & Graphic Designer. 20 questions each, a mark apiece, across the five
CPD competencies, passing at 60%.

Nothing is sent to anybody by this script. Each paper lands as a draft in
Learning, where HR reviews it and assigns it to its department. Pass
`--publish` to also publish the papers whose answer key is complete.

The Marketing paper has NO answer key - the source document never carried one
- so its 20 questions import unkeyed and it stays a draft until HR marks the
answers in the composer. `--publish` reports it rather than forcing it.

Safe to re-run: a paper whose title already exists is skipped, so correcting
one file and re-running does not duplicate the others. Runs as the given
HR/admin, who is recorded as the author.

Seeds ONE workspace - the papers are a workspace's own departments, not a
deployment-wide fixture. `--schema` picks which, the same way
`alembic -x schema=ws_acme` does, and the default is the first workspace,
whose data lives in `public`.

    uv run python scripts/seed_cpd_assessments.py hr@optiminastic.com
    uv run python scripts/seed_cpd_assessments.py hr@optiminastic.com --publish
    uv run python scripts/seed_cpd_assessments.py boss@acme.com --schema ws_acme
"""

from __future__ import annotations

import asyncio
import sys

from app.core.cpd_papers import load_papers
from app.core.tenancy import use_schema
from app.db.session import SessionFactory, engine
from app.models.assessment import AssessmentStatus
from app.models.employee import Role
from app.repositories.assessment import AssessmentRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.schemas.auth import CurrentUser
from app.services.assessment_service import AssessmentService
from app.services.notification_service import NotificationService


async def seed(author_email: str, *, publish: bool, schema: str | None = None) -> list[str]:
    papers = load_papers()
    async with SessionFactory() as session:
        # Before ANY read: the author lookup, the duplicate-title check and
        # every insert have to land in the same workspace, and the default
        # search_path is the first one.
        if schema is not None:
            await use_schema(session, schema)
        employees = EmployeeRepository(session)
        author = await employees.find_by_work_email_insensitive(author_email.strip())
        if author is None or author.role not in (Role.ADMIN, Role.IT_ADMIN, Role.HR):
            raise SystemExit(f"{author_email} is not an HR or admin account in Avora.")
        caller = CurrentUser(employee_id=author.id, role=author.role, manager_id=author.manager_id)

        assessments = AssessmentRepository(session)
        service = AssessmentService(
            assessments,
            employees,
            NotificationService(NotificationRepository(session)),
            AuditRepository(session),
        )

        existing = {row.title for row in await assessments.list_all(include_drafts=True)}
        done: list[str] = []
        for paper in papers:
            if paper.title in existing:
                done.append(f"skipped (already there): {paper.title}")
                continue
            created = await service.create(caller, paper)
            note = f"created draft: {created.title} ({created.question_count} questions"
            if created.unkeyed_count:
                done.append(
                    f"{note}, {created.unkeyed_count} with NO answer key - "
                    f"set the key in Learning before publishing)"
                )
                continue
            if not publish:
                done.append(f"{note})")
                continue
            live = await service.publish(caller, created.id)
            assert live.status is AssessmentStatus.PUBLISHED
            done.append(f"{note}, published - assign it to {live.department or 'an audience'})")
        await session.commit()
    await engine.dispose()
    return done


USAGE = (
    "usage: python scripts/seed_cpd_assessments.py <hr-or-admin-email> "
    "[--publish] [--schema <workspace-schema>]"
)


if __name__ == "__main__":
    args = sys.argv[1:]
    wants_publish = "--publish" in args
    if wants_publish:
        args.remove("--publish")
    target: str | None = None
    if "--schema" in args:
        at = args.index("--schema")
        if at + 1 >= len(args):
            raise SystemExit(USAGE)
        target = args[at + 1]
        del args[at : at + 2]
    if len(args) != 1 or args[0].startswith("-"):
        raise SystemExit(USAGE)
    for line in asyncio.run(seed(args[0], publish=wants_publish, schema=target)):
        print(line)
