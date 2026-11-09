"""Plan and apply dated annual Academic Event rows from the calendar workbook."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction

from ..models.academic_events import AcademicEvent, AcademicEventDefinition
from ..models.physicians import Physician
from ._mutation import scheduling_administration_mutation
from .academic_event_definition_import import (
    DefinitionImportData,
    SourceDefinition,
    build_academic_event_definition_import_plan,
)
from .academic_events import (
    correct_academic_event_occurrence,
    create_academic_event_occurrence,
    reverse_academic_event_not_occurring,
)


class AcademicEventScheduleImportError(ValueError):
    """The annual calendar rows cannot safely become retained occurrences."""


@dataclass(frozen=True)
class ScheduleTokenResolution:
    source: SourceDefinition
    token: str
    state: str
    physicians: tuple[Physician, ...] = ()


@dataclass(frozen=True)
class UnsupportedScheduleRow:
    source: SourceDefinition
    reason: str


@dataclass(frozen=True)
class PlannedOccurrence:
    source: SourceDefinition
    definition: AcademicEventDefinition
    date_ranges: tuple[tuple[date, date], ...]
    period_of_day: str
    participants: tuple[Physician, ...]
    all_available_faculty: bool
    event: AcademicEvent | None = None


@dataclass(frozen=True)
class AcademicEventScheduleImportPlan:
    data: DefinitionImportData
    exact: tuple[PlannedOccurrence, ...]
    correct: tuple[PlannedOccurrence, ...]
    create: tuple[PlannedOccurrence, ...]
    dated: tuple[SourceDefinition, ...]
    multi_range: tuple[SourceDefinition, ...]
    undated: tuple[SourceDefinition, ...]
    unsupported: tuple[UnsupportedScheduleRow, ...]
    unique_names: tuple[ScheduleTokenResolution, ...]
    ambiguous_names: tuple[ScheduleTokenResolution, ...]
    unmatched_names: tuple[ScheduleTokenResolution, ...]
    all_available_faculty: tuple[SourceDefinition, ...]
    ignored_residents: tuple[SourceDefinition, ...]
    blockers: tuple[str, ...]


def _name_key(value):
    value = " ".join(str(value or "").split())
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first} {last}"
    return re.sub(r"[^0-9a-z]+", " ", value.casefold()).strip()


def _contiguous_ranges(dates):
    dates = sorted(set(dates))
    if not dates:
        return ()
    result = []
    start = previous = dates[0]
    for current in dates[1:]:
        if current != previous + timedelta(days=1):
            result.append((start, previous))
            start = current
        previous = current
    result.append((start, previous))
    return tuple(result)


def _normalized_date(value, academic_year, source):
    try:
        result = date(
            academic_year if value.month >= 7 else academic_year + 1, value.month, value.day
        )
    except ValueError as error:
        raise AcademicEventScheduleImportError(
            f"Row {source.row_number} ({source.base_name}) cannot normalize {value.isoformat()} into the academic year."
        ) from error
    if not (date(academic_year, 7, 1) <= result <= date(academic_year + 1, 6, 30)):
        raise AcademicEventScheduleImportError(
            f"Row {source.row_number} ({source.base_name}) has a date outside the academic year."
        )
    return result


def _source_date_ranges(source, academic_year):
    if source.date_ranges:
        dates = []
        for source_start, source_end in source.date_ranges:
            start = _normalized_date(source_start, academic_year, source)
            end = _normalized_date(source_end, academic_year, source)
            if end < start:
                raise AcademicEventScheduleImportError(
                    f"Row {source.row_number} ({source.base_name}) ends before it starts."
                )
            dates.extend(start + timedelta(days=offset) for offset in range((end - start).days + 1))
        return _contiguous_ranges(dates)
    starts = tuple(_normalized_date(value, academic_year, source) for value in source.start_dates)
    ends = tuple(_normalized_date(value, academic_year, source) for value in source.end_dates)
    if not ends:
        return _contiguous_ranges(starts)
    if len(starts) != len(ends):
        raise AcademicEventScheduleImportError(
            f"Row {source.row_number} ({source.base_name}) has different numbers of start and end dates."
        )
    dates = []
    for start, end in zip(starts, ends, strict=True):
        if end < start:
            raise AcademicEventScheduleImportError(
                f"Row {source.row_number} ({source.base_name}) ends before it starts."
            )
        dates.extend(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return _contiguous_ranges(dates)


def _period(source):
    value = re.sub(r"[^a-z]+", " ", source.period.casefold()).strip()
    periods = {
        "": AcademicEvent.PeriodOfDay.WHOLE_DAY,
        "all day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
        "whole day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
        "full day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
        "am": AcademicEvent.PeriodOfDay.AM,
        "am only": AcademicEvent.PeriodOfDay.AM,
        "morning": AcademicEvent.PeriodOfDay.AM,
        "pm": AcademicEvent.PeriodOfDay.PM,
        "pm only": AcademicEvent.PeriodOfDay.PM,
        "afternoon": AcademicEvent.PeriodOfDay.PM,
    }
    try:
        return periods[value]
    except KeyError as error:
        raise AcademicEventScheduleImportError(
            f"Row {source.row_number} ({source.base_name}) has unsupported coverage {source.period!r}."
        ) from error


def _faculty_tokens(value, physicians_by_key):
    value = " ".join(value.split())
    if not value:
        return ()
    tokens = []
    for segment in re.split(r"[;\n/]+", value):
        segment = segment.strip()
        if _name_key(segment) in physicians_by_key:
            tokens.append(segment)
        else:
            tokens.extend(
                token.strip() for token in re.split(r",(?=\s*[^,]+)", segment) if token.strip()
            )
    return tuple(tokens)


def _faculty_matches(token, physicians_by_key, physicians_by_name_part):
    exact_matches = tuple(physicians_by_key[_name_key(token)])
    if exact_matches:
        return exact_matches
    # A lone first or last name is only useful when it identifies one current physician.
    key = _name_key(token)
    return tuple(physicians_by_name_part[key]) if len(key.split()) == 1 else ()


def _definition_matches(data):
    catalog = build_academic_event_definition_import_plan(data)
    matched = {item.source: item.definition for item in (*catalog.exact, *catalog.different)}
    blockers = list(catalog.blockers)
    if catalog.create:
        blockers.append("Apply the definition import before importing annual schedules.")
    if any(not item.rules for item in catalog.rule_different):
        blockers.append(
            "Apply the missing recurring definition rules before importing annual schedules."
        )
    return matched, tuple(blockers)


def build_academic_event_schedule_import_plan(data):
    """Use the definition parser's row identities to plan only explicit annual rows."""
    definitions, blockers = _definition_matches(data)
    physicians_by_key = defaultdict(list)
    physicians_by_name_part = defaultdict(list)
    for physician in Physician.objects.order_by("display_name", "pk"):
        physicians_by_key[_name_key(physician.display_name)].append(physician)
        for part in set(_name_key(physician.display_name).split()):
            physicians_by_name_part[part].append(physician)

    exact = []
    correct = []
    create = []
    dated = [
        item.source for item in data.unsupported if item.source.start_dates or item.source.end_dates
    ]
    multi_range = []
    undated = []
    unsupported = [UnsupportedScheduleRow(item.source, item.reason) for item in data.unsupported]
    unique_names = []
    ambiguous_names = []
    unmatched_names = []
    collective = []
    ignored_residents = []
    row_blockers = list(blockers)

    for source in data.definitions:
        definition = definitions.get(source)
        if (
            definition is not None
            and definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
        ):
            unsupported.append(
                UnsupportedScheduleRow(
                    source,
                    "Its Monthly rule owns occurrences; schedule import does not date it.",
                )
            )
            continue
        if not source.start_dates and not source.end_dates:
            undated.append(source)
            continue
        dated.append(source)
        if definition is None:
            unsupported.append(
                UnsupportedScheduleRow(
                    source, "Its event definition has not been applied or is ambiguous."
                )
            )
            continue
        if (
            definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.YEARLY
            or not definition.applies_to(data.academic_year)
        ):
            unsupported.append(
                UnsupportedScheduleRow(
                    source, "Its definition cannot have an annual occurrence this year."
                )
            )
            continue
        try:
            date_ranges = _source_date_ranges(source, data.academic_year)
            period_of_day = _period(source)
        except AcademicEventScheduleImportError as error:
            unsupported.append(UnsupportedScheduleRow(source, str(error)))
            row_blockers.append(str(error))
            continue
        if not date_ranges:
            undated.append(source)
            continue
        if len(date_ranges) > 1:
            multi_range.append(source)

        participants = []
        all_available = _name_key(source.faculty) == "all available faculty"
        if all_available:
            collective.append(source)
        else:
            for token in _faculty_tokens(source.faculty, physicians_by_key):
                matches = _faculty_matches(token, physicians_by_key, physicians_by_name_part)
                resolution = ScheduleTokenResolution(
                    source,
                    token,
                    "unique" if len(matches) == 1 else "ambiguous" if matches else "unmatched",
                    matches,
                )
                if len(matches) == 1:
                    unique_names.append(resolution)
                    participants.append(matches[0])
                elif matches:
                    ambiguous_names.append(resolution)
                    row_blockers.append(
                        f"Row {source.row_number} ({source.base_name}) has ambiguous Faculty name {token!r}."
                    )
                else:
                    unmatched_names.append(resolution)
                    row_blockers.append(
                        f"Row {source.row_number} ({source.base_name}) has no matching Faculty name {token!r}."
                    )
        if source.residents:
            ignored_residents.append(source)
        if any(item.source == source for item in (*ambiguous_names, *unmatched_names)):
            continue
        participants = tuple(
            sorted(
                {person.pk: person for person in participants}.values(),
                key=lambda person: person.pk,
            )
        )
        existing = (
            AcademicEvent.objects.filter(
                definition=definition, academic_year=data.academic_year, is_active=True
            )
            .prefetch_related("participants", "ranges")
            .first()
        )
        planned = PlannedOccurrence(
            source, definition, date_ranges, period_of_day, participants, all_available, existing
        )
        if existing is None or existing.resolution != AcademicEvent.Resolution.DATED:
            create.append(planned)
        elif _occurrence_matches(existing, planned):
            exact.append(planned)
        else:
            correct.append(planned)
    return AcademicEventScheduleImportPlan(
        data=data,
        exact=tuple(exact),
        correct=tuple(correct),
        create=tuple(create),
        dated=tuple(dated),
        multi_range=tuple(multi_range),
        undated=tuple(undated),
        unsupported=tuple(unsupported),
        unique_names=tuple(unique_names),
        ambiguous_names=tuple(ambiguous_names),
        unmatched_names=tuple(unmatched_names),
        all_available_faculty=tuple(collective),
        ignored_residents=tuple(ignored_residents),
        blockers=tuple(dict.fromkeys(row_blockers)),
    )


