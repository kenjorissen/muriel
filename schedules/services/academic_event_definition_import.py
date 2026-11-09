"""Interpret the Academic Year Calendar definition catalog workbook."""

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zipfile import BadZipFile

from django.db import transaction
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from ..models.academic_events import (
    AcademicEvent,
    AcademicEventDefinition,
    AcademicEventMonthlyRule,
)
from ._mutation import scheduling_administration_mutation
from .academic_events import (
    create_academic_event_definition,
    create_academic_event_monthly_rule,
)
from .workbook_upload import WorkbookImportError, validate_workbook_archive

_YEAR_RANGE = re.compile(r"\b(20\d{2})\s*[-/]\s*(?:20)?(\d{2}|20\d{2})\b")
_QUARTER = re.compile(r"^Q([1-4])(?:\b|\s|[-:(])", re.IGNORECASE)
_QUARTER_SECTION = re.compile(r"^Q(\d+)(?:\b|\s|[-:(])", re.IGNORECASE)
_NAME_HEADERS = (
    # TEXT-CONTRACT: academic-event-import-event-column — The normalized `event` token
    # supports the accepted workbook's Event column. Scope: the case-insensitive,
    # whitespace-normalized header token only. Required by docs/design.md's Academic
    # Event import paragraph beginning “The importer selects exactly one worksheet with
    # a supported Event column”; protected independently by
    # schedules.tests.test_import_academic_event_definitions.AcademicEventDefinitionImportTests.test_parser_recognizes_faculty_meetings_and_preserves_other_rows_as_yearly.
    "event",
    "academic event",
    "event name",
    "name",
)
_DATE_HEADERS = ("date", "dates", "date(s)", "event date", "event dates")
_END_DATE_HEADERS = ("end date", "end dates", "date end")
_TYPICAL_HEADERS = ("typically scheduled", "typical schedule")
_FACULTY_HEADERS = (
    "faculty",
    "faculty attending",
    "faculty attendees",
    "faculty attendee",
    "attendees",
)
_RESIDENT_HEADERS = ("residents", "residents attending", "resident attendees", "resident attendee")
_DETAIL_HEADERS = ("detail", "details", "suffix", "event detail")
_NOTE_HEADERS = ("note", "notes", "notes/links")
_PERIOD_HEADERS = ("period", "coverage", "time", "am pm", "am/pm")
_ACADEMIC_YEAR_LABEL = re.compile(r"\bay\b|academic year|\b\d{2,4}\s*[-/]\s*\d{2,4}\b")
_TEXT_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?")
_TEXT_DATE_RANGE = re.compile(
    rf"(?P<start>{_TEXT_DATE.pattern})(?:\s+-\s+(?P<end>{_TEXT_DATE.pattern}))?"
)
_UNSUPPORTED = {
    "residency interviews": "Residency Interviews is handled by the dedicated Interviews workspace.",
}


class AcademicEventDefinitionImportError(ValueError):
    """The Academic Year Calendar cannot be interpreted or applied safely."""


@dataclass(frozen=True)
class SourceMonthlyRule:
    weekday: int
    ordinals: tuple[int, ...]
    effective_start: date
    effective_end: date | None = None


@dataclass(frozen=True)
class SourceDefinition:
    row_number: int
    base_name: str
    name_key: str
    expected_quarter: str | None
    default_duration_days: int
    typically_scheduled: str
    scheduling_mode: str = AcademicEventDefinition.SchedulingMode.YEARLY
    default_precepting_impact: str = AcademicEvent.PreceptingImpact.HARD
    all_available_faculty: bool = False
    monthly_rule: SourceMonthlyRule | None = None
    start_dates: tuple[date, ...] = ()
    end_dates: tuple[date, ...] = ()
    date_ranges: tuple[tuple[date, date], ...] = ()
    faculty: str = ""
    residents: str = ""
    suffix: str = ""
    note: str = ""
    period: str = ""


@dataclass(frozen=True)
class UnsupportedSourceDefinition:
    source: SourceDefinition
    reason: str


