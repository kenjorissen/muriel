"""Vacation source reads, lifecycle mutations, and retained history."""

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models.vacations import Vacation, VacationChange
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)


def vacations_overlapping(start_date, end_date):
    """Return Vacation Schedule facts that overlap an inclusive date range."""
    _validate_date_range(start_date, end_date)
    return Vacation.objects.overlapping(start_date, end_date).select_related("physician")


def vacation_covers(vacation, on_date, period_of_day):
    """Answer whether one Vacation Schedule fact covers an AM or PM session."""
    return vacation.covers(on_date, period_of_day)


@scheduling_mutation
@transaction.atomic
def create_vacation(
    *,
    physician,
    start_date,
    end_date,
    period_of_day=Vacation.PeriodOfDay.WHOLE_DAY,
    scheduler,
    note="",
):
    require_scheduler(scheduler)
    _validate_date_range(start_date, end_date)
    existing = Vacation.objects.filter(
        physician=physician,
        start_date=start_date,
        end_date=end_date,
        period_of_day=period_of_day,
    ).first()
    if existing:
        return existing
    capture_calendar_range(start_date, end_date)
    vacation = Vacation(
        physician=physician,
        start_date=start_date,
        end_date=end_date,
        period_of_day=period_of_day,
        note=normalize_note(note),
    )
    vacation.full_clean()
    vacation.save()
    _record_change(
        vacation=vacation,
        action=VacationChange.Action.CREATED,
        previous_value=None,
        recorded_value=_value(vacation),
        previous_note=None,
        recorded_note=vacation.note,
        scheduler=scheduler,
    )
    return vacation


@scheduling_mutation
@transaction.atomic
def correct_vacation(*, vacation, start_date, end_date, period_of_day, note="", scheduler):
    require_scheduler(scheduler)
    _validate_date_range(start_date, end_date)
    vacation = Vacation.objects.select_for_update().get(pk=vacation.pk)
    capture_calendar_range(vacation.start_date, vacation.end_date)
    capture_calendar_range(start_date, end_date)
    previous_value = _value(vacation)
    previous_note = vacation.note
    note = normalize_note(note)
    recorded_value = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "period_of_day": period_of_day,
    }
    if previous_value == recorded_value and previous_note == note:
        return vacation
    vacation.start_date = start_date
    vacation.end_date = end_date
    vacation.period_of_day = period_of_day
    vacation.note = note
    vacation.full_clean()
    vacation.save(update_fields=("start_date", "end_date", "period_of_day", "note"))
    _record_change(
        vacation=vacation,
        action=VacationChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_value(vacation),
        previous_note=previous_note,
        recorded_note=vacation.note,
        scheduler=scheduler,
    )
    return vacation


@scheduling_mutation
@transaction.atomic
def remove_vacation(*, vacation, scheduler):
    require_scheduler(scheduler)
    vacation = Vacation.objects.select_for_update().get(pk=vacation.pk)
    capture_calendar_range(vacation.start_date, vacation.end_date)
    _record_change(
        vacation=vacation,
        action=VacationChange.Action.REMOVED,
        previous_value=_value(vacation),
        recorded_value=None,
        previous_note=vacation.note,
        recorded_note=None,
        scheduler=scheduler,
    )
    vacation.delete()


def _validate_date_range(start_date, end_date):
    if not start_date or not end_date:
        raise ValidationError("Start and end dates are required.")
    if end_date < start_date:
        raise ValidationError({"end_date": "End date cannot be before start date."})


def _value(vacation):
    return {
        "start_date": vacation.start_date.isoformat(),
        "end_date": vacation.end_date.isoformat(),
        "period_of_day": vacation.period_of_day,
    }


def _record_change(
    *, vacation, action, previous_value, recorded_value, previous_note, recorded_note, scheduler
):
    return VacationChange.objects.create(
        vacation=vacation,
        vacation_id_at_recording=vacation.pk,
        physician=vacation.physician,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        previous_note=previous_note,
        recorded_note=recorded_note,
        changed_by=scheduler,
    )
