"""Every background worker that is supposed to be running, declared in one place.

This list is the point of the whole mechanism. Monitoring that you have to
remember to configure is monitoring that does not exist: the celebrations
scheduler was killed seven weeks before anyone noticed, because nothing anywhere
asserted that it ought to be alive. Its heartbeat URL was never set, so there was
no monitor to go red.

Adding a worker to the deployment means adding it here. A worker named here with
no heartbeat row has never run; one whose row has gone stale has stopped. Both
are reportable without anyone configuring anything per worker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    label: str
    # The module the deployment starts. Named explicitly rather than derived
    # from `name`: guessing one from the other is how a guard quietly stops
    # guarding (`autocheckout` does not appear in `auto_checkout_scheduler`).
    module: str
    # How long silence is normal. Generously above the worker's own tick, so a
    # slow tick or a restart is not reported as an outage.
    max_silence_minutes: int
    # What stops working when this one does - the sentence an alert should carry.
    impact: str


EXPECTED_WORKERS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        "celebrations",
        "Celebrations",
        "worker.celebrations_scheduler",
        90,
        "Birthday, work-anniversary and festival emails stop going out.",
    ),
    WorkerSpec(
        "eod",
        "EOD reports",
        "worker.eod_scheduler",
        180,
        "End-of-day reports are not drafted or sent.",
    ),
    WorkerSpec(
        "payroll",
        "Payroll",
        "worker.payroll_scheduler",
        180,
        "The payroll digest and auto-release stop running.",
    ),
    WorkerSpec(
        "autocheckout",
        "Auto checkout",
        "worker.auto_checkout_scheduler",
        180,
        "Forgotten clock-outs are never closed, so days stay open.",
    ),
    WorkerSpec(
        "task-escalation",
        "Task escalation",
        "worker.task_escalation_scheduler",
        180,
        "Overdue tasks are not escalated to managers.",
    ),
    WorkerSpec(
        "circle-documents",
        "Circle documents",
        "worker.circle_documents_scheduler",
        24 * 60,
        "HR documents stop syncing in from Circle.",
    ),
    WorkerSpec(
        "forms",
        "Onboarding forms",
        "worker.forms_scheduler",
        180,
        "Joiner and buddy feedback forms are never sent.",
    ),
    WorkerSpec(
        "ocr",
        "Screenshot OCR",
        "worker.ocr_worker",
        180,
        "Screenshots are not read, so work attribution stops.",
    ),
)

BY_NAME: dict[str, WorkerSpec] = {spec.name: spec for spec in EXPECTED_WORKERS}
