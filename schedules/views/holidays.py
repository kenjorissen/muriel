"""Holiday list, scheduling, and retained-history HTTP workflow."""

from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..forms.holidays import CalendarYearSelectionForm, HolidayOccurrenceForm
from ..models.holidays import HolidayOccurrence, HolidayOccurrenceChange
from ..services._mutation import can_manage_scheduling_administration
from ..services.holidays import (
    holiday_occurrences_for,
    holiday_projected_work_items,
    schedule_holiday_occurrence,
)
from ..services.operational_attention import organization_today
from ..services.work_remaining import (
    InclusiveDateRange,
    focused_work_remaining,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from .vacation_navigation import vacation_workspace_navigation


@login_required
def holiday_workspace(request):
    calendar_year = _requested_calendar_year(request)
    if calendar_year is None:
        return HttpResponseBadRequest("Choose a valid calendar year.")
    occurrences = tuple(
        holiday_occurrences_for(calendar_year).order_by("nominal_date", "holiday", "pk")
    )
    return render(
        request,
        "schedules/holiday_workspace.html",
        _workspace_context(
            calendar_year,
            occurrences,
            can_import=can_manage_scheduling_administration(request.user),
        ),
    )


@login_required
def holiday_occurrence_schedule(request, occurrence_id):
    occurrence = _manually_schedulable_occurrence(occurrence_id)
    if occurrence.state != HolidayOccurrence.State.UNSCHEDULED:
        raise Http404("Only an unresolved holiday can be scheduled.")
    return _occurrence_form(request, occurrence, is_schedule=True)


@login_required
def holiday_occurrence_correct(request, occurrence_id):
    occurrence = _manually_schedulable_occurrence(occurrence_id)
    if not occurrence.observed_date:
        raise Http404("Only a dated holiday can be edited.")
    return _occurrence_form(request, occurrence, is_schedule=False)


@login_required
def holiday_history(request):
    calendar_year = _requested_calendar_year(request)
    if calendar_year is None:
        return HttpResponseBadRequest("Choose a valid calendar year.")
    return render(
        request,
        "schedules/holiday_history.html",
        {
            "holiday_history_table": _holiday_history_table(
                HolidayOccurrenceChange.objects.filter(
                    recorded_value__calendar_year=calendar_year
                ).select_related("changed_by")
            ),
            "workspace_url": _workspace_url(calendar_year),
        },
    )


def _holiday_history_table(changes):
    return DataTable(
        identity="holiday-history",
        label="Holiday history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Change",
                "Prior value",
                "Recorded value",
                "Note",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    history_timestamp(change.changed_at),
                    DataTableCell(
                        change.changed_by.get_username() if change.changed_by else "System"
                    ),
                    DataTableCell(change.get_action_display(), kind="status", row_header=True),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.note or "Blank note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No holiday changes recorded.",
    )


def _occurrence_form(request, occurrence, *, is_schedule):
    calendar_year = _requested_calendar_year(request)
    if calendar_year != occurrence.calendar_year:
        calendar_year = occurrence.calendar_year
    initial = {"observed_date": occurrence.observed_date}
    if is_schedule and occurrence.nominal_date.weekday() < 5:
        initial["observed_date"] = occurrence.nominal_date
    form = HolidayOccurrenceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            schedule_holiday_occurrence(
                occurrence=occurrence,
                observed_date=form.cleaned_data["observed_date"],
                note=form.cleaned_data["note"],
                scheduler=request.user,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_workspace_url(calendar_year))
    action = "holiday-occurrence-schedule" if is_schedule else "holiday-occurrence-correct"
    return render(
        request,
        "schedules/holiday_occurrence_form.html",
        {
            "form": form,
            "occurrence": occurrence,
            "title": "Schedule holiday" if is_schedule else "Edit holiday",
            "submit_label": "Schedule" if is_schedule else "Save holiday",
            "form_action": f"{reverse(action, args=(occurrence.pk,))}?calendar_year={calendar_year}",
            "workspace_url": _workspace_url(calendar_year),
        },
    )


def _manually_schedulable_occurrence(occurrence_id):
    occurrence = get_object_or_404(HolidayOccurrence, pk=occurrence_id)
    if not occurrence.is_manually_schedulable:
        raise Http404("This computed holiday cannot be edited.")
    return occurrence


def _requested_calendar_year(request):
    value = request.GET.get("calendar_year")
    if not value:
        return organization_today().year
    try:
        calendar_year = int(value)
    except TypeError, ValueError:
        return None
    return calendar_year if 1 <= calendar_year <= 9999 else None


def _workspace_context(calendar_year, occurrences, *, can_import=False):
    visible_years = set(range(organization_today().year - 5, organization_today().year + 6))
    visible_years.add(calendar_year)
    work_remaining = _holiday_scoped_work_remaining(calendar_year, occurrences)
    work_keys = {item.key for item in work_remaining.items}
    return {
        "schedule_workspace_page_title": "Holidays",
        "schedule_workspace_label": "Vacation",
        "schedule_workspace_navigation": vacation_workspace_navigation(
            "holidays",
            calendar_year=calendar_year,
            can_import=can_import,
        ),
        "holiday_calendar_year": calendar_year,
        "holiday_calendar_year_form": CalendarYearSelectionForm(
            initial={"calendar_year": calendar_year}, years=visible_years
        ),
        "schedule_workspace_calendar_year_picker_action": reverse("holiday-workspace"),
        "schedule_workspace_previous_calendar_year_url": _workspace_url(calendar_year - 1),
        "schedule_workspace_next_calendar_year_url": _workspace_url(calendar_year + 1),
        "holiday_occurrences": occurrences,
        "holiday_occurrences_table": _holiday_occurrences_table(
            occurrences, calendar_year, work_keys
        ),
        "holiday_unscheduled_occurrences": tuple(
            occurrence
            for occurrence in occurrences
            if occurrence.state == HolidayOccurrence.State.UNSCHEDULED
        ),
        "work_remaining": work_remaining,
        "holiday_history_url": f"{reverse('holiday-history')}?calendar_year={calendar_year}",
    }


def _holiday_scoped_work_remaining(calendar_year, occurrences):
    selected_range = InclusiveDateRange(date(calendar_year, 1, 1), date(calendar_year, 12, 31))
    return focused_work_remaining(
        holiday_projected_work_items(occurrences),
        selected_range,
    )


def _holiday_occurrences_table(occurrences, calendar_year, work_keys):
    return DataTable(
        identity="holiday-occurrences",
        label=f"Holidays for {calendar_year}",
        width="compact",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Holiday", "Observed date", "Affected coverage", "Source", "Actions")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(occurrence.name, row_header=True),
                    DataTableCell(
                        occurrence.observed_date.strftime("%b %-d, %Y"),
                        kind="date",
                        machine_value=occurrence.observed_date.isoformat(),
                    )
                    if occurrence.observed_date
                    else DataTableCell(
                        "Not observed"
                        if occurrence.state == HolidayOccurrence.State.NOT_OBSERVED
                        else "Unscheduled",
                        kind="status",
                        tone="danger" if f"holiday-{occurrence.pk}" in work_keys else "",
                    ),
                    DataTableCell(occurrence.get_coverage_display()),
                    DataTableCell(occurrence.source),
                    DataTableCell(
                        "Schedule"
                        if occurrence.state == HolidayOccurrence.State.UNSCHEDULED
                        else "Edit",
                        kind="link",
                        url=(
                            f"{reverse('holiday-occurrence-schedule', args=(occurrence.pk,))}?calendar_year={calendar_year}"
                            if occurrence.state == HolidayOccurrence.State.UNSCHEDULED
                            else f"{reverse('holiday-occurrence-correct', args=(occurrence.pk,))}?calendar_year={calendar_year}"
                        ),
                    )
                    if occurrence.is_manually_schedulable
                    else DataTableCell("No action", kind="metadata"),
                ),
                tone="danger" if f"holiday-{occurrence.pk}" in work_keys else "",
            )
            for occurrence in occurrences
        ),
        empty_state=f"No holidays are configured for {calendar_year}.",
    )


def _workspace_url(calendar_year):
    return f"{reverse('holiday-workspace')}?calendar_year={calendar_year}"
