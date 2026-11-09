"""Vacation Schedule HTTP workflow and calendar presentation."""

import calendar
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models.functions import Lower
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..calendar_presentation import (
    CalendarAction,
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarWeek,
    VacationPresentation,
)
from ..forms.vacations import VacationForm
from ..models.vacations import Vacation, VacationChange
from ..physician_scope import physician_scope_ids
from ..services._mutation import can_manage_scheduling_administration
from ..services.operational_attention import organization_today
from ..services.vacations import (
    correct_vacation,
    create_vacation,
    remove_vacation,
    vacations_overlapping,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from ._shared import _requested_date, _requested_month, _shift_month
from .vacation_navigation import vacation_workspace_navigation


@login_required
def vacation_workspace(request):
    month = _vacation_requested_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    calendar_data = _vacation_calendar_data(month, physician_scope_ids(request))
    return render(
        request,
        "schedules/vacation_workspace.html",
        _vacation_workspace_context(
            month,
            "calendar",
            can_import=can_manage_scheduling_administration(request.user),
            **calendar_data,
        ),
    )


@login_required
def vacation_list(request):
    month = _vacation_requested_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    return render(
        request,
        "schedules/vacation_list.html",
        _vacation_workspace_context(
            month,
            "list",
            scope_ids=physician_scope_ids(request),
            can_import=can_manage_scheduling_administration(request.user),
        ),
    )


@login_required
def vacation_create(request):
    return_context = _vacation_return_context(request)
    form = VacationForm(request.POST or None, initial=_vacation_prefill(request))
    if request.method == "POST" and form.is_valid():
        try:
            create_vacation(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_vacation_return_url(return_context))
    return render(
        request,
        "schedules/vacation_form.html",
        {
            "form": form,
            "title": "Record vacation",
            "form_action": _vacation_action_url("vacation-create", return_context),
            "workspace_url": _vacation_return_url(return_context),
        },
    )


@login_required
def vacation_correct(request, vacation_id):
    vacation = get_object_or_404(Vacation, pk=vacation_id)
    return_context = _vacation_return_context(request)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        # The edit form displays the retained physician as fixed text; never trust a posted replacement.
        form_data["physician"] = vacation.physician_id
    form = VacationForm(
        form_data,
        initial={
            "physician": vacation.physician_id,
            "start_date": vacation.start_date,
            "end_date": vacation.end_date,
            "period_of_day": vacation.period_of_day,
            "note": vacation.note,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_vacation(
                vacation=vacation,
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
                period_of_day=form.cleaned_data["period_of_day"],
                note=form.cleaned_data["note"],
                scheduler=request.user,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_vacation_return_url(return_context))
    return render(
        request,
        "schedules/vacation_form.html",
        {
            "form": form,
            "title": "Edit vacation",
            "vacation": vacation,
            "form_action": _vacation_action_url("vacation-correct", return_context, vacation.pk),
            "remove_action": _vacation_action_url("vacation-remove", return_context, vacation.pk),
            "workspace_url": _vacation_return_url(return_context),
        },
    )


@login_required
def vacation_remove(request, vacation_id):
    vacation = get_object_or_404(Vacation, pk=vacation_id)
    return_context = _vacation_return_context(request)
    if request.method != "POST":
        return redirect(_vacation_action_url("vacation-correct", return_context, vacation.pk))
    remove_vacation(vacation=vacation, scheduler=request.user)
    return redirect(_vacation_return_url(return_context))


@login_required
def vacation_history(request):
    return render(
        request,
        "schedules/vacation_history.html",
        {
            "vacation_history_table": _vacation_history_table(
                VacationChange.objects.select_related("physician", "changed_by")
            ),
            "workspace_url": _vacation_return_url(_vacation_return_context(request)),
        },
    )


def _vacation_history_table(changes):
    return DataTable(
        identity="vacation-history",
        label="Vacation Schedule history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Physician",
                "Change",
                "Prior value",
                "Recorded value",
                "Prior note",
                "Recorded note",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.physician.display_name, row_header=True),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(
                        "No prior note."
                        if change.previous_note is None
                        else change.previous_note or "Blank note.",
                        kind="metadata",
                    ),
                    DataTableCell(
                        "No recorded note."
                        if change.recorded_note is None
                        else change.recorded_note or "Blank note.",
                        kind="metadata",
                    ),
                )
            )
            for change in changes
        ),
        empty_state="No vacation changes recorded.",
    )


def _vacation_requested_month(request):
    value = request.GET.get("month")
    month = _requested_month(value)
    return month if not value or month is not None else None


def _vacation_return_context(request):
    value = request.GET.get("month")
    month = _vacation_requested_month(request)
    return {
        "view": request.GET.get("view", "calendar"),
        "month": month if month and value == month.strftime("%Y-%m") else None,
        "view_was_provided": "view" in request.GET,
    }


