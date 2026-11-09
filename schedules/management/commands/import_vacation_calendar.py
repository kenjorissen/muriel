"""Preview, import, or replace Faculty vacations from the legacy workbook."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from schedules.services._mutation import can_manage_scheduling_administration
from schedules.services.vacation_calendar_import import (
    VacationCalendarImportError,
    apply_vacation_calendar_import,
    build_vacation_calendar_import_plan,
    describe_vacation_calendar_import_plan,
    load_vacation_calendar,
)


def _scheduler(username):
    user_model = get_user_model()
    candidates = user_model.objects.filter(**{user_model.USERNAME_FIELD: username})
    count = candidates.count()
    if not count:
        raise CommandError(f"Scheduler {username!r} does not exist.")
    if count != 1:
        raise CommandError(f"Scheduler {username!r} is ambiguous.")
    scheduler = candidates.get()
    if not scheduler.is_active:
        raise CommandError(f"Scheduler {username!r} is inactive.")
    if not can_manage_scheduling_administration(scheduler):
        raise CommandError(f"Scheduler {username!r} lacks Scheduling Administrator authority.")
    return scheduler


class Command(BaseCommand):
    help = "Preview, import, or replace Faculty vacations from the legacy Excel calendar."

    def add_arguments(self, parser):
        parser.add_argument("xlsx_path", type=Path)
        parser.add_argument("--scheduler", required=True, metavar="USERNAME")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Synchronize matched Faculty vacations within the workbook's date window.",
        )
        parser.add_argument("--apply", action="store_true", help="Apply the validated import.")

    def handle(self, *args, **options):
        try:
            data = load_vacation_calendar(options["xlsx_path"])
        except VacationCalendarImportError as error:
            raise CommandError(str(error)) from error
        scheduler = _scheduler(options["scheduler"])
        plan = build_vacation_calendar_import_plan(data, replace=options["replace"])
        for line in describe_vacation_calendar_import_plan(plan, replace=options["replace"]):
            self.stdout.write(line)
        for warning in plan.warnings:
            self.stdout.write(self.style.WARNING(f"WARNING: {warning}"))
        for blocker in plan.blockers:
            self.stdout.write(self.style.ERROR(f"BLOCKER: {blocker}"))
        if not options["apply"]:
            self.stdout.write("Preview only; no changes were made.")
            return
        if plan.blockers:
            raise CommandError("Import blocked; no changes were made: " + "; ".join(plan.blockers))
        try:
            apply_vacation_calendar_import(plan, scheduler=scheduler)
        except (PermissionDenied, ValidationError, ValueError) as error:
            raise CommandError(f"Import blocked; no changes were made: {error}") from error
        self.stdout.write(
            self.style.SUCCESS(
                f"Vacation calendar import applied: created {len(plan.create)}, "
                f"reused {len(plan.reuse)}, removed {len(plan.remove)}."
            )
        )