def _occurrence_matches(event, planned):
    return (
        event.date_ranges == planned.date_ranges
        and event.period_of_day == planned.period_of_day
        and event.suffix == planned.source.suffix
        and event.note == planned.source.note
        and event.all_available_faculty == planned.all_available_faculty
        and tuple(event.participants.order_by("pk").values_list("pk", flat=True))
        == tuple(person.pk for person in planned.participants)
    )


def _plan_signature(plan):
    def event_value(event):
        if event is None:
            return None
        return (
            event.pk,
            event.resolution,
            event.is_active,
            event.date_ranges,
            event.period_of_day,
            event.suffix,
            event.note,
            event.all_available_faculty,
            tuple(event.participants.order_by("pk").values_list("pk", flat=True)),
        )

    def values(item):
        return (
            item.source.row_number,
            item.definition.pk,
            event_value(item.event),
            item.date_ranges,
            item.period_of_day,
            item.source.suffix,
            item.source.note,
            item.all_available_faculty,
            tuple(person.pk for person in item.participants),
        )

    return (
        tuple(values(item) for item in plan.exact),
        tuple(values(item) for item in plan.correct),
        tuple(values(item) for item in plan.create),
        plan.blockers,
    )


def _reason(source, source_name):
    detail = f"Imported from {source_name}"
    return f"{detail}: {source.note}" if source.note else detail


