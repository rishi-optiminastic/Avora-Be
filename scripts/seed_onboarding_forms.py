"""Load the two onboarding feedback forms as DRAFTS, ready to review and publish.

- "New joiner feedback": opens for the joiner 15 days after their start date.
- "Buddy feedback": goes to the joiner's buddy 30 days after the start date.

Nothing is sent by this script. HR reviews each draft in Avora (Forms), edits
anything, and publishes it; only then does the daily job start sending, and
only to people who join on or after that day.

Safe to re-run: a form whose title already exists (draft or published) is
skipped. Runs as the given HR/admin, who is recorded as the author.

    uv run python scripts/seed_onboarding_forms.py hr@optiminastic.com
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

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

JOINER_DAYS = 15
BUDDY_DAYS = 30


def _rating(label: str) -> dict[str, Any]:
    return {"type": "rating", "label": label, "with_comment": True}


def _plain_rating(label: str) -> dict[str, Any]:
    return {"type": "rating", "label": label}


def _open(label: str) -> dict[str, Any]:
    return {"type": "paragraph", "label": label, "required": False}


def _section(label: str) -> dict[str, Any]:
    return {"type": "section", "label": label}


NEW_JOINER: dict[str, Any] = {
    "title": "New joiner feedback",
    "trigger": "new_joiner",
    "trigger_days": JOINER_DAYS,
    "due_days": 7,
    "rating_labels": ["Poor", "Below Average", "Average", "Good", "Excellent"],
    "intro": (
        "We want to make it easier to give us candid feedback about your first few weeks "
        "at Optimistic Media.\n\n"
        "We know our onboarding isn't perfect. In the past, we've received feedback that "
        "parts of the joining experience could feel information-heavy, that system access "
        "was not always ready early enough, and that new joiners weren't always sure who to "
        "approach for particular questions.\n\n"
        "We've also learned that having a Buddy doesn't automatically make settling in "
        "easier, we need to be clearer about what a Buddy should help with and make sure "
        "they have enough time to be available.\n\n"
        "These are areas we're actively working on. We'd therefore really value your "
        "perspective, including constructive or critical feedback. You won't offend us by "
        "identifying something that didn't work that information helps us improve the "
        "experience for the next person."
    ),
    "fields": [
        _section("Section A. Overall Onboarding Experience"),
        _rating(
            "We've previously heard that onboarding can sometimes feel like a lot of "
            "information arriving at once, and we're working on making the experience clearer "
            "and more paced.\n\n"
            "Thinking about your own experience, what worked well and what could we have done "
            "differently to help you settle in?\n\n"
            "Overall, how would you rate your onboarding experience?"
        ),
        _rating(
            "One development area we've identified is making the handover between your "
            "recruitment, HR, IT and the your team feel completely seamless.\n\n"
            "Were there any points in your joining process where you felt unclear about what "
            "was happening next, had to chase someone, or experienced unnecessary delays?\n\n"
            "How smooth and well-organised did the process feel overall?"
        ),
        _rating(
            "We've been thinking about whether our Welcome Kit is genuinely useful rather than "
            "simply a nice first-day gesture.\n\n"
            "What did you find genuinely useful or enjoyable about the Welcome Kit, and what if "
            "anything felt unnecessary or was missing?"
        ),
        _rating(
            "We've previously received feedback that access to tools, information or systems "
            "hasn't always been as timely as it should be. That's something we want to "
            "improve.\n\n"
            "Was there anything you needed in your first few days that you didn't have when "
            "you needed it?\n\n"
            "What information, resources or access would have helped you become productive "
            "sooner?"
        ),
        _rating(
            "We've learned that an induction can cover a lot of material without necessarily "
            "helping someone understand how the organisation actually works day to day.\n\n"
            "After your induction, what still felt unclear about us - our culture, "
            "expectations, policies, ways of working or processes?\n\n"
            "And what part of the induction helped you most?"
        ),
        _rating(
            "We recognise that feeling part of a new team can take longer than completing an "
            "onboarding checklist, and we've received feedback before that we could do more to "
            "help new joiners build relationships early.\n\n"
            "How included and comfortable do you currently feel within your team?\n\n"
            "Is there anything we could have done differently to help you feel part of the "
            "team sooner?"
        ),
        _section("Section B. Buddy System"),
        _rating(
            "One thing we're developing is the Buddy experience itself. We've learned that "
            "assigning a Buddy isn't enough, we need to make sure Buddies understand the role "
            "and have enough time to support a new colleague.\n\n"
            "What did your Buddy do that genuinely helped you settle in?\n\n"
            "Where could the Buddy support have been stronger?"
        ),
        _rating(
            "We've heard previously that Buddy availability can vary depending on workload, so "
            "we'd like to understand your experience rather than assume the system worked as "
            "intended.\n\n"
            "Were there moments when you wanted help from your Buddy but didn't feel able to "
            "ask, or they weren't available?\n\n"
            "What would have made it easier to approach them?"
        ),
        _rating(
            "We've realised that some of the hardest things for a new joiner to learn are the "
            "unwritten ones, how people communicate, where decisions happen, who to ask, and "
            "what's considered normal within the team.\n\n"
            "What unwritten aspects of working here did your Buddy help you understand?"
        ),
        _rating(
            "We're still improving how the Buddy programme works, so a lower rating is just as "
            "useful to us as a high one.\n\n"
            "Overall, how would you rate your Buddy experience?\n\n"
            "What is the main reason for your rating?"
        ),
        _rating(
            "We've received feedback that Buddies sometimes focus on answering immediate "
            "questions but could do more proactively to explain how the team and organisation "
            "work.\n\n"
            "Looking back, what do you wish your Buddy had proactively helped you understand or "
            "experience?"
        ),
        _section("Section C. Overall Feedback"),
        _open(
            "What should we definitely keep doing?\n\n"
            "Was there a particular person, moment, resource or part of the onboarding process "
            "that made joining us easier for you?"
        ),
        _open(
            "We've already identified some things we could do better including pacing "
            "information more effectively, making access smoother, clarifying Buddy "
            "responsibilities, and helping new joiners understand the informal ways of working "
            "sooner.\n\n"
            "But we're likely missing things that are more visible to someone experiencing "
            "onboarding for the first time.\n\n"
            "If you could change one thing about your onboarding experience for the next person "
            "joining us, what would you change?\n\n"
            "Please be as specific or constructively critical as you'd like."
        ),
    ],
}

BUDDY: dict[str, Any] = {
    "title": "Buddy feedback",
    "trigger": "buddy",
    "trigger_days": BUDDY_DAYS,
    "due_days": 7,
    "rating_labels": [
        "Needs Significant Support",
        "Needs Improvement",
        "On Track",
        "Good Progress",
        "Excellent Progress",
    ],
    "intro": (
        "As part of the Buddy System, request you to kindly fill in the below form and share "
        "your feedback on the new joinee's progress and your overall Buddy experience. Your "
        "inputs will help us strengthen the Buddy System going forward."
    ),
    "fields": [
        _plain_rating("How well has the new joiner settled into the team and workplace?"),
        _plain_rating(
            "How comfortable does the new joiner appear to be approaching colleagues, asking "
            "questions, and seeking clarification when needed?"
        ),
        _plain_rating(
            "How well does the new joiner seem to understand the team's ways of working, "
            "communication channels, and workplace practices?"
        ),
        _plain_rating(
            "How confident does the new joiner appear in navigating their day-to-day work "
            "environment?"
        ),
        _open(
            "What strengths or positive behaviours have you observed in the new joiner during "
            "their first month?"
        ),
        _open(
            "Where have you noticed the new joiner becoming more comfortable, confident, or "
            "independent over time?"
        ),
        _open(
            "Are there any areas where the new joiner may benefit from additional support, "
            "guidance, or context?"
        ),
        _open(
            "Is there anything the new joiner may not yet feel comfortable asking about or "
            "raising themselves?"
        ),
        _open(
            "What is one developmental suggestion you would give the new joiner to help them "
            "settle in and succeed more effectively?"
        ),
        _open(
            "Is there anything you think the manager or team could do to better support the new "
            "joiner at this stage?"
        ),
        _open(
            "We've identified areas we may need to improve, including clearer Buddy "
            "expectations, better guidance and making the role easier to manage alongside "
            "regular work. What is one thing you would change about the Buddy System for the "
            "next Buddy/new-joiner pair?"
        ),
        {
            "type": "choice",
            "label": "How comfortable would you feel taking on the Buddy role again?",
            "options": ["Definitely", "Probably", "Unsure", "Probably not", "Definitely not"],
        },
        _open(
            "What would make you more willing or better equipped to take on the Buddy role again?"
        ),
    ],
}


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
        for definition in (NEW_JOINER, BUDDY):
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
        raise SystemExit("usage: python scripts/seed_onboarding_forms.py <hr-or-admin-email>")
    for line in asyncio.run(seed(sys.argv[1])):
        print(line)
