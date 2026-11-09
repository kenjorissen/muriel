"""Fixed clinic-holiday occurrence materialization and scheduling mutations."""

from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from ..models.holidays import ClinicHoliday, HolidayOccurrence, HolidayOccurrenceChange
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from .lead_times import lead_time_months
from .work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    shift_date_months,
)


@dataclass(frozen=True)
class HolidayProjection:
    """The one calendar and scheduling view of an observed clinic closure."""

    name: str
    observed_date: date
    coverage: str

    def closes(self, period_of_day):
        return self.coverage == HolidayOccurrence.Coverage.WHOLE_DAY or period_of_day == "PM"

    @property
    def coverage_label(self):
        return "AM and PM" if self.coverage == HolidayOccurrence.Coverage.WHOLE_DAY else "PM only"


def holiday_projections_for_dates(dates):
    """Return retained or default observed holidays for the supplied calendar dates.

    Defaults are projected without materializing records, so every scheduling read has
    the same closure answer while a holiday workspace remains the explicit writer.
    """
    dates = tuple(dict.fromkeys(dates))
    if not dates:
        return ()
    years = range(
        max(1, min(day.year for day in dates) - 1),
        min(9999, max(day.year for day in dates) + 1) + 1,
    )
    occurrences = {
        (occurrence.holiday, occurrence.calendar_year): occurrence
        for occurrence in HolidayOccurrence.objects.filter(calendar_year__in=years)
    }
    projections = []
    for calendar_year in years:
        for values in _default_occurrences(calendar_year):
            occurrence = occurrences.get((values["holiday"], calendar_year))
            observed_date = occurrence.observed_date if occurrence else values["observed_date"]
            state = occurrence.state if occurrence else values["state"]
            coverage = occurrence.coverage if occurrence else values["coverage"]
            if observed_date in dates and state in (
                HolidayOccurrence.State.COMPUTED,
                HolidayOccurrence.State.SCHEDULED,
            ):
                projections.append(
                    HolidayProjection(
                        occurrence.name if occurrence else values["name"], observed_date, coverage
                    )
                )
    return tuple(sorted(projections, key=lambda item: (item.observed_date, item.name)))


def holiday_closure_for(on_date, period_of_day):
    """Return the clinic closure affecting one AM or PM scheduling slot, if any."""
    return next(
        (
            holiday
            for holiday in holiday_projections_for_dates((on_date,))
            if holiday.closes(period_of_day)
        ),
        None,
    )


@scheduling_mutation
def holiday_occurrences_for(calendar_year):
    """Return retained occurrences for a calendar year, materializing missing defaults once."""
    if not isinstance(calendar_year, int) or calendar_year < 1:
        raise ValidationError({"calendar_year": "Calendar year must be a positive whole number."})
    for values in _default_occurrences(calendar_year):
        occurrence, created = HolidayOccurrence.objects.get_or_create(
            holiday=values["holiday"], calendar_year=calendar_year, defaults=values
        )
        if created:
            _record_change(
                occurrence=occurrence,
                action=HolidayOccurrenceChange.Action.MATERIALIZED,
                previous_value=None,
                recorded_value=_occurrence_value(occurrence),
                scheduler=None,
                note="",
            )
    return HolidayOccurrence.objects.filter(calendar_year=calendar_year)


@scheduling_mutation
@transaction.atomic
def schedule_holiday_occurrence(*, occurrence, observed_date, scheduler, note=""):
    """Schedule, reschedule, or correct a manually schedulable holiday occurrence."""
    require_scheduler(scheduler)
    occurrence = HolidayOccurrence.objects.select_for_update().get(pk=occurrence.pk)
    capture_calendar_range(occurrence.observed_date)
    capture_calendar_range(observed_date)
    if not occurrence.is_manually_schedulable:
        raise ValidationError("This computed holiday cannot be manually scheduled.")
    if not isinstance(observed_date, date):
        raise ValidationError({"observed_date": "Choose an observed date."})

    previous_value = _occurrence_value(occurrence)
    recorded_value = _occurrence_value(
        occurrence, observed_date=observed_date, state=HolidayOccurrence.State.SCHEDULED
    )
    if previous_value == recorded_value:
        return occurrence

    occurrence.observed_date = observed_date
    occurrence.state = HolidayOccurrence.State.SCHEDULED
    occurrence.full_clean()
    occurrence.save(update_fields=("observed_date", "state"))
    _record_change(
        occurrence=occurrence,
        action=(
            HolidayOccurrenceChange.Action.SCHEDULED
            if previous_value["state"] == HolidayOccurrence.State.UNSCHEDULED
            else HolidayOccurrenceChange.Action.CORRECTED
        ),
        previous_value=previous_value,
        recorded_value=_occurrence_value(occurrence),
        scheduler=scheduler,
        note=note,
    )
    return occurrence


