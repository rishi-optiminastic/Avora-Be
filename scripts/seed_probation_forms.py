"""Load the three probation review forms as DRAFTS, ready to review and publish.

- "Probation Self-Assessment": to the employee on probation.
- "Probation Evaluation - Reporting Manager": to their reporting manager, about
  them, with weighted performance criteria (weights add up to 100).
- "Probation Review - HR Feedback": one shared form per probationer that any
  HR or admin may fill in.

Each opens 21 days before the probationer's probation end date. Nothing is
sent by this script. HR reviews each draft in Avora (Forms), edits anything,
and publishes it; only then does the daily job start sending, and only to
people still on probation.

Safe to re-run: a form whose title already exists (draft or published) is
skipped. Runs as the given HR/admin, who is recorded as the author.

    uv run python scripts/seed_probation_forms.py hr@optiminastic.com
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.core.probation import REVIEW_LEAD_DAYS
from app.db.session import SessionFactory, engine
from app.models.employee import Role
from app.models.form import FormStatus
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormRepository, FormResponseRepository
from app.repositories.leave_policy import LeavePolicyRepository
from app.repositories.probation_checklist import ProbationChecklistRepository
from app.schemas.auth import CurrentUser
from app.schemas.form import FormWrite
from app.services.form_service import FormService
from app.services.probation_forms import ProbationFormLink

DUE_DAYS = 7


def _rating(label: str) -> dict[str, Any]:
    return {"type": "rating", "label": label}


def _weighted(label: str, help_text: str, weight: int) -> dict[str, Any]:
    return {"type": "rating", "label": label, "help_text": help_text, "weight": weight}


def _open(label: str) -> dict[str, Any]:
    return {"type": "paragraph", "label": label, "required": False}


def _section(label: str) -> dict[str, Any]:
    return {"type": "section", "label": label}


def _base(title: str, trigger: str, labels: list[str], intro: str) -> dict[str, Any]:
    return {
        "title": title,
        "trigger": trigger,
        "trigger_days": REVIEW_LEAD_DAYS,
        "due_days": DUE_DAYS,
        "rating_labels": labels,
        "intro": intro,
    }


SELF: dict[str, Any] = {
    **_base(
        "Probation Self-Assessment",
        "probation_self",
        ["Strongly Disagree", "Disagree", "Neutral", "Agree", "Strongly Agree"],
        "As you come to the end of your probation period, we'd like to hear about your "
        "experience so far at Optiminastic.\n\n"
        "This form is designed to help us understand your experience, how supported and "
        "confident you feel in your role, and what has worked well or could be improved "
        "during your probation.\n\n"
        "Your feedback will help us strengthen the employee experience and identify areas "
        "where we can provide better support.\n\n"
        "Please answer the questions honestly and openly. There are no right or wrong "
        "answers - your feedback is valuable to us.",
    ),
    "fields": [
        _section("Section 1. Role Clarity & Understanding"),
        _rating(
            "I have a clear understanding of what is expected of me in my role and how my work "
            "contributes to the team/company."
        ),
        _rating(
            "I feel I have gained a good understanding of the organisation, its processes, and "
            "the way work is done during my probation period."
        ),
        _section("Section 2. Confidence, Learning & Ownership"),
        _rating("I feel increasingly confident in handling my responsibilities independently."),
        _rating(
            "I have had sufficient opportunities to learn, take ownership, and contribute "
            "meaningfully during my probation period."
        ),
        _rating(
            "I feel my strengths and skills are being effectively utilised in my current role."
        ),
        _section("Section 3. Manager Support & Feedback"),
        _rating(
            "My reporting manager has provided me with the right level of guidance, feedback, "
            "and support during my probation."
        ),
        _rating(
            "I have received meaningful feedback that has helped me understand what I am doing "
            "well and where I can improve."
        ),
        _section("Section 4. Team & Work Environment"),
        _rating(
            "I feel comfortable asking questions, sharing ideas, and raising concerns with my "
            "manager and team."
        ),
        _rating(
            "I feel that my role, team, and work environment are aligned with what I understood "
            "and expected when I joined."
        ),
        _section("Section 5. Overall Probation Experience & Future"),
        _rating(
            "At the end of my probation period, I feel confident about continuing and growing in "
            "my role within the organisation."
        ),
        _section("Open questions"),
        _open(
            "What would you consider your key achievements or contributions during your "
            "probation period?"
        ),
        _open(
            "What has been the biggest challenge you faced during your probation, and how could "
            "we have supported you better?"
        ),
        _open(
            "Is there anything you wish you had known or understood better when you joined the "
            "organisation?"
        ),
        _open(
            "What additional support, resources, training, or guidance would help you perform "
            "better in your role?"
        ),
        _open(
            "As you move forward in the organisation, what are you looking forward to learning, "
            "contributing, or taking on?"
        ),
    ],
}

MANAGER: dict[str, Any] = {
    **_base(
        "Probation Evaluation - Reporting Manager",
        "probation_manager",
        [
            "Unsatisfactory",
            "Needs Improvement",
            "Meets Expectations",
            "Exceeds Expectations",
            "Exceptional",
        ],
        "As part of the probation confirmation process, we request you to share your feedback "
        "on the employee's performance during their probation period.\n\n"
        "Please evaluate the employee across the key performance criteria based on their "
        "overall performance, contributions, behaviour, and role fit during the probation "
        "period. The weighted criteria will help provide a structured and objective "
        "assessment, while the open-ended questions will allow you to highlight key strengths, "
        "future potential, and any areas that may require attention or development.\n\n"
        "Please provide honest, specific, and constructive feedback to support the "
        "confirmation decision and the employee's continued growth within the organisation.",
    ),
    "fields": [
        _section("Performance criteria"),
        _weighted(
            "Job Knowledge & Skills",
            "Demonstrates required skills and knowledge for the role; understands role-specific "
            "tasks and responsibilities.",
            20,
        ),
        _weighted(
            "Quality of Work",
            "Completes work accurately, with attention to detail, and meets quality standards.",
            15,
        ),
        _weighted(
            "Productivity & Efficiency",
            "Achieves set targets, meets deadlines, and manages time effectively.",
            15,
        ),
        _weighted(
            "Adaptability",
            "Shows flexibility in handling change, adapting to new responsibilities or team needs.",
            10,
        ),
        _weighted(
            "Initiative & Problem-Solving",
            "Proactively takes on tasks, identifies potential issues, and suggests solutions.",
            10,
        ),
        _weighted(
            "Communication Skills",
            "Communicates clearly, effectively, and professionally with team members and "
            "management.",
            10,
        ),
        _weighted(
            "Teamwork & Collaboration",
            "Works well with others, supports team goals, and contributes positively to the team "
            "environment.",
            10,
        ),
        _weighted(
            "Attendance & Punctuality",
            "Maintains consistent attendance, adheres to work hours, and minimizes unexcused "
            "absences.",
            5,
        ),
        _weighted(
            "Adherence to Company Policies",
            "Follows company policies, procedures, and standards of conduct.",
            5,
        ),
        _section("Open questions"),
        _open(
            "How would you evaluate the employee's overall performance during the probation "
            "period? What have been their key strengths and contributions?"
        ),
        _open(
            "Do you see the employee taking on greater responsibilities in the future? If yes, "
            "what kind of responsibilities or opportunities would you consider suitable for them?"
        ),
        _open(
            "Is there anything about the employee's performance, behaviour, role fit, or "
            "development that HR should be aware of?"
        ),
        _section("Recommendation"),
        {
            "type": "choice",
            "label": "Your recommendation",
            "options": [
                "Confirm Employment",
                "Consider Extension of Probation with Clear Goals",
                "Terminate Employment or Reassign",
            ],
        },
    ],
}

HR: dict[str, Any] = {
    **_base(
        "Probation Review - HR Feedback",
        "probation_hr",
        [
            "Unsatisfactory",
            "Needs Improvement",
            "Meets Expectations",
            "Exceeds Expectations",
            "Outstanding",
        ],
        "HR's review of the employee's attendance, punctuality, policy adherence and conduct "
        "during their probation period.",
    ),
    "fields": [
        _section("Attendance & Punctuality"),
        _rating("Regularity and attendance during the probation period"),
        _rating("Punctuality and adherence to working hours"),
        _rating("Timely reporting of planned/unplanned absence"),
        _rating("Compliance with the company's leave and attendance process"),
        _rating("Consistency in attendance without frequent unplanned absences"),
        _section("Policy Adherence & Conduct"),
        _rating("Adherence to company policies and procedures"),
        _rating("Professional conduct and workplace behaviour"),
        _rating("Adherence to company rules and guidelines"),
        _rating("Respectful and appropriate behaviour with colleagues and stakeholders"),
        _rating("Compliance with HR processes and documentation requirements"),
        _rating("Responsiveness to feedback or concerns raised regarding conduct/policy adherence"),
        _section("Open questions"),
        _open(
            "Were there any significant concerns regarding the employee's attendance, "
            "punctuality, or leave patterns during the probation period? Please elaborate."
        ),
        _open(
            "Were there any concerns, incidents, or feedback related to the employee's adherence "
            "to company policies or workplace conduct? Please provide details."
        ),
        _open(
            "During the probation period, were any performance-related concerns regarding the "
            "employee communicated to HR by the reporting manager? If yes, please provide details."
        ),
    ],
}

FORMS = (SELF, MANAGER, HR)


async def seed(author_email: str) -> list[str]:
    async with SessionFactory() as session:
        employees = EmployeeRepository(session)
        author = await employees.find_by_work_email_insensitive(author_email.strip())
        if author is None or author.role not in (Role.ADMIN, Role.IT_ADMIN, Role.HR):
            raise SystemExit(f"{author_email} is not an HR or admin account in Avora.")
        caller = CurrentUser(employee_id=author.id, role=author.role, manager_id=author.manager_id)
        forms = FormRepository(session)
        service = FormService(
            forms,
            FormResponseRepository(session),
            employees,
            AuditRepository(session),
            ProbationFormLink(
                ProbationChecklistRepository(session),
                LeavePolicyRepository(session),
                AttendancePolicyRepository(session),
                employees,
            ),
        )
        existing = {f.title for f in await forms.list(include_drafts=True, include_archived=False)}
        done: list[str] = []
        for definition in FORMS:
            if definition["title"] in existing:
                done.append(f"skipped (already there): {definition['title']}")
                continue
            created = await service.create(caller, FormWrite.model_validate(definition))
            assert created.status is FormStatus.DRAFT
            done.append(f"created draft: {created.title} ({created.question_count} questions)")
        await session.commit()
    await engine.dispose()
    return done


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/seed_probation_forms.py <hr-or-admin-email>")
    for line in asyncio.run(seed(sys.argv[1])):
        print(line)
