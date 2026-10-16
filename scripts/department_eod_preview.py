"""Render the per-department EOD rollup email to HTML files (dev / design).

Pure template rendering: no database, no LLM, no network, nothing sent. Use it to
look at the email while iterating on the layout, and to check how it degrades on
the awkward days - a department where nobody reported, a one-person department, a
long name, a department with no blockers.

    uv run python scripts/department_eod_preview.py [OUT_DIR]

Writes one file per sample plus an index.html that frames them side by side.
Defaults to ~/Downloads.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.schemas.eod import EodCumulativeTotals, EodDepartmentDigest, EodDepartmentMember
from app.services.email_templates import eod_department_digest_email
from app.services.eod_service import UNASSIGNED_DEPARTMENT

TODAY = datetime.now(UTC).date().isoformat()
DASHBOARD_URL = "https://avora.optiminastic.com/dashboard/me/department-eod"


def _member(name: str, coverage: str, **kw: object) -> EodDepartmentMember:
    """One roster row. Keyword-only beyond the name and coverage: the row has
    nine fields and positional arguments made the samples unreadable."""
    worked = int(kw.get("worked", 0) or 0)
    active = int(kw.get("active", 0) or 0)
    return EodDepartmentMember(
        employee_id=uuid.uuid4(),
        name=name,
        job_title=kw.get("title"),  # type: ignore[arg-type]
        coverage=coverage,
        worked_minutes=worked,
        active_minutes=active,
        active_pct=round(active / worked * 100) if worked else 0,
        tasks_done=int(kw.get("done", 0) or 0),
        blockers=int(kw.get("blockers", 0) or 0),
        tasks=list(kw.get("tasks", []) or []),  # type: ignore[arg-type]
        tasks_open=int(kw.get("open", 0) or 0),
        tasks_overdue=int(kw.get("overdue", 0) or 0),
    )


def _digest(
    department: str,
    members: list[EodDepartmentMember],
    blockers: list[str],
    *,
    from_date: str = TODAY,
    to_date: str = TODAY,
) -> EodDepartmentDigest:
    """Totals and coverage counts summed off the members, the same way
    `EodService._build_digests` does - so the preview cannot drift from the real
    numbers by hand-editing one and forgetting the other."""
    counts = {"submitted": 0, "pending": 0, "absent": 0, "failed": 0, "missing": 0}
    for m in members:
        counts[m.coverage] += 1
    return EodDepartmentDigest(
        department=department,
        from_date=from_date,
        to_date=to_date,
        member_count=len(members),
        submitted=counts["submitted"],
        pending=counts["pending"],
        absent=counts["absent"],
        failed=counts["failed"],
        missing=counts["missing"],
        submission_rate=counts["submitted"] / len(members) if members else 0.0,
        working_days=1,  # the preview is a single day
        submitted_days=counts["submitted"],
        totals=EodCumulativeTotals(
            worked_minutes=sum(m.worked_minutes for m in members),
            active_minutes=sum(m.active_minutes for m in members),
            tasks_done=sum(m.tasks_done for m in members),
            blockers=sum(m.blockers for m in members),
        ),
        members=members,
        blockers=blockers,
        open_tasks=sum(m.tasks_open for m in members),
        overdue_tasks=sum(m.tasks_overdue for m in members),
    )


def samples() -> list[tuple[str, EodDepartmentDigest]]:
    """The days worth looking at, not just the flattering one.

    Names are deliberately fictional placeholders. Using real colleagues here
    would put invented hours and activity percentages next to real people in a
    file that looks like a report - fine until someone screenshots it and it
    reads as a judgement on their week.
    """
    engineering = _digest(
        "Engineering",
        [
            _member(
                "Alex Rivera",
                "submitted",
                title="Backend Engineer",
                worked=492,
                active=401,
                done=4,
                blockers=1,
                open=7,
                overdue=2,
                tasks=[
                    "Ingest retry path",
                    "Webhook signature fix",
                    "Device reassign API",
                    "Agent heartbeat",
                ],
            ),
            _member(
                "Blair Okonkwo",
                "submitted",
                title="Frontend Engineer",
                worked=455,
                active=338,
                done=3,
                open=4,
                tasks=["Attendance report columns", "Leave picker fix", "Toolbar filters"],
            ),
            _member(
                "Casey Lindqvist",
                "submitted",
                title="DevOps",
                worked=521,
                active=390,
                done=5,
                blockers=1,
                open=9,
                overdue=3,
                tasks=[
                    "Coolify migration step",
                    "Registry cleanup",
                    "Worker heartbeats",
                    "TLS renewal",
                    "Backup verification",
                ],
            ),
            _member(
                "Dev Raman",
                "submitted",
                title="Full Stack",
                worked=388,
                active=250,
                done=2,
                open=3,
                tasks=["Payroll LOP recalc", "Payslip PDF spacing"],
            ),
            _member(
                "Elliot Marsh",
                "pending",
                title="QA",
                worked=410,
                active=300,
                done=1,
                open=5,
                overdue=1,
                tasks=["Regression sweep"],
            ),
            _member("Frankie Dubois", "absent", open=2),
            _member("Gale Whitfield", "missing", open=6, overdue=4),
        ],
        ["Staging DB credentials expired", "Waiting on the copy deck for onboarding"],
    )
    design = _digest(
        "Design",
        [
            _member(
                "Harper Nilsen",
                "submitted",
                title="Product Designer",
                worked=470,
                active=370,
                done=6,
                open=5,
                tasks=[
                    "Department EOD layout",
                    "Empty states",
                    "Coverage bar",
                    "Roster spacing",
                    "Icon pass",
                    "Dark mode check",
                ],
            ),
            _member(
                "Indra Castellanos",
                "submitted",
                title="Brand Designer",
                worked=430,
                active=300,
                done=2,
                blockers=1,
                open=6,
                overdue=1,
                tasks=["Brand refresh deck", "Type scale"],
            ),
        ],
        ["Blocked on the brand sign-off"],
    )
    quiet = _digest(
        "Finance",
        [
            _member("Jordan Abernathy-Reyes", "missing", title="Finance Lead", open=3, overdue=1),
            _member("Kai Thornberry", "absent"),
        ],
        [],
    )
    solo = _digest(
        "People",
        [
            _member(
                "Logan Pereira",
                "submitted",
                title="HR Manager",
                worked=465,
                active=355,
                done=3,
                open=2,
                tasks=["Probation letters", "Onboarding checklist", "Policy doc review"],
            )
        ],
        [],
    )
    unassigned = _digest(
        UNASSIGNED_DEPARTMENT,
        [
            _member(
                "Morgan Vasquez",
                "submitted",
                title="IT Admin",
                worked=600,
                active=520,
                done=8,
                blockers=2,
                open=11,
                overdue=4,
                tasks=["Fleet audit", "Device rebind", "Cert request", "Agent rollout"],
            ),
            _member("Noor Ellingham", "missing", open=1),
        ],
        ["Code-signing certificate still not issued"],
    )
    week_start = (date.fromisoformat(TODAY) - timedelta(days=6)).isoformat()
    window = _digest("Engineering", engineering.members, engineering.blockers, from_date=week_start)
    return [
        ("engineering", engineering),
        ("design", design),
        ("nobody-reported", quiet),
        ("one-person", solo),
        ("no-department", unassigned),
        ("seven-day-window", window),
    ]


_INDEX_CSS = """
body{margin:0;font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#1a1530;color:#f6f4ff;padding:24px}
h1{font:600 22px Georgia,serif;margin:0 0 4px}
p.sub{color:#b3a3f4;margin:0 0 20px;font-size:13px}
.row{display:flex;gap:18px;overflow-x:auto;padding-bottom:12px}
.col{flex:0 0 600px}
.cap{font:600 11px/1 sans-serif;letter-spacing:.12em;text-transform:uppercase;
color:#b3a3f4;margin:0 0 8px}
/* Must be at least the email's own max-width (560px) plus its gutters, or the
   roster's right-hand columns get clipped and the preview lies about the mail. */
iframe{width:600px;height:1000px;border:0;border-radius:10px;background:#fff}
"""


def main() -> None:
    out_dir = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Downloads"
    out_dir = out_dir / "avora-department-eod-preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    cards = ""
    for slug, digest in samples():
        subject, html = eod_department_digest_email(digest=digest, dashboard_url=DASHBOARD_URL)
        (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        cards += (
            f'<div class="col"><p class="cap">{slug}</p>'
            f'<iframe src="{slug}.html" title="{slug}"></iframe></div>'
        )
        print(f"{slug:18}  {subject}")

    (out_dir / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Department EOD preview</title>"
        f"<style>{_INDEX_CSS}</style>"
        "<h1>Department EOD rollup</h1>"
        "<p class='sub'>The email admins receive, one per department, at the send time. "
        "Scroll sideways for the awkward days.</p>"
        f"<div class='row'>{cards}</div>",
        encoding="utf-8",
    )
    print(f"\nOpen: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