def _vacation_workspace_url(view, month=None):
    view_name = "vacation-workspace" if view == "calendar" else "vacation-list"
    url = reverse(view_name)
    return f"{url}?month={month:%Y-%m}" if month else url


def _vacation_return_url(return_context):
    view = return_context["view"]
    month = return_context["month"]
    if view not in {"calendar", "list"}:
        view = "calendar"
    if return_context["view_was_provided"]:
        url = _vacation_workspace_url(view)
        return f"{url}?month={month:%Y-%m}" if month else url
    return _vacation_workspace_url(view, month)


def _vacation_action_url(view_name, return_context, *args):
    url = reverse(view_name, args=args)
    view = return_context["view"]
    month = return_context["month"]
    if view not in {"calendar", "list"}:
        view = "calendar"
    query = []
    if return_context["view_was_provided"]:
        query.append(f"view={view}")
    if month:
        query.append(f"month={month:%Y-%m}")
    return f"{url}?{'&'.join(query)}" if query else url


def _vacation_workspace_context(month, view, scope_ids=(), can_import=False, **calendar_data):
    today = organization_today()
    context = {
        "schedule_workspace_page_title": "Vacation",
        "schedule_workspace_label": "Vacation",
        "schedule_workspace_navigation": vacation_workspace_navigation(
            view,
            month=month,
            can_import=can_import,
        ),
        "schedule_workspace_selected_month": month,
        "schedule_workspace_previous_month_url": _vacation_workspace_url(
            view, _shift_month(month, -1)
        ),
        "schedule_workspace_next_month_url": _vacation_workspace_url(view, _shift_month(month, 1)),
        "schedule_workspace_month_picker_action": reverse(
            "vacation-workspace" if view == "calendar" else "vacation-list"
        ),
        "schedule_workspace_month_picker_preserved_query": {},
        "schedule_workspace_month_min": date(2000, 1, 1),
        "schedule_workspace_month_max": date(2100, 12, 1),
        "vacation_view": view,
        "vacation_add_url": (
            _vacation_action_url(
                "vacation-create", {"view": view, "month": month, "view_was_provided": True}
            )
            + f"&start_date={today:%Y-%m-%d}&end_date={today:%Y-%m-%d}"
        ),
        "vacation_history_url": _vacation_action_url(
            "vacation-history", {"view": view, "month": month, "view_was_provided": True}
        ),
        **calendar_data,
    }
    if view == "list":
        month_end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        rows = vacations_overlapping(month, month_end)
        if scope_ids:
            rows = rows.filter(physician_id__in=scope_ids)
        rows = rows.order_by(
            "start_date",
            "end_date",
            Lower("physician__display_name"),
            "physician__display_name",
            "pk",
        )
        context["vacation_list_rows"] = rows
        context["vacation_list_table"] = _vacation_list_table(rows, month)
    return context


def _vacation_list_table(rows, month):
    return DataTable(
        identity="vacation-list",
        label="Vacations",
        width="compact",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Physician", "Dates", "Coverage", "Note", "Actions")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        vacation.physician.display_name,
                        kind="link",
                        url=reverse("physician-detail", args=(vacation.physician_id,)),
                        row_header=True,
                    ),
                    DataTableCell(
                        f"{vacation.start_date:%b} {vacation.start_date.day}, {vacation.start_date.year} through {vacation.end_date:%b} {vacation.end_date.day}, {vacation.end_date.year}"
                    ),
                    DataTableCell(vacation.get_period_of_day_display()),
                    DataTableCell(
                        vacation.note or "No note", kind="metadata" if not vacation.note else "text"
                    ),
                    DataTableCell(
                        "Edit",
                        kind="link",
                        url=f"{reverse('vacation-correct', args=(vacation.pk,))}?view=list&month={month:%Y-%m}",
                    ),
                )
            )
            for vacation in rows
        ),
        empty_state=f"No vacations overlap {month:%B %Y}.",
    )


def _vacation_prefill(request):
    start_value = request.GET.get("start_date")
    end_value = request.GET.get("end_date")
    if not start_value and not end_value:
        today = organization_today()
        return {"start_date": today, "end_date": today}
    if not start_value or not end_value:
        return {}
    start_date = _requested_date(start_value)
    end_date = _requested_date(end_value)
    if start_date is None or end_date is None or start_date > end_date:
        return {}
    return {"start_date": start_date, "end_date": end_date}