@dataclass(frozen=True)
class DefinitionImportData:
    source_name: str
    academic_year: int
    definitions: tuple[SourceDefinition, ...]
    unsupported: tuple[UnsupportedSourceDefinition, ...]


@dataclass(frozen=True)
class PlannedDefinition:
    source: SourceDefinition
    definition: AcademicEventDefinition
    differences: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedMonthlyRule:
    source: SourceDefinition
    definition: AcademicEventDefinition | None
    rules: tuple[AcademicEventMonthlyRule, ...] = ()
    differences: tuple[str, ...] = ()


@dataclass(frozen=True)
class DefinitionImportConflict:
    source: SourceDefinition
    message: str


@dataclass(frozen=True)
class DefinitionImportPlan:
    data: DefinitionImportData
    exact: tuple[PlannedDefinition, ...]
    different: tuple[PlannedDefinition, ...]
    create: tuple[SourceDefinition, ...]
    conflicts: tuple[DefinitionImportConflict, ...]
    rule_exact: tuple[PlannedMonthlyRule, ...]
    rule_different: tuple[PlannedMonthlyRule, ...]
    rule_create: tuple[SourceDefinition, ...]
    rule_conflicts: tuple[DefinitionImportConflict, ...]

    @property
    def blockers(self):
        return tuple(conflict.message for conflict in (*self.conflicts, *self.rule_conflicts))

    @property
    def conflict_count(self):
        return len(self.conflicts) + len(self.rule_conflicts)


def _clean_text(value):
    return " ".join(str(value or "").split())


def _name_key(value):
    return _clean_text(value).casefold()


def _header_key(value):
    return _name_key(value).rstrip(":")


def _date_column_header(value):
    return value in _DATE_HEADERS or bool(
        _ACADEMIC_YEAR_LABEL.search(value) and re.search(r"\bdates?\b", value)
    )


def _workbook_year(worksheet):
    candidates = []
    for value in (worksheet.title, *(cell.value for row in worksheet.iter_rows() for cell in row)):
        if not isinstance(value, str):
            continue
        match = _YEAR_RANGE.search(_clean_text(value))
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if end < 100:
            end += 2000
        if end != start + 1:
            raise AcademicEventDefinitionImportError(
                "Academic Year Calendar year labels must describe consecutive years."
            )
        candidates.append(start)
    if not candidates:
        raise AcademicEventDefinitionImportError(
            "Academic Year Calendar is missing its academic-year label, such as 2026-2027."
        )
    if len(set(candidates)) != 1:
        raise AcademicEventDefinitionImportError(
            "Academic Year Calendar contains conflicting academic-year labels."
        )
    return candidates[0]


def _calendar_sheet(workbook):
    sheets = []
    for worksheet in workbook.worksheets:
        try:
            headers = _column_headers(worksheet)
            academic_year = _workbook_year(worksheet)
        except AcademicEventDefinitionImportError:
            continue
        sheets.append((worksheet, headers, academic_year))
    if not sheets:
        raise AcademicEventDefinitionImportError(
            "Workbook must contain exactly one matching calendar worksheet with a supported "
            "Event column and a consecutive academic-year label, such as 2026-2027."
        )
    if len(sheets) > 1:
        raise AcademicEventDefinitionImportError(
            "Workbook contains multiple matching calendar worksheets with supported Event "
            "columns and consecutive academic-year labels."
        )
    return sheets[0]