def holiday_projected_work_items(occurrences):
    """Project unresolved manually schedulable holiday occurrences as work."""
    return tuple(
        ProjectedWorkItem(
            key=f"holiday-{occurrence.pk}",
            area="holidays",
            label=f"{occurrence.name} is unscheduled",
            affected_range=InclusiveDateRange(occurrence.nominal_date, occurrence.nominal_date),
            matters_on=shift_date_months(occurrence.nominal_date, -lead_time_months("holidays")),
            target_url=reverse("holiday-occurrence-schedule", args=(occurrence.pk,)),
            reasons=(ProjectedWorkReason("required", "Observed date needs scheduling", 1),),
        )
        for occurrence in occurrences
        if occurrence.is_manually_schedulable
        and occurrence.state == HolidayOccurrence.State.UNSCHEDULED
    )


def _default_occurrences(calendar_year):
    new_years_day = date(calendar_year, 1, 1)
    independence_day = date(calendar_year, 7, 4)
    christmas_eve = date(calendar_year, 12, 24)
    christmas_day = date(calendar_year, 12, 25)
    return (
        _dated(ClinicHoliday.NEW_YEARS_DAY, new_years_day, _weekday_observance(new_years_day)),
        _dated(
            ClinicHoliday.MEMORIAL_DAY,
            _last_weekday_in_month(calendar_year, 5, weekday=0),
        ),
        _independence_day(independence_day),
        _dated(ClinicHoliday.LABOR_DAY, _first_weekday_in_month(calendar_year, 9, weekday=0)),
        _dated(
            ClinicHoliday.THANKSGIVING_DAY,
            _nth_weekday_in_month(calendar_year, 11, weekday=3, occurrence=4),
        ),
        _christmas_eve(christmas_eve),
        _dated(ClinicHoliday.CHRISTMAS_DAY, christmas_day, _weekday_observance(christmas_day)),
    )


def _dated(holiday, nominal_date, observed_date=None):
    observed_date = observed_date or nominal_date
    return {
        "holiday": holiday,
        "calendar_year": nominal_date.year,
        "name": ClinicHoliday(holiday).label,
        "nominal_date": nominal_date,
        "observed_date": observed_date,
        "coverage": HolidayOccurrence.Coverage.WHOLE_DAY,
        "state": HolidayOccurrence.State.COMPUTED,
    }


def _independence_day(nominal_date):
    if nominal_date.weekday() < 5:
        return _dated(ClinicHoliday.INDEPENDENCE_DAY, nominal_date)
    return _undated(
        ClinicHoliday.INDEPENDENCE_DAY, nominal_date, HolidayOccurrence.State.UNSCHEDULED
    )


def _christmas_eve(nominal_date):
    if nominal_date.weekday() < 5:
        values = _dated(ClinicHoliday.CHRISTMAS_EVE, nominal_date)
        values["coverage"] = HolidayOccurrence.Coverage.PM
        return values
    return _undated(ClinicHoliday.CHRISTMAS_EVE, nominal_date, HolidayOccurrence.State.NOT_OBSERVED)


def _undated(holiday, nominal_date, state):
    return {
        "holiday": holiday,
        "calendar_year": nominal_date.year,
        "name": ClinicHoliday(holiday).label,
        "nominal_date": nominal_date,
        "observed_date": None,
        "coverage": (
            HolidayOccurrence.Coverage.PM
            if holiday == ClinicHoliday.CHRISTMAS_EVE
            else HolidayOccurrence.Coverage.WHOLE_DAY
        ),
        "state": state,
    }


def _weekday_observance(nominal_date):
    if nominal_date.weekday() == 5:
        return nominal_date - timedelta(days=1)
    if nominal_date.weekday() == 6:
        return nominal_date + timedelta(days=1)
    return nominal_date


def _first_weekday_in_month(year, month, weekday):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7)


def _last_weekday_in_month(year, month, weekday):
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _nth_weekday_in_month(year, month, weekday, occurrence):
    return _first_weekday_in_month(year, month, weekday) + timedelta(days=7 * (occurrence - 1))


def _occurrence_value(occurrence, *, observed_date=None, state=None):
    observed_date = occurrence.observed_date if observed_date is None else observed_date
    state = occurrence.state if state is None else state
    return {
        "holiday": occurrence.holiday,
        "calendar_year": occurrence.calendar_year,
        "name": occurrence.name,
        "nominal_date": occurrence.nominal_date.isoformat(),
        "observed_date": observed_date.isoformat() if observed_date else None,
        "coverage": occurrence.coverage,
        "state": state,
    }


def _record_change(*, occurrence, action, previous_value, recorded_value, scheduler, note):
    return HolidayOccurrenceChange.objects.create(
        occurrence=occurrence,
        occurrence_id_at_recording=occurrence.pk,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
        note=normalize_note(note),
    )