def _vacation_calendar_data(month, scope_ids=()):
    displayed_weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    calendar_start = displayed_weeks[0][0]
    calendar_end = displayed_weeks[-1][-1]
    vacations = tuple(vacations_overlapping(calendar_start, calendar_end))
    if scope_ids:
        vacations = tuple(vacation for vacation in vacations if vacation.physician_id in scope_ids)
    weeks = tuple(
        _vacation_calendar_week(month, week_dates, vacations) for week_dates in displayed_weeks
    )
    return {
        "calendar_start": calendar_start,
        "calendar_end": calendar_end,
        "vacations": vacations,
        "weeks": weeks,
        "calendar": _vacation_calendar_lattice(month, weeks),
    }


def _vacation_calendar_week(month, week_dates, vacations):
    week_start = week_dates[0]
    week_end = week_dates[-1]
    segments = []
    for vacation in vacations:
        if vacation.start_date > week_end or vacation.end_date < week_start:
            continue
        start_date = max(vacation.start_date, week_start)
        end_date = min(vacation.end_date, week_end)
        coverage_label = (
            ""
            if vacation.period_of_day == Vacation.PeriodOfDay.WHOLE_DAY
            else vacation.period_of_day
        )
        physician_name = vacation.physician.display_name
        display_text = f"{physician_name} ({coverage_label})" if coverage_label else physician_name
        segments.append(
            {
                "vacation": vacation,
                "physician_name": physician_name,
                "start_date": start_date,
                "end_date": end_date,
                "coverage_label": coverage_label,
                "coverage_text": vacation.get_period_of_day_display(),
                "display_text": display_text,
                "accessible_label": (
                    f"{display_text}, {start_date.strftime('%B')} {start_date.day} through "
                    f"{end_date.strftime('%B')} {end_date.day}, {end_date.year}"
                ),
            }
        )

    segments.sort(
        key=lambda segment: (
            segment["physician_name"].casefold(),
            segment["physician_name"],
            segment["vacation"].pk,
            segment["start_date"],
            segment["end_date"],
            segment["coverage_label"],
        )
    )
    lanes = []
    for segment in segments:
        for lane_number, lane_segments in enumerate(lanes, start=1):
            if all(
                segment["start_date"] > occupied["end_date"]
                or segment["end_date"] < occupied["start_date"]
                for occupied in lane_segments
            ):
                lane_segments.append(segment)
                segment["lane"] = lane_number
                break
        else:
            lanes.append([segment])
            segment["lane"] = len(lanes)

        segment["start_column"] = (segment["start_date"] - week_start).days + 1
        segment["end_column"] = (segment["end_date"] - week_start).days + 2
        segment["day_count"] = (segment["end_date"] - segment["start_date"]).days + 1
        segment["grid_column"] = f"{segment['start_column']} / span {segment['day_count']}"

    days = tuple(
        {
            "date": day,
            "date_label": f"{day.strftime('%b')} {day.day}",
            "weekday_label": day.strftime("%A"),
            "in_selected_month": day.year == month.year and day.month == month.month,
        }
        for day in week_dates
    )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "week_label": (
            f"{week_start.strftime('%B')} {week_start.day} through "
            f"{week_end.strftime('%B')} {week_end.day}, {week_end.year}"
        ),
        "days": days,
        "segments": tuple(segments),
        "lane_count": len(lanes),
        "lane_rows": max(1, len(lanes)),
    }


def _vacation_calendar_lattice(month, weeks):
    return CalendarLattice(
        label=f"{month:%B %Y} vacation calendar",
        columns=("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
        variant="seven-column",
        has_gutter=False,
        holiday_header_labels=False,
        weeks=tuple(
            CalendarWeek(
                label=week["week_label"],
                days=tuple(
                    CalendarDay(
                        day["date"].isoformat(),
                        day["date_label"],
                        day["in_selected_month"],
                        CalendarAction(
                            (
                                f"{reverse('vacation-create')}?view=calendar"
                                f"&start_date={day['date']:%Y-%m-%d}&end_date={day['date']:%Y-%m-%d}"
                                f"&month={month:%Y-%m}"
                            ),
                            f"Add vacation for {day['date']:%B} {day['date'].day}, {day['date'].year}",
                            "",
                            f"vacation-add-{day['date']:%Y-%m-%d}",
                            "vacation-date-add-link",
                        ),
                    )
                    for day in week["days"]
                ),
                lanes=(
                    CalendarLane(
                        "Vacation entries",
                        tuple(
                            CalendarItem(
                                segment["start_column"],
                                segment["day_count"],
                                "schedules/calendar_vacation_item.html",
                                VacationPresentation(
                                    segment["physician_name"],
                                    segment["coverage_label"],
                                    segment["accessible_label"],
                                    f"{reverse('vacation-correct', args=(segment['vacation'].pk,))}"
                                    f"?view=calendar&month={month:%Y-%m}",
                                ),
                            )
                            for segment in week["segments"]
                        ),
                        variant="range",
                        holiday_items=True,
                    ),
                ),
            )
            for week in weeks
        ),
    )