@scheduling_administration_mutation
def apply_academic_event_schedule_import(*, plan, scheduler):
    if plan.blockers:
        raise AcademicEventScheduleImportError(
            "Cannot apply an annual schedule import while blockers remain."
        )
    fresh_plan = build_academic_event_schedule_import_plan(plan.data)
    if _plan_signature(fresh_plan) != _plan_signature(plan):
        raise AcademicEventScheduleImportError(
            "The annual schedule changed after preview. Preview the workbook again to review a fresh plan."
        )
    with transaction.atomic():
        for item in fresh_plan.create:
            if item.event and item.event.resolution == AcademicEvent.Resolution.NOT_OCCURRING:
                reverse_academic_event_not_occurring(event=item.event, scheduler=scheduler)
            create_academic_event_occurrence(
                definition=item.definition,
                academic_year=plan.data.academic_year,
                start_date=item.date_ranges[0][0],
                end_date=item.date_ranges[-1][1],
                date_ranges=item.date_ranges,
                participants=item.participants,
                all_available_faculty=item.all_available_faculty,
                suffix=item.source.suffix,
                note=item.source.note,
                period_of_day=item.period_of_day,
                scheduler=scheduler,
                reason=_reason(item.source, plan.data.source_name),
            )
        for item in fresh_plan.correct:
            correct_academic_event_occurrence(
                event=item.event,
                start_date=item.date_ranges[0][0],
                end_date=item.date_ranges[-1][1],
                date_ranges=item.date_ranges,
                participants=item.participants,
                all_available_faculty=item.all_available_faculty,
                suffix=item.source.suffix,
                note=item.source.note,
                period_of_day=item.period_of_day,
                scheduler=scheduler,
                reason=_reason(item.source, plan.data.source_name),
            )
