"""Preview or import a bounded Faculty roster and weekly templates from CSV."""

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.db.models.functions import Lower, Trim

from schedules.models import (
    CustomAvailabilityCategory,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
)
from schedules.services._mutation import can_manage_scheduling_administration, scheduling_operation
from schedules.services.availability_categories import (
    create_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)

ROUNDS_NAME = "Rounds"
EFFECTIVE_START_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
ROSTER_HEADERS = (
    "Physician",
    "Monday AM",
    "Monday PM",
    "Tuesday AM",
    "Tuesday PM",
    "Wednesday AM",
    "Wednesday PM",
    "Thursday AM",
    "Thursday PM",
    "Friday AM",
    "Friday PM",
)
SLOT_HEADERS = dict(zip(ROSTER_HEADERS[1:], FacultyAvailabilityTemplate.SLOT_CHOICES, strict=True))
ALLOWED_VALUES = {
    "Open": FacultyAvailabilityTemplate.AvailabilityState.OPEN,
    "Patient care": FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE,
    "Off": FacultyAvailabilityTemplate.AvailabilityState.OFF,
    ROUNDS_NAME: ROUNDS_NAME,
}


@dataclass(frozen=True)
class RosterRow:
    name: str
    values: dict[str, str]


@dataclass(frozen=True)
class PlannedPhysician:
    row: RosterRow
    physician: Physician | None
    create_physician: bool
    create_role: bool
    create_template: bool


@dataclass(frozen=True)
class ImportPlan:
    category: CustomAvailabilityCategory | None
    create_category: bool
    physicians: tuple[PlannedPhysician, ...]
    blockers: tuple[str, ...]


def _normalized_name(value):
    return value.strip().casefold()


