"""Interpret and synchronize Faculty vacations from the legacy workbook."""

import calendar
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from ..models import Physician, Vacation
from ._mutation import (
    capture_calendar_range,
    require_scheduling_administrator,
    scheduling_operation,
)
from .vacations import create_vacation, remove_vacation
from .workbook_upload import (
    WorkbookImportError,
    validate_workbook_archive,
)

MONTH_ALIASES = {
    month: {
        calendar.month_name[month].upper(),
        calendar.month_abbr[month].upper(),
        "SEPT" if month == 9 else calendar.month_abbr[month].upper(),
    }
    for month in range(1, 13)
}
IGNORED_MARKERS = {"E": "event", "I": "interview"}
VACATION_MARKER = "V"


class VacationCalendarImportError(ValueError):
    """The uploaded workbook cannot be interpreted or safely synchronized."""


@dataclass(frozen=True)
class SourceFaculty:
    key: str
    workbook_name: str
    display_name: str


@dataclass(frozen=True)
class SourceVacation:
    faculty_key: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class IgnoredMarkerSummary:
    faculty_key: str
    marker: str
    dates: tuple[date, ...]


@dataclass(frozen=True)
class CalendarData:
    source_name: str
    start_date: date
    end_date: date
    faculty: tuple[SourceFaculty, ...]
    vacations: tuple[SourceVacation, ...]
    ignored_markers: tuple[IgnoredMarkerSummary, ...]


@dataclass(frozen=True)
class PlannedVacation:
    physician: Physician
    start_date: date
    end_date: date