def _column_headers(worksheet):
    headers = {}
    for row in range(1, min(worksheet.max_row, 30) + 1):
        row_headers = {
            _header_key(worksheet.cell(row, column).value): column
            for column in range(1, worksheet.max_column + 1)
            if _header_key(worksheet.cell(row, column).value)
        }
        if not set(_NAME_HEADERS).intersection(row_headers):
            continue
        name_header = next(header for header in _NAME_HEADERS if header in row_headers)
        headers = {
            "name": row_headers[name_header],
            "date": next(
                (column for header, column in row_headers.items() if _date_column_header(header)),
                None,
            ),
            "end_date": next(
                (row_headers[header] for header in _END_DATE_HEADERS if header in row_headers), None
            ),
            "typically_scheduled": next(
                (row_headers[header] for header in _TYPICAL_HEADERS if header in row_headers), None
            ),
            "faculty": next(
                (row_headers[header] for header in _FACULTY_HEADERS if header in row_headers), None
            ),
            "residents": next(
                (row_headers[header] for header in _RESIDENT_HEADERS if header in row_headers), None
            ),
            "suffix": next(
                (row_headers[header] for header in _DETAIL_HEADERS if header in row_headers), None
            ),
            "note": next(
                (row_headers[header] for header in _NOTE_HEADERS if header in row_headers), None
            ),
            "period": next(
                (row_headers[header] for header in _PERIOD_HEADERS if header in row_headers), None
            ),
            "row": row,
        }
        break
    if not headers:
        raise AcademicEventDefinitionImportError(
            "Academic Year Calendar is missing an Event, Academic Event, Event Name, or Name column."
        )
    return headers


def _quarter_in_row(worksheet, row):
    values = [
        _clean_text(worksheet.cell(row, column).value)
        for column in range(1, worksheet.max_column + 1)
    ]
    matches = []
    for value in values:
        section = _QUARTER_SECTION.match(value)
        if section and section.group(1) not in {"1", "2", "3", "4"}:
            raise AcademicEventDefinitionImportError(
                f"Academic Year Calendar row {row} has an unrecognized quarter section Q{section.group(1)}."
            )
        if match := _QUARTER.match(value):
            matches.append(match.group(1))
    if len(set(matches)) > 1:
        raise AcademicEventDefinitionImportError(
            f"Academic Year Calendar row {row} names multiple quarter sections."
        )
    return f"Q{matches[0]}" if matches else None


def _is_quarter_section_label(value):
    return bool(re.fullmatch(r"Q[1-4](?:\s*[:\-|].*|\s*\(.*\))?", value, re.IGNORECASE))


def _as_date(value, *, row_number, label):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise AcademicEventDefinitionImportError(
        f"Academic Year Calendar row {row_number} has an invalid {label}."
    )