def _load_roster(path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != list(ROSTER_HEADERS):
                raise CommandError(
                    "CSV columns must be exactly: " + ", ".join(ROSTER_HEADERS) + "."
                )
            rows = []
            seen_names = set()
            for line_number, raw_row in enumerate(reader, start=2):
                if None in raw_row or any(raw_row[header] is None for header in ROSTER_HEADERS):
                    raise CommandError(
                        f"CSV row {line_number} is missing one or more required slots."
                    )
                name = raw_row["Physician"].strip()
                if not name:
                    raise CommandError(f"CSV row {line_number} has a blank Physician.")
                normalized_name = _normalized_name(name)
                if normalized_name in seen_names:
                    raise CommandError(f"CSV contains duplicate physician name: {name}.")
                seen_names.add(normalized_name)
                values = {}
                for header in ROSTER_HEADERS[1:]:
                    value = raw_row[header].strip()
                    if not value:
                        raise CommandError(f"CSV row {line_number} has a missing {header} slot.")
                    if value not in ALLOWED_VALUES:
                        raise CommandError(
                            f"CSV row {line_number} has unknown {header} value: {value!r}."
                        )
                    values[SLOT_HEADERS[header][0]] = value
                rows.append(RosterRow(name=name, values=values))
    except OSError as error:
        raise CommandError(f"Cannot read CSV file {path}: {error}") from error
    if not rows:
        raise CommandError("CSV must contain at least one physician row.")
    return tuple(rows)


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


def _rounds_category():
    return list(
        CustomAvailabilityCategory.objects.annotate(normalized_name=Lower(Trim("name")))
        .filter(normalized_name=ROUNDS_NAME.lower())
        .order_by("pk")
    )


def _matching_physicians(name):
    return list(
        Physician.objects.annotate(normalized_name=Lower(Trim("display_name")))
        .filter(normalized_name=_normalized_name(name))
        .order_by("pk")
    )


def _has_ongoing_faculty_role(physician, effective_start):
    return EffectiveRole.objects.filter(
        physician=physician,
        role=EffectiveRole.Role.FACULTY,
        effective_start__lte=effective_start,
        effective_end__isnull=True,
    ).exists()


def _role_blocker(physician, effective_start):
    role = EffectiveRole(
        physician=physician,
        role=EffectiveRole.Role.FACULTY,
        effective_start=effective_start,
        effective_end=None,
    )
    try:
        role.full_clean()
    except ValidationError as error:
        return f"{physician.display_name}: Faculty role cannot start {effective_start}: {error}."
    return None


def _template_slots(row, category):
    slots = {}
    for slot, value in row.values.items():
        if value == ROUNDS_NAME:
            slots[slot] = FacultyAvailabilityTemplate.category_selection(category)
        else:
            slots[slot] = FacultyAvailabilityTemplate.base_selection(ALLOWED_VALUES[value])
    return slots


def _template_plan(physician, row, effective_start, category):
    overlaps = FacultyAvailabilityTemplate.objects.filter(physician=physician).filter(
        Q(effective_end__isnull=True) | Q(effective_end__gte=effective_start)
    )
    if category is not None or ROUNDS_NAME not in row.values.values():
        slots = _template_slots(row, category)
        ongoing_match = overlaps.filter(effective_end__isnull=True).filter(
            Q(effective_start__isnull=True) | Q(effective_start__lte=effective_start)
        )
        if any(template.slots == slots for template in ongoing_match):
            return False, None
    if overlaps.exists():
        return (
            False,
            f"{physician.display_name}: an existing Faculty template overlaps {effective_start}.",
        )
    return True, None


def _build_plan(rows, effective_start):
    blockers = []
    categories = _rounds_category()
    if len(categories) > 1:
        blockers.append("Rounds schedule option is ambiguous.")
        category = None
        create_category = False
    elif categories:
        category = categories[0]
        create_category = False
        if not category.is_active:
            blockers.append("Rounds schedule option is inactive.")
        elif category.impact != CustomAvailabilityCategory.Impact.NOT_AVAILABLE:
            blockers.append("Rounds schedule option must have Not available impact.")
    else:
        category = None
        create_category = True

    planned_physicians = []
    for row in rows:
        matches = _matching_physicians(row.name)
        if len(matches) > 1:
            blockers.append(f"{row.name}: physician identity is ambiguous.")
            continue
        physician = matches[0] if matches else None
        create_physician_record = physician is None
        create_role = create_physician_record or not _has_ongoing_faculty_role(
            physician, effective_start
        )
        if physician and create_role:
            blocker = _role_blocker(physician, effective_start)
            if blocker:
                blockers.append(blocker)
        create_template = True
        if physician:
            create_template, blocker = _template_plan(physician, row, effective_start, category)
            if blocker:
                blockers.append(blocker)
        planned_physicians.append(
            PlannedPhysician(
                row=row,
                physician=physician,
                create_physician=create_physician_record,
                create_role=create_role,
                create_template=create_template,
            )
        )
    return ImportPlan(
        category=category,
        create_category=create_category,
        physicians=tuple(planned_physicians),
        blockers=tuple(blockers),
    )


def _describe_plan(plan):
    lines = []
    if plan.create_category:
        lines.append("CREATE schedule option: Rounds (Not available)")
    else:
        lines.append("REUSE schedule option: Rounds")
    for planned in plan.physicians:
        name = planned.row.name
        lines.append(("CREATE" if planned.create_physician else "REUSE") + f" physician: {name}")
        lines.append(("CREATE" if planned.create_role else "REUSE") + f" Faculty role: {name}")
        lines.append(
            ("CREATE" if planned.create_template else "NO-OP") + f" Faculty template: {name}"
        )
    return lines


def _apply_plan(plan, scheduler, effective_start, reason):
    with scheduling_operation():
        category = plan.category
        if plan.create_category:
            category = create_custom_availability_category(
                name=ROUNDS_NAME,
                impact=CustomAvailabilityCategory.Impact.NOT_AVAILABLE,
                scheduler=scheduler,
                reason=reason,
            )
        physicians = []
        for planned in plan.physicians:
            physician = planned.physician
            if planned.create_physician:
                physician = create_physician(
                    display_name=planned.row.name,
                    scheduler=scheduler,
                    reason=reason,
                )
            if planned.create_role:
                add_role(
                    physician=physician,
                    role=EffectiveRole.Role.FACULTY,
                    effective_start=effective_start,
                    effective_end=None,
                    scheduler=scheduler,
                    reason=reason,
                )
            physicians.append((planned, physician))

        rounds_physicians = [
            physician
            for planned, physician in physicians
            if ROUNDS_NAME in planned.row.values.values()
        ]
        active_assignment_ids = set(
            category.assignments.filter(is_active=True).values_list("physician_id", flat=True)
        )
        if not {physician.pk for physician in rounds_physicians}.issubset(active_assignment_ids):
            existing_physicians = list(
                Physician.objects.filter(pk__in=active_assignment_ids).order_by("pk")
            )
            set_custom_availability_category_physicians(
                category=category,
                physicians=[*existing_physicians, *rounds_physicians],
                scheduler=scheduler,
                reason=reason,
            )

        for planned, physician in physicians:
            if planned.create_template:
                create_faculty_availability_template(
                    physician=physician,
                    effective_start=effective_start,
                    effective_end=None,
                    slots=_template_slots(planned.row, category),
                    scheduler=scheduler,
                    reason=reason,
                )


class Command(BaseCommand):
    help = "Preview or import a Faculty roster and weekly templates from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path)
        parser.add_argument("--effective-start", required=True, type=str)
        parser.add_argument("--scheduler", required=True, metavar="USERNAME")
        parser.add_argument("--apply", action="store_true", help="Apply the validated import.")

    def handle(self, *args, **options):
        effective_start_value = options["effective_start"]
        if not EFFECTIVE_START_PATTERN.fullmatch(effective_start_value):
            raise CommandError("--effective-start must use YYYY-MM-DD.")
        try:
            effective_start = date.fromisoformat(effective_start_value)
        except ValueError as error:
            raise CommandError("--effective-start must use YYYY-MM-DD.") from error
        path = options["csv_path"]
        rows = _load_roster(path)
        scheduler = _scheduler(options["scheduler"])
        plan = _build_plan(rows, effective_start)
        for line in _describe_plan(plan):
            self.stdout.write(line)
        for blocker in plan.blockers:
            self.stdout.write(self.style.ERROR(f"BLOCKER: {blocker}"))
        if not options["apply"]:
            self.stdout.write("Preview only; no changes were made.")
            return
        if plan.blockers:
            raise CommandError("Import blocked; no changes were made: " + "; ".join(plan.blockers))
        reason = f"Imported faculty template roster from {path.name}."
        try:
            _apply_plan(plan, scheduler, effective_start, reason)
        except (ValidationError, ValueError) as error:
            raise CommandError(f"Import blocked; no changes were made: {error}") from error
        self.stdout.write(self.style.SUCCESS("Faculty roster import applied."))
