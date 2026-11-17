"""Turning a playbook step into one concrete task. Pure, so every rule is
unit-tested directly.

A playbook is a checklist somebody writes ONCE and the system stamps out per
person: onboarding a joiner, closing a probation. Measured on the live
database before this was built, that work is being retyped by hand -
74 onboarding-shaped tasks for one HR lead across 41 distinct steps, 32 more
for another. The steps do not change; only the name does.

**Why this is deterministic and not inferred.** The accuracy of an automated
task comes from who supplied the intent. Here a human wrote the step and
chose the trigger, and the machine contributes exactly two things: a name
substitution and a date offset. There is no judgement in the loop, so there
is nothing to be wrong about - the same reasoning that kept
`app/core/recurrence.py` free of a model.

The rules, and why each is the way it is:

* **An unknown placeholder is left standing, not blanked.** `{nmae}` renders
  as `{nmae}` so the typo is visible in the task title. Blanking it would
  produce "Induction - " and read as deliberate. Publishing validates the
  placeholders, so this is the second line of defence, not the first.
* **A placeholder with nothing behind it renders as a readable fallback.** A
  joiner with no department on file gives "their department", never "None"
  and never an empty gap.
* **Offsets are counted from the run's anchor date**, not from the day the
  worker happened to run - the same reason recurrence steps from `last_due`.
  A playbook run on a joiner who started last Monday puts day-3 tasks on
  Thursday, not three days from now.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.playbook import PlaybookTrigger

#: What a step may refer to. Deliberately small: every one of these is a fact
#: already on the employee record, so none of them needs looking up or
#: guessing. Adding one means adding a column somebody maintains.
PLACEHOLDERS: frozenset[str] = frozenset(
    {"name", "first_name", "department", "job_title", "start_date"}
)

#: Shown when the field is empty on the employee record. Reads as prose in a
#: task title ("Set up their department access") instead of leaking "None".
_FALLBACK = {
    "name": "the new joiner",
    "first_name": "they",
    "department": "their department",
    "job_title": "their role",
    "start_date": "their start date",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

#: A step cannot be scheduled further out than this from its anchor. Catches a
#: fat-fingered 3000 before it puts a task eight years out.
MAX_OFFSET_DAYS = 365
MIN_OFFSET_DAYS = -365


@dataclass(frozen=True)
class Subject:
    """The person a run is about, reduced to the facts a step may use.

    A plain value rather than the ORM row, so the rendering rules are testable
    without a database and cannot accidentally reach for a column nobody
    validated.
    """

    full_name: str
    department: str | None = None
    job_title: str | None = None
    hire_date: date | None = None

    def value_for(self, placeholder: str) -> str | None:
        """This subject's value for one placeholder, or None when empty.

        A mapping rather than a chain of comparisons: it keeps the set of
        supported placeholders in one readable place next to PLACEHOLDERS,
        and an empty string and a missing field collapse to the same None so
        the caller has one case to handle.
        """
        whole = self.full_name.strip()
        known = {
            "name": whole,
            "first_name": whole.split(" ")[0] if whole else "",
            "department": (self.department or "").strip(),
            "job_title": (self.job_title or "").strip(),
            "start_date": self.hire_date.isoformat() if self.hire_date else "",
        }
        return known.get(placeholder) or None


def unknown_placeholders(template: str) -> list[str]:
    """Placeholders a step refers to that nothing can fill. Used at publish time, so
    the author is told rather than the joiner seeing `{nmae}` in a title."""
    return sorted({p for p in _PLACEHOLDER.findall(template) if p not in PLACEHOLDERS})


def render(template: str, subject: Subject) -> str:
    """A step's template with this subject's facts in it.

    Unknown placeholders are left exactly as written - see the module docstring.
    """

    def swap(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        if placeholder not in PLACEHOLDERS:
            return match.group(0)
        return subject.value_for(placeholder) or _FALLBACK[placeholder]

    return _PLACEHOLDER.sub(swap, template)


def due_on(anchor: date, offset_days: int) -> date:
    """When a step's task is due: the run's anchor plus the step's offset.

    Clamped, not rejected: a run must not fail part-way through because step 9
    of 11 carried a silly number, leaving the person with two thirds of a
    checklist and no way to tell which third is missing.
    """
    clamped = max(MIN_OFFSET_DAYS, min(MAX_OFFSET_DAYS, offset_days))
    return anchor + timedelta(days=clamped)


def opens_on(anchor: date, trigger_days: int | None) -> date:
    """The day an automatic playbook becomes due for a subject."""
    return anchor + timedelta(days=trigger_days or 0)


def is_due(
    hire_date: date | None,
    is_active: bool,
    auto_assign_since: date | None,
    trigger_days: int | None,
    today: date,
) -> bool:
    """Should the new-joiner playbook run for this person today?

    Plain values rather than ORM rows, so the rule is testable without a
    database - and the three conditions are each load-bearing:

    * **A joining date on record.** Without one there is no telling a new
      joiner from somebody HR added to Avora years late, so nobody is guessed
      at and HR runs it by hand instead.
    * **On or after `auto_assign_since`.** THE backfill guard. Publishing a
      playbook must not onboard all 29 people already on the roster.
    * **The offset has passed.** Day-0 fires on their start date, not when
      the record was created.
    """
    if hire_date is None or auto_assign_since is None or not is_active:
        return False
    return hire_date >= auto_assign_since and opens_on(hire_date, trigger_days) <= today


# --- repeating triggers ----------------------------------------------------- #
#
# A new-joiner playbook fires ONCE for a person, ever. A month-end compliance
# checklist fires once a MONTH, forever. Both are "automatic", and the only
# difference is what counts as a repeat - so uniqueness is keyed by a PERIOD
# rather than being hard-coded to once-ever.
#
# `period_key` is "" for a one-shot trigger and "YYYY-MM" for a monthly one.
# Putting it in the key is what lets the same run table serve both without a
# monthly playbook either refusing to run again or running twice in a day.

#: Triggers that repeat on a calendar period rather than firing once.
MONTHLY_TRIGGERS: frozenset[PlaybookTrigger] = frozenset(
    {PlaybookTrigger.MONTH_START, PlaybookTrigger.MONTH_END}
)


def last_day_of(year: int, month: int) -> date:
    """The last calendar day of a month - 28 or 29 February included."""
    return date(year, month, monthrange(year, month)[1])


def month_anchor(
    trigger: PlaybookTrigger, today: date, trigger_days: int | None
) -> date | None:
    """The day a monthly playbook's steps count from, or None if not due today.

    MONTH_START anchors on the 1st and fires `trigger_days` later, so a
    checklist whose steps are "GSTR 1 by the 11th, GSTR 3B by the 20th" is
    written as offsets from the 1st - which is how the deadlines are actually
    expressed.

    MONTH_END anchors on the last day and fires `trigger_days` BEFORE it, so
    "start the close three days out" is one number.
    """
    days = trigger_days or 0
    if trigger is PlaybookTrigger.MONTH_START:
        first = date(today.year, today.month, 1)
        return first if today == first + timedelta(days=days) else None
    if trigger is PlaybookTrigger.MONTH_END:
        last = last_day_of(today.year, today.month)
        return last if today == last - timedelta(days=days) else None
    return None


def period_key(trigger: PlaybookTrigger, anchor: date) -> str:
    """What counts as a repeat for this trigger.

    "" means once ever - a person is onboarded once, offboarded once. A
    monthly trigger is keyed by the month it belongs to, so November's close
    and December's are different runs and neither can happen twice.
    """
    return f"{anchor.year:04d}-{anchor.month:02d}" if trigger in MONTHLY_TRIGGERS else ""