@dataclass(frozen=True)
class ImportPlan:
    data: CalendarData
    matched_physicians: tuple[Physician, ...]
    create: tuple[PlannedVacation, ...]
    reuse: tuple[Vacation, ...]
    remove: tuple[Vacation, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def _clean_text(value):
    return " ".join(str(value or "").split())


def _source_display_name(value):
    value = _clean_text(value)
    if "," not in value:
        return value
    last_name, first_names = value.split(",", 1)
    return _clean_text(f"{first_names} {last_name}")


def _name_key(value):
    return re.sub(r"[^0-9a-z]+", " ", _source_display_name(value).casefold()).strip()


def _month_metadata(workbook):
    try:
        worksheet = workbook["`Mos.Yr"]
    except KeyError as error:
        raise VacationCalendarImportError(
            "Workbook is missing the `Mos.Yr month/year sheet."
        ) from error
    months = []
    for row in range(2, worksheet.max_row + 1):
        month_name = _clean_text(worksheet.cell(row, 1).value).upper()
        year_value = worksheet.cell(row, 2).value
        if not month_name and year_value is None:
            continue
        month_numbers = [
            month
            for month in range(1, 13)
            if month_name in MONTH_ALIASES[month]
            or month_name == calendar.month_name[month].upper()
        ]
        if len(month_numbers) != 1 or not isinstance(year_value, (int, float)):
            raise VacationCalendarImportError(f"Invalid month/year metadata on `Mos.Yr row {row}.")
        months.append((int(year_value), month_numbers[0]))
    if len(months) != 12:
        raise VacationCalendarImportError("Workbook must describe exactly 12 calendar months.")
    for previous, current in pairwise(months):
        expected = (previous[0] + (previous[1] == 12), previous[1] % 12 + 1)
        if current != expected:
            raise VacationCalendarImportError(
                "Workbook months must be consecutive and in calendar order."
            )
    return tuple(months)


def _calendar_sheet(workbook, month):
    matches = [
        worksheet
        for worksheet in workbook.worksheets
        if _clean_text(worksheet.title).upper() in MONTH_ALIASES[month]
    ]
    if len(matches) != 1:
        raise VacationCalendarImportError(
            f"Workbook must contain exactly one {calendar.month_name[month]} calendar sheet."
        )
    return matches[0]


def _date_header_row(worksheet, year, month):
    day_count = calendar.monthrange(year, month)[1]
    for row in range(1, min(worksheet.max_row, 20) + 1):
        values = [worksheet.cell(row, column).value for column in range(3, day_count + 3)]
        if values == list(range(1, day_count + 1)):
            return row
    raise VacationCalendarImportError(
        f"{worksheet.title} is missing the expected day-number header."
    )


def _faculty_header_row(worksheet):
    for row in range(1, min(worksheet.max_row, 100) + 1):
        if _clean_text(worksheet.cell(row, 2).value).upper() == "FACULTY:":
            return row
    raise VacationCalendarImportError(f"{worksheet.title} is missing its FACULTY section.")


def _faculty_rows(worksheet, header_row):
    rows = []
    for row in range(header_row + 1, min(worksheet.max_row, header_row + 60) + 1):
        name = _clean_text(worksheet.cell(row, 2).value)
        if not name:
            break
        rows.append((row, name))
    if not rows:
        raise VacationCalendarImportError(f"{worksheet.title} has no names in its FACULTY section.")
    return tuple(rows)


def _contiguous_ranges(dates):
    ordered = sorted(set(dates))
    if not ordered:
        return ()
    ranges = []
    start_date = previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            ranges.append((start_date, previous))
            start_date = current
        previous = current
    ranges.append((start_date, previous))
    return tuple(ranges)


def load_vacation_calendar(source, *, source_name=None):
    if source_name is None:
        source_name = (
            Path(os.fspath(source)).name if isinstance(source, (str, os.PathLike)) else None
        )
    source_name = Path(source_name or getattr(source, "name", "uploaded-workbook.xlsx")).name
    try:
        validate_workbook_archive(source, source_name)
    except WorkbookImportError as error:
        raise VacationCalendarImportError(str(error)) from error
    try:
        workbook = load_workbook(source, data_only=True, read_only=True, keep_links=False)
    except (BadZipFile, OSError, InvalidFileException, ValueError) as error:
        raise VacationCalendarImportError(
            f"Cannot read Excel workbook {source_name}: {error}"
        ) from error
    try:
        months = _month_metadata(workbook)
        faculty_by_key = {}
        vacation_dates = defaultdict(list)
        ignored_dates = defaultdict(list)
        for year, month in months:
            worksheet = _calendar_sheet(workbook, month)
            _date_header_row(worksheet, year, month)
            faculty_header_row = _faculty_header_row(worksheet)
            for row, workbook_name in _faculty_rows(worksheet, faculty_header_row):
                faculty_key = _name_key(workbook_name)
                if not faculty_key:
                    raise VacationCalendarImportError(
                        f"{worksheet.title}!B{row} has an invalid Faculty name."
                    )
                source = SourceFaculty(
                    key=faculty_key,
                    workbook_name=workbook_name,
                    display_name=_source_display_name(workbook_name),
                )
                previous = faculty_by_key.setdefault(faculty_key, source)
                if previous.display_name.casefold() != source.display_name.casefold():
                    raise VacationCalendarImportError(
                        f"Workbook uses conflicting Faculty names {previous.workbook_name!r} "
                        f"and {workbook_name!r}."
                    )
                for day in range(1, calendar.monthrange(year, month)[1] + 1):
                    value = _clean_text(worksheet.cell(row, day + 2).value).upper()
                    if not value:
                        continue
                    marker_date = date(year, month, day)
                    if value == VACATION_MARKER:
                        vacation_dates[faculty_key].append(marker_date)
                    elif value in IGNORED_MARKERS:
                        ignored_dates[(faculty_key, value)].append(marker_date)
                    else:
                        coordinate = worksheet.cell(row, day + 2).coordinate
                        raise VacationCalendarImportError(
                            f"Unknown Faculty marker {value!r} for {source.display_name} "
                            f"on {worksheet.title}!{coordinate}."
                        )
        vacations = tuple(
            SourceVacation(faculty_key, start_date, end_date)
            for faculty_key in sorted(vacation_dates)
            for start_date, end_date in _contiguous_ranges(vacation_dates[faculty_key])
        )
        ignored_markers = tuple(
            IgnoredMarkerSummary(faculty_key, marker, tuple(sorted(set(dates))))
            for (faculty_key, marker), dates in sorted(ignored_dates.items())
        )
        first_year, first_month = months[0]
        last_year, last_month = months[-1]
        return CalendarData(
            source_name=source_name,
            start_date=date(first_year, first_month, 1),
            end_date=date(last_year, last_month, calendar.monthrange(last_year, last_month)[1]),
            faculty=tuple(faculty_by_key[key] for key in sorted(faculty_by_key)),
            vacations=vacations,
            ignored_markers=ignored_markers,
        )
    finally:
        workbook.close()


def build_vacation_calendar_import_plan(data, *, replace):
    blockers = []
    warnings = []
    vacation_keys = {vacation.faculty_key for vacation in data.vacations}
    physicians_by_key = defaultdict(list)
    for physician in Physician.objects.order_by("display_name", "pk"):
        physicians_by_key[_name_key(physician.display_name)].append(physician)

    matched = {}
    for source in data.faculty:
        matches = physicians_by_key[source.key]
        if len(matches) > 1:
            blockers.append(f"{source.display_name}: physician identity is ambiguous.")
        elif matches:
            matched[source.key] = matches[0]
        elif source.key in vacation_keys:
            blockers.append(f"{source.display_name}: no matching physician exists.")
        else:
            warnings.append(
                f"Ignored unmatched Faculty name with no vacation markers: {source.display_name}."
            )

    desired = []
    for vacation in data.vacations:
        physician = matched.get(vacation.faculty_key)
        if physician is not None:
            desired.append(PlannedVacation(physician, vacation.start_date, vacation.end_date))
    desired.sort(key=lambda item: (item.physician.display_name, item.start_date, item.end_date))
    desired_signatures = {
        (
            item.physician.pk,
            item.start_date,
            item.end_date,
            Vacation.PeriodOfDay.WHOLE_DAY,
        )
        for item in desired
    }

    existing = list(
        Vacation.objects.overlapping(data.start_date, data.end_date)
        .filter(physician_id__in=[physician.pk for physician in matched.values()])
        .select_related("physician")
        .order_by("physician__display_name", "start_date", "end_date", "pk")
    )
    existing_by_signature = defaultdict(list)
    for vacation in existing:
        signature = (
            vacation.physician_id,
            vacation.start_date,
            vacation.end_date,
            vacation.period_of_day,
        )
        existing_by_signature[signature].append(vacation)

    create = []
    reuse = []
    remove = []
    for item in desired:
        signature = (
            item.physician.pk,
            item.start_date,
            item.end_date,
            Vacation.PeriodOfDay.WHOLE_DAY,
        )
        matches = existing_by_signature.get(signature, [])
        if matches:
            reuse.append(matches[0])
            duplicates = matches[1:]
            if duplicates and replace:
                remove.extend(duplicates)
            elif duplicates:
                blockers.append(
                    f"{item.physician.display_name}: duplicate existing vacation "
                    f"{item.start_date} through {item.end_date}."
                )
        else:
            create.append(item)

    for vacation in existing:
        signature = (
            vacation.physician_id,
            vacation.start_date,
            vacation.end_date,
            vacation.period_of_day,
        )
        if signature in desired_signatures:
            continue
        label = (
            f"{vacation.physician.display_name}: existing vacation {vacation.start_date} "
            f"through {vacation.end_date} ({vacation.get_period_of_day_display()})"
        )
        if vacation.start_date < data.start_date or vacation.end_date > data.end_date:
            blockers.append(f"{label} crosses the workbook boundary and cannot be replaced.")
        elif replace:
            remove.append(vacation)
        else:
            blockers.append(f"{label} differs from the workbook; preview again with --replace.")

    remove = sorted(
        {vacation.pk: vacation for vacation in remove}.values(),
        key=lambda item: (item.physician.display_name, item.start_date, item.end_date, item.pk),
    )
    return ImportPlan(
        data=data,
        matched_physicians=tuple(
            sorted(matched.values(), key=lambda physician: (physician.display_name, physician.pk))
        ),
        create=tuple(create),
        reuse=tuple(reuse),
        remove=tuple(remove),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def describe_vacation_calendar_import_plan(plan, *, replace):
    data = plan.data
    vacation_days = sum(
        (vacation.end_date - vacation.start_date).days + 1 for vacation in data.vacations
    )
    lines = [
        f"Workbook window: {data.start_date} through {data.end_date}",
        f"Faculty names: {len(data.faculty)}; matched physicians: {len(plan.matched_physicians)}",
        f"Vacation markers: {vacation_days} days in {len(data.vacations)} ranges",
        f"Mode: {'replace workbook-scoped Faculty vacations' if replace else 'additive/idempotent'}",
    ]
    ignored_totals = defaultdict(int)
    source_by_key = {source.key: source for source in data.faculty}
    for summary in data.ignored_markers:
        ignored_totals[summary.marker] += len(summary.dates)
    if ignored_totals:
        labels = ", ".join(
            f"{marker} ({IGNORED_MARKERS[marker]})={count}"
            for marker, count in sorted(ignored_totals.items())
        )
        lines.append(f"Ignored non-vacation markers: {labels}")
        for summary in data.ignored_markers:
            source = source_by_key[summary.faculty_key]
            lines.append(
                f"IGNORE {summary.marker}: {source.display_name} ({len(summary.dates)} days)"
            )
    for vacation in plan.remove:
        lines.append(
            f"REMOVE vacation: {vacation.physician.display_name} "
            f"{vacation.start_date} through {vacation.end_date} "
            f"({vacation.get_period_of_day_display()})"
        )
    for vacation in plan.reuse:
        lines.append(
            f"REUSE vacation: {vacation.physician.display_name} "
            f"{vacation.start_date} through {vacation.end_date}"
        )
    for vacation in plan.create:
        lines.append(
            f"CREATE vacation: {vacation.physician.display_name} "
            f"{vacation.start_date} through {vacation.end_date}"
        )
    return tuple(lines)


def apply_vacation_calendar_import(plan, *, scheduler):
    require_scheduling_administrator(scheduler)
    if plan.blockers:
        raise VacationCalendarImportError(
            "Cannot apply a Vacation calendar import while blockers remain."
        )
    note = (
        f"Imported from {plan.data.source_name}; workbook window "
        f"{plan.data.start_date} through {plan.data.end_date}."
    )
    with scheduling_operation():
        for vacation in (*plan.remove, *plan.create):
            capture_calendar_range(vacation.start_date, vacation.end_date)
        for vacation in plan.remove:
            remove_vacation(vacation=vacation, scheduler=scheduler)
        for vacation in plan.create:
            create_vacation(
                physician=vacation.physician,
                start_date=vacation.start_date,
                end_date=vacation.end_date,
                period_of_day=Vacation.PeriodOfDay.WHOLE_DAY,
                note=note,
                scheduler=scheduler,
            )