def _dates(value, *, row_number, label):
    """Read one Excel date or a simple list of ISO or numeric calendar dates."""
    if value in (None, ""):
        return ()
    if isinstance(value, (datetime, date)):
        return (_as_date(value, row_number=row_number, label=label),)
    if not isinstance(value, str):
        raise AcademicEventDefinitionImportError(
            f"Academic Year Calendar row {row_number} has an invalid {label}."
        )
    values = []
    for item in re.findall(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?", value):
        try:
            if "-" in item:
                values.append(datetime.strptime(item, "%Y-%m-%d").date())
            else:
                parts = item.split("/")
                values.append(
                    date(
                        int(parts[2]) if len(parts) == 3 and len(parts[2]) == 4 else 2000,
                        int(parts[0]),
                        int(parts[1]),
                    )
                )
        except ValueError as error:
            raise AcademicEventDefinitionImportError(
                f"Academic Year Calendar row {row_number} has an invalid {label}."
            ) from error
    if not values:
        raise AcademicEventDefinitionImportError(
            f"Academic Year Calendar row {row_number} has an invalid {label}."
        )
    return tuple(values)


def _single_column_date_ranges(value, *, row_number):
    """Parse a date cell without confusing explicit spans with disjoint dates."""
    if value in (None, ""):
        return ()
    if isinstance(value, (datetime, date)):
        parsed = _as_date(value, row_number=row_number, label="event date")
        return ((parsed, parsed),)
    if not isinstance(value, str):
        raise AcademicEventDefinitionImportError(
            f"Academic Year Calendar row {row_number} has an invalid event date."
        )
    ranges = []
    for item in value.split(","):
        match = _TEXT_DATE_RANGE.fullmatch(item.strip())
        if not match:
            raise AcademicEventDefinitionImportError(
                f"Academic Year Calendar row {row_number} has an invalid event date."
            )
        dates = []
        for label, text in (("start", match.group("start")), ("end", match.group("end"))):
            if text is None:
                continue
            dates.append(_dates(text, row_number=row_number, label=f"event {label} date")[0])
        ranges.append((dates[0], dates[-1]))
    return tuple(ranges)


def _normalized_date(value, academic_year):
    return date(academic_year if value.month >= 7 else academic_year + 1, value.month, value.day)


def _duration_days(worksheet, row_number, headers, academic_year):
    if not headers["date"]:
        return 1
    if not headers["end_date"]:
        ranges = _single_column_date_ranges(
            worksheet.cell(row_number, headers["date"]).value,
            row_number=row_number,
        )
        if len(ranges) != 1:
            return 1
        start, end = (_normalized_date(value, academic_year) for value in ranges[0])
        if end < start:
            raise AcademicEventDefinitionImportError(
                f"Academic Year Calendar row {row_number} ends before it starts."
            )
        return (end - start).days + 1
    starts = _dates(
        worksheet.cell(row_number, headers["date"]).value,
        row_number=row_number,
        label="event date",
    )
    if not starts:
        return 1
    ends = _dates(
        worksheet.cell(row_number, headers["end_date"]).value if headers["end_date"] else None,
        row_number=row_number,
        label="event end date",
    )
    if not ends or len(starts) != 1 or len(ends) != 1:
        return 1
    start, end = starts[0], ends[0]
    if end < start:
        raise AcademicEventDefinitionImportError(
            f"Academic Year Calendar row {row_number} ends before it starts."
        )
    return (end - start).days + 1


def load_academic_event_definition_import(source, *, source_name=None):
    if source_name is None:
        source_name = (
            Path(os.fspath(source)).name if isinstance(source, (str, os.PathLike)) else None
        )
    source_name = Path(source_name or getattr(source, "name", "uploaded-workbook.xlsx")).name
    try:
        validate_workbook_archive(source, source_name)
    except WorkbookImportError as error:
        raise AcademicEventDefinitionImportError(str(error)) from error
    try:
        workbook = load_workbook(source, data_only=True, read_only=True, keep_links=False)
    except (BadZipFile, OSError, InvalidFileException, ValueError, KeyError) as error:
        raise AcademicEventDefinitionImportError(
            f"Cannot read Excel workbook {source_name}: {error}"
        ) from error
    try:
        worksheet, headers, academic_year = _calendar_sheet(workbook)
        definitions = []
        unsupported = []
        quarter = None
        for row in range(headers["row"] + 1, worksheet.max_row + 1):
            section_quarter = _quarter_in_row(worksheet, row)
            name = _clean_text(worksheet.cell(row, headers["name"]).value)
            if section_quarter and (not name or _is_quarter_section_label(name)):
                quarter = section_quarter
                continue
            if not name:
                continue
            if _header_key(name) in _NAME_HEADERS:
                continue
            is_faculty_meetings = _name_key(name) == "faculty meetings"
            if quarter is None and not is_faculty_meetings:
                raise AcademicEventDefinitionImportError(
                    f"Academic Year Calendar row {row} appears before a Q1-Q4 section."
                )
            source_definition = SourceDefinition(
                row_number=row,
                base_name=name,
                name_key=_name_key(name),
                expected_quarter=None if is_faculty_meetings else quarter,
                default_duration_days=_duration_days(worksheet, row, headers, academic_year),
                typically_scheduled=_clean_text(
                    worksheet.cell(row, headers["typically_scheduled"]).value
                    if headers["typically_scheduled"]
                    else ""
                ),
                scheduling_mode=(
                    AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
                    if is_faculty_meetings
                    else AcademicEventDefinition.SchedulingMode.YEARLY
                ),
                default_precepting_impact=(
                    AcademicEvent.PreceptingImpact.NONE
                    if is_faculty_meetings
                    else AcademicEvent.PreceptingImpact.HARD
                ),
                all_available_faculty=is_faculty_meetings,
                monthly_rule=(
                    SourceMonthlyRule(
                        weekday=3,
                        ordinals=(1, 3, 4, 5),
                        effective_start=date(academic_year, 7, 1),
                    )
                    if is_faculty_meetings
                    else None
                ),
                start_dates=(
                    tuple(
                        start
                        for start, _ in _single_column_date_ranges(
                            worksheet.cell(row, headers["date"]).value,
                            row_number=row,
                        )
                    )
                    if headers["date"] and not headers["end_date"]
                    else _dates(
                        worksheet.cell(row, headers["date"]).value,
                        row_number=row,
                        label="event date",
                    )
                    if headers["date"]
                    else ()
                ),
                end_dates=(
                    _dates(
                        worksheet.cell(row, headers["end_date"]).value,
                        row_number=row,
                        label="event end date",
                    )
                    if headers["end_date"]
                    else ()
                ),
                date_ranges=(
                    _single_column_date_ranges(
                        worksheet.cell(row, headers["date"]).value,
                        row_number=row,
                    )
                    if headers["date"] and not headers["end_date"]
                    else ()
                ),
                faculty=_clean_text(
                    worksheet.cell(row, headers["faculty"]).value if headers["faculty"] else ""
                ),
                residents=_clean_text(
                    worksheet.cell(row, headers["residents"]).value if headers["residents"] else ""
                ),
                suffix=_clean_text(
                    worksheet.cell(row, headers["suffix"]).value if headers["suffix"] else ""
                ),
                note=_clean_text(
                    worksheet.cell(row, headers["note"]).value if headers["note"] else ""
                ),
                period=_clean_text(
                    worksheet.cell(row, headers["period"]).value if headers["period"] else ""
                ),
            )
            reason = _UNSUPPORTED.get(source_definition.name_key)
            if reason:
                unsupported.append(UnsupportedSourceDefinition(source_definition, reason))
            else:
                definitions.append(source_definition)
        if not definitions and not unsupported:
            raise AcademicEventDefinitionImportError(
                "Academic Year Calendar contains no Academic Event definition rows."
            )
        return DefinitionImportData(
            source_name=source_name,
            academic_year=academic_year,
            definitions=tuple(definitions),
            unsupported=tuple(unsupported),
        )
    finally:
        workbook.close()


def _effective_years(start_year, end_year):
    return f"{start_year} onward" if end_year is None else f"{start_year}-{end_year}"


def _definition_differences(definition, source, academic_year):
    differences = []
    if definition.scheduling_mode != source.scheduling_mode:
        differences.append(
            "Scheduling mode: "
            f"Muriel {definition.get_scheduling_mode_display()}; "
            f"workbook {AcademicEventDefinition.SchedulingMode(source.scheduling_mode).label}"
        )
    if definition.expected_quarter != source.expected_quarter:
        differences.append(
            "Quarter: "
            f"Muriel {definition.expected_quarter or 'None'}; "
            f"workbook {source.expected_quarter or 'None'}"
        )
    if (definition.start_academic_year, definition.end_academic_year) != (academic_year, None):
        differences.append(
            "Effective years: "
            f"Muriel {_effective_years(definition.start_academic_year, definition.end_academic_year)}; "
            f"workbook {_effective_years(academic_year, None)}"
        )
    if definition.default_duration_days != source.default_duration_days:
        differences.append(
            "Default duration: "
            f"Muriel {definition.default_duration_days} day(s); "
            f"workbook {source.default_duration_days} day(s)"
        )
    if definition.default_period_of_day != AcademicEvent.PeriodOfDay.WHOLE_DAY:
        differences.append(
            f"Coverage: Muriel {definition.get_default_period_of_day_display()}; workbook Whole day"
        )
    if definition.default_precepting_impact != source.default_precepting_impact:
        differences.append(
            "Precepting impact: "
            f"Muriel {definition.get_default_precepting_impact_display()}; "
            f"workbook {AcademicEvent.PreceptingImpact(source.default_precepting_impact).label}"
        )
    if definition.default_required_participant_count != 1:
        differences.append(
            "Participant requirement: "
            f"Muriel {definition.default_required_participant_count}; workbook 1"
        )
    if definition.all_available_faculty != source.all_available_faculty:
        differences.append(
            "Faculty scope: "
            f"Muriel {'All available Faculty' if definition.all_available_faculty else 'Individual participants'}; "
            f"workbook {'All available Faculty' if source.all_available_faculty else 'Individual participants'}"
        )
    if definition.typically_scheduled != source.typically_scheduled:
        differences.append(
            "Typically scheduled: "
            f"Muriel {definition.typically_scheduled or 'Not specified'}; "
            f"workbook {source.typically_scheduled or 'Not specified'}"
        )
    return tuple(differences)


def build_academic_event_definition_import_plan(data):
    sources_by_name = defaultdict(list)
    for source in data.definitions:
        sources_by_name[source.name_key].append(source)
    existing_by_name = defaultdict(list)
    for definition in AcademicEventDefinition.objects.order_by("pk"):
        existing_by_name[_name_key(definition.base_name)].append(definition)

    exact = []
    different = []
    create = []
    conflicts = []
    matched_definitions = {}
    for name_key, sources in sources_by_name.items():
        existing = existing_by_name[name_key]
        repeated = len(sources) > 1
        source_quarters = Counter(source.expected_quarter for source in sources)
        for source in sources:
            if source_quarters[source.expected_quarter] > 1:
                conflicts.append(
                    DefinitionImportConflict(
                        source,
                        f"Row {source.row_number} ({source.base_name}): the name appears more than once in {source.expected_quarter}.",
                    )
                )
                continue
            matches = (
                [
                    definition
                    for definition in existing
                    if definition.expected_quarter == source.expected_quarter
                ]
                if repeated
                else existing
            )
            if len(matches) > 1:
                conflicts.append(
                    DefinitionImportConflict(
                        source,
                        f"Row {source.row_number} ({source.base_name}): existing definitions are ambiguous.",
                    )
                )
            elif not matches:
                create.append(source)
            else:
                matched_definitions[source] = matches[0]
                differences = _definition_differences(matches[0], source, data.academic_year)
                if differences:
                    different.append(PlannedDefinition(source, matches[0], differences))
                else:
                    exact.append(PlannedDefinition(source, matches[0]))
    rule_exact = []
    rule_different = []
    rule_create = []
    rule_conflicts = []
    conflicting_sources = {conflict.source for conflict in conflicts}
    for source in data.definitions:
        if source.monthly_rule is None:
            continue
        if source in conflicting_sources:
            rule_conflicts.append(
                DefinitionImportConflict(
                    source,
                    f"Row {source.row_number} ({source.base_name}): cannot identify a definition for its recurrence rule.",
                )
            )
            continue
        if source in create:
            rule_create.append(source)
            continue
        definition = matched_definitions[source]
        if definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.MONTHLY_RULES:
            rule_conflicts.append(
                DefinitionImportConflict(
                    source,
                    f"Row {source.row_number} ({source.base_name}): Muriel definition is not a Monthly-rules definition.",
                )
            )
            continue
        rules = tuple(definition.monthly_rules.order_by("weekday", "effective_start", "pk"))
        matching = tuple(
            rule
            for rule in rules
            if (
                rule.weekday == source.monthly_rule.weekday
                and tuple(sorted(rule.ordinals)) == source.monthly_rule.ordinals
                and rule.effective_start == source.monthly_rule.effective_start
                and rule.effective_end == source.monthly_rule.effective_end
            )
        )
        if len(matching) == 1:
            rule_exact.append(PlannedMonthlyRule(source, definition, matching))
        elif len(matching) > 1:
            rule_conflicts.append(
                DefinitionImportConflict(
                    source,
                    f"Row {source.row_number} ({source.base_name}): existing recurrence rules are ambiguous.",
                )
            )
        else:
            rule_different.append(
                PlannedMonthlyRule(
                    source,
                    definition,
                    rules,
                    (_monthly_rule_difference(rules, source.monthly_rule),),
                )
            )
    return DefinitionImportPlan(
        data=data,
        exact=tuple(exact),
        different=tuple(different),
        create=tuple(create),
        conflicts=tuple(conflicts),
        rule_exact=tuple(rule_exact),
        rule_different=tuple(rule_different),
        rule_create=tuple(rule_create),
        rule_conflicts=tuple(rule_conflicts),
    )


def _monthly_rule_difference(rules, source_rule):
    workbook = _monthly_rule_label(
        source_rule.weekday, source_rule.ordinals, source_rule.effective_start
    )
    if not rules:
        return f"Recurrence: Muriel has no rule; workbook {workbook}"
    muriel = "; ".join(
        _monthly_rule_label(
            rule.weekday, tuple(sorted(rule.ordinals)), rule.effective_start, rule.effective_end
        )
        for rule in rules
    )
    return f"Recurrence: Muriel {muriel}; workbook {workbook}"


def _monthly_rule_label(weekday, ordinals, effective_start, effective_end=None):
    weekday_label = AcademicEventMonthlyRule.WEEKDAY_CHOICES[weekday][1]
    ordinal_labels = ("first", "second", "third", "fourth", "fifth")
    ordinal_label = ", ".join(ordinal_labels[ordinal - 1] for ordinal in ordinals)
    effective_label = f"from {effective_start.isoformat()}"
    if effective_end:
        effective_label += f" through {effective_end.isoformat()}"
    return f"{ordinal_label} {weekday_label} {effective_label}"


def _plan_signature(plan):
    def source_value(source):
        return (
            source.row_number,
            source.name_key,
            source.expected_quarter,
            source.scheduling_mode,
            source.default_duration_days,
            source.default_precepting_impact,
            source.all_available_faculty,
            source.typically_scheduled,
            (
                source.monthly_rule.weekday,
                source.monthly_rule.ordinals,
                source.monthly_rule.effective_start,
                source.monthly_rule.effective_end,
            )
            if source.monthly_rule
            else None,
        )

    return (
        tuple((source_value(item.source), item.definition.pk) for item in plan.exact),
        tuple(
            (source_value(item.source), item.definition.pk, item.differences)
            for item in plan.different
        ),
        tuple(source_value(source) for source in plan.create),
        tuple((source_value(item.source), item.message) for item in plan.conflicts),
        tuple(
            (source_value(item.source), item.definition.pk, tuple(rule.pk for rule in item.rules))
            for item in plan.rule_exact
        ),
        tuple(
            (
                source_value(item.source),
                item.definition.pk,
                tuple(rule.pk for rule in item.rules),
                item.differences,
            )
            for item in plan.rule_different
        ),
        tuple(source_value(source) for source in plan.rule_create),
        tuple((source_value(item.source), item.message) for item in plan.rule_conflicts),
    )


@scheduling_administration_mutation
def apply_academic_event_definition_import(*, plan, scheduler):
    if plan.blockers:
        raise AcademicEventDefinitionImportError("Cannot apply an import while conflicts remain.")
    fresh_plan = build_academic_event_definition_import_plan(plan.data)
    if _plan_signature(fresh_plan) != _plan_signature(plan):
        raise AcademicEventDefinitionImportError(
            "The definition catalog changed after preview. Preview the workbook again to review a fresh plan."
        )
    with transaction.atomic():
        for source in fresh_plan.create:
            definition = create_academic_event_definition(
                base_name=source.base_name,
                scheduling_mode=source.scheduling_mode,
                start_academic_year=plan.data.academic_year,
                end_academic_year=None,
                expected_quarter=source.expected_quarter,
                default_duration_days=source.default_duration_days,
                default_period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
                default_precepting_impact=source.default_precepting_impact,
                default_required_participant_count=1,
                all_available_faculty=source.all_available_faculty,
                typically_scheduled=source.typically_scheduled,
                scheduler=scheduler,
            )
            if source.monthly_rule:
                create_academic_event_monthly_rule(
                    definition=definition,
                    weekday=source.monthly_rule.weekday,
                    ordinals=source.monthly_rule.ordinals,
                    effective_start=source.monthly_rule.effective_start,
                    effective_end=source.monthly_rule.effective_end,
                    scheduler=scheduler,
                )
