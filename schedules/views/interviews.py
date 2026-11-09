"""Interview Schedule HTTP workflow and calendar/list presentation."""

import calendar
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..academic_years import academic_year_bounds, academic_year_for_date
from ..calendar_presentation import (
    CalendarAction,
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarWeek,
    InterviewFindingPresentation,
    InterviewPresentation,
    finding_tone,
)
from ..forms.academic_events import AcademicYearSelectionForm
from ..forms.interviews import InterviewShiftForm
from ..models.interviews import InterviewShift
from ..physician_scope import physician_scope_ids
from ..services._mutation import scheduling_operation
from ..services.holidays import holiday_closure_for
from ..services.interviews import (
    assign_interview_shift,
    change_interview_shift_note,
    correct_interview_shift,
    create_interview_shift,
    interview_default_date,
    interview_schedule_summary,
    interview_shift_changes,
    interview_work_reasons,
    interview_workspace_default_month,
    remove_interview_shift,
    unassign_interview_shift,
)
from ..services.operational_attention import organization_today
from ..services.work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    focused_range_has_membership,
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
from ._shared import (
    _requested_month,
    academic_year_month,
    academic_year_month_navigation_context,
    academic_year_navigation_context,
    bounded_academic_years,
    requested_bounded_academic_year,
)


def _current_academic_year():
    from ..academic_years import academic_year_for_date

    return academic_year_for_date(organization_today())


@login_required
def interview_workspace(request):
    academic_year, month = _requested_interview_period(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    if month is None:
        return HttpResponseBadRequest("Choose a month within the selected academic year.")
    summary = interview_schedule_summary(academic_year)
    selected_range = _interview_selected_range(academic_year, month)
    work_remaining = _interview_scoped_work_remaining(summary, selected_range)
    focused_findings = _interview_focused_findings(summary.findings, selected_range)
    scope_ids = physician_scope_ids(request)
    return render(
        request,
        "schedules/interview_workspace.html",
        _interview_workspace_context(
            academic_year=academic_year,
            interview_view="calendar",
            month=month,
            summary=summary,
            work_remaining=work_remaining,
            focused_findings=focused_findings,
            **_interview_calendar_data(
                month,
                summary.shifts,
                academic_year,
                focused_findings,
                bool(work_remaining.items),
                selected_range,
                scope_ids,
            ),
        ),
    )


@login_required
def interview_list(request):
    academic_year = _requested_interview_year(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    scope_ids = physician_scope_ids(request)
    return render(
        request,
        "schedules/interview_list.html",
        _interview_workspace_context(
            academic_year=academic_year,
            interview_view="list",
            summary=interview_schedule_summary(academic_year),
            scope_ids=scope_ids,
        ),
    )


@login_required
def interview_create(request):
    academic_year = _requested_interview_year(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    return_context = _interview_return_context(request, academic_year)
    suggested_date = _interview_form_date(request) or interview_default_date(academic_year)
    form = InterviewShiftForm(
        request.POST or None,
        on_date=suggested_date,
        initial={"date": suggested_date},
    )
    if request.method == "POST" and form.is_valid():
        if InterviewShift.objects.filter(date=form.cleaned_data["date"], is_active=True).exists():
            form.add_error("date", "An active Interview shift is already scheduled for this date.")
        else:
            try:
                with scheduling_operation():
                    shift = create_interview_shift(
                        academic_year=academic_year,
                        date=form.cleaned_data["date"],
                        note=form.cleaned_data["note"],
                        scheduler=request.user,
                    )
                    _apply_interview_assignment(shift, form, request.user)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return redirect(_interview_return_url(return_context))
    return render(
        request,
        "schedules/interview_form.html",
        _interview_form_context(
            form=form,
            academic_year=academic_year,
            return_context=return_context,
            title="Schedule Interview",
            submit_label="Schedule Interview",
        ),
    )


@login_required
def interview_correct(request, shift_id):
    shift = get_object_or_404(InterviewShift, pk=shift_id, is_active=True)
    from ..academic_years import academic_year_for_date

    academic_year = academic_year_for_date(shift.date)
    return_context = _interview_return_context(request, academic_year)
    form_date = _interview_form_date(request) or shift.date
    form = InterviewShiftForm(
        request.POST or None,
        on_date=form_date,
        shift=shift,
        initial={"date": shift.date, "note": shift.note},
    )
    if request.method == "POST" and form.is_valid():
        try:
            with scheduling_operation():
                shift = correct_interview_shift(
                    shift=shift,
                    academic_year=academic_year,
                    date=form.cleaned_data["date"],
                    scheduler=request.user,
                )
                _apply_interview_assignment(shift, form, request.user)
                change_interview_shift_note(
                    shift=shift, note=form.cleaned_data["note"], scheduler=request.user
                )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_interview_return_url(return_context))
    return render(
        request,
        "schedules/interview_form.html",
        _interview_form_context(
            form=form,
            academic_year=academic_year,
            return_context=return_context,
            title="Edit Interview",
            submit_label="Save Interview",
            shift=shift,
        ),
    )


@login_required
def interview_remove(request, shift_id):
    shift = get_object_or_404(InterviewShift, pk=shift_id, is_active=True)
    from ..academic_years import academic_year_for_date

    return_context = _interview_return_context(request, academic_year_for_date(shift.date))
    if request.method != "POST":
        return redirect(_interview_action_url("interview-correct", return_context, shift.pk))
    remove_interview_shift(shift=shift, scheduler=request.user)
    return redirect(_interview_return_url(return_context))


@login_required
def interview_history(request):
    return_url = reverse("interview-workspace")
    if request.GET.get("academic_year") or request.GET.get("month"):
        academic_year, month = _requested_interview_period(request)
        if academic_year is None or month is None:
            return HttpResponseBadRequest("Choose a valid academic year and month.")
        return_url = _interview_workspace_url(academic_year, "calendar", month)
    return render(
        request,
        "schedules/interview_history.html",
        {
            "interview_history_table": _interview_history_table(interview_shift_changes()),
            "return_url": return_url,
        },
    )


def _interview_history_table(changes):
    return DataTable(
        identity="interview-history",
        label="Interview history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Shift",
                "Change",
                "Prior value",
                "Recorded value",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(
                        str(change.shift)
                        if change.shift
                        else f"Interview shift #{change.shift_id_at_recording}",
                        row_header=True,
                    ),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                )
            )
            for change in changes
        ),
        empty_state="No Interview changes recorded.",
    )


def _apply_interview_assignment(shift, form, scheduler):
    physician = form.cleaned_data["physician"]
    if physician is None:
        return unassign_interview_shift(shift=shift, scheduler=scheduler)
    return assign_interview_shift(
        shift=shift,
        physician=physician,
        override=form.fields["physician"].requires_override(physician),
        scheduler=scheduler,
    )


def _interview_form_context(
    *, form, academic_year, return_context, title, submit_label, shift=None
):
    return {
        "form": form,
        "title": title,
        "submit_label": submit_label,
        "shift": shift,
        "academic_year": academic_year,
        "interview_return_url": _interview_return_url(return_context),
        "form_action": _interview_action_url(
            "interview-correct" if shift else "interview-create",
            return_context,
            *((shift.pk,) if shift else ()),
        ),
        "remove_action": (
            _interview_action_url("interview-remove", return_context, shift.pk) if shift else None
        ),
    }


def _requested_interview_year(request):
    return requested_bounded_academic_year(request, _current_academic_year())


def _requested_interview_period(request):
    # The shared picker distinguishes direct year changes from month navigation.
    from ..academic_years import academic_year_for_date

    picker = request.GET.get("picker")
    if picker == "academic_year":
        academic_year = _requested_interview_year(request)
        if academic_year is None:
            return None, None
        value = request.GET.get("month")
        if not value:
            return academic_year, interview_workspace_default_month(
                academic_year, organization_today()
            )
        month = _requested_month(value)
        if month is None or month.strftime("%Y-%m") != value:
            return academic_year, None
        return academic_year, month.replace(
            year=academic_year if month.month >= 7 else academic_year + 1
        )
    if picker == "month":
        month = _requested_month(request.GET.get("month"))
        if month is None or month.strftime("%Y-%m") != request.GET.get("month"):
            return None, None
        academic_year = academic_year_for_date(month)
        return (
            (academic_year, month)
            if academic_year in bounded_academic_years(_current_academic_year())
            else (None, None)
        )
    if picker:
        return None, None
    academic_year = _requested_interview_year(request)
    if academic_year is None:
        return None, None
    value = request.GET.get("month")
    if value:
        month = _requested_month(value)
        if month is None or month.strftime("%Y-%m") != value:
            return academic_year, None
    else:
        month = interview_workspace_default_month(academic_year, organization_today())
    return (
        (academic_year, month)
        if academic_year_for_date(month) == academic_year
        else (academic_year, None)
    )


def _interview_return_context(request, academic_year):
    view = request.GET.get("view", "calendar")
    if view not in {"calendar", "list"}:
        view = "calendar"
    month = None
    if view == "calendar":
        value = request.GET.get("month")
        candidate = _requested_month(value) if value else None
        if candidate and candidate.strftime("%Y-%m") == value:
            month = candidate
    return {"academic_year": academic_year, "view": view, "month": month}


def _interview_workspace_url(academic_year, view="calendar", month=None):
    view_name = "interview-list" if view == "list" else "interview-workspace"
    url = f"{reverse(view_name)}?academic_year={academic_year}"
    return f"{url}&month={month:%Y-%m}" if view == "calendar" and month else url


def _interview_return_url(return_context):
    return _interview_workspace_url(**return_context)


def _interview_action_url(view_name, return_context, *args):
    url = reverse(view_name, args=args)
    query = f"academic_year={return_context['academic_year']}&view={return_context['view']}"
    if return_context["view"] == "calendar" and return_context["month"]:
        query += f"&month={return_context['month']:%Y-%m}"
    return f"{url}?{query}"


def _interview_form_date(request):
    value = request.POST.get("date") if request.method == "POST" else request.GET.get("date")
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _interview_workspace_context(
    *,
    academic_year,
    interview_view,
    summary,
    scope_ids=(),
    month=None,
    work_remaining=None,
    focused_findings=None,
    **calendar_data,
):
    from ..academic_years import academic_year_for_date, academic_year_label

    selected_range = _interview_selected_range(academic_year, month)
    work_remaining = work_remaining or _interview_scoped_work_remaining(summary, selected_range)
    focused_findings = focused_findings or _interview_focused_findings(
        summary.findings, selected_range
    )
    focused_attention = bool(work_remaining.items)
    findings_by_shift = {}
    for finding in focused_findings:
        findings_by_shift.setdefault(finding.shift.pk, []).append(finding)
    for shift in summary.shifts:
        shift.findings = tuple(findings_by_shift.get(shift.pk, ()))
        shift.has_unavailable_physician_finding = any(
            finding.code == "not_available_assignment" and finding.severity == "hard"
            for finding in shift.findings
        )
        shift.operational_attention = focused_attention and _interview_shift_is_focused(
            shift, selected_range
        )
        shift.scope_assignment = (
            shift.physician.display_name
            if shift.physician_id and (not scope_ids or shift.physician_id in scope_ids)
            else "Assigned outside focus"
            if shift.physician_id
            else ""
        )
    current_academic_year = _current_academic_year()
    academic_year_choices = bounded_academic_years(current_academic_year)
    context = {
        "academic_year": academic_year,
        "academic_year_label": academic_year_label(academic_year),
        "schedule_workspace_page_title": "Interviews",
        "schedule_workspace_label": "Interviews",
        "schedule_workspace_year_picker_action": reverse(
            "interview-workspace" if interview_view == "calendar" else "interview-list"
        ),
        "schedule_workspace_year_picker_preserves_month": interview_view == "calendar",
        "interview_selected_month": month,
        "interview_view": interview_view,
        "schedule_workspace_navigation": (
            {
                "label": "Calendar",
                "url": _interview_workspace_url(academic_year, "calendar", month),
                "current": interview_view == "calendar",
            },
            {
                "label": "List",
                "url": _interview_workspace_url(academic_year, "list"),
                "current": interview_view == "list",
            },
        ),
        "schedule_workspace_month_picker_action": reverse("interview-workspace"),
        "schedule_workspace_month_picker_preserved_query": {
            "academic_year": academic_year,
            "picker": "month",
        },
        "schedule_workspace_year_form": AcademicYearSelectionForm(
            initial={"academic_year": academic_year},
            years=academic_year_choices,
        ),
        "interview_summary": summary,
        "work_remaining": work_remaining,
        "interview_finding_tone": finding_tone(focused_findings),
        "interview_shifts": summary.shifts,
        "interview_list_table": _interview_list_table(
            summary.shifts, academic_year, academic_year_label(academic_year)
        ),
        "interview_add_action": _interview_action_url(
            "interview-create",
            {"academic_year": academic_year, "view": interview_view, "month": month},
        ),
        "interview_history_url": (
            f"{reverse('interview-history')}?academic_year={academic_year}&month={month:%Y-%m}"
            if month
            else reverse("interview-history")
        ),
        "interview_history_label": "Interview history",
        "schedule_workspace_surface_label": "Interview schedule",
        **academic_year_navigation_context(
            academic_year,
            academic_year_choices,
            lambda selected_year: _interview_workspace_url(
                selected_year,
                interview_view,
                academic_year_month(selected_year, month) if month else None,
            ),
        ),
        **(
            academic_year_month_navigation_context(
                academic_year_choices,
                month,
                lambda selected_month: _interview_workspace_url(
                    academic_year_for_date(selected_month), "calendar", selected_month
                ),
            )
            if month
            else {}
        ),
        **calendar_data,
    }
    if interview_view == "calendar":
        context["schedule_workspace_selected_month"] = month
        context["schedule_workspace_month_min"] = date(current_academic_year - 5, 7, 1)
        context["schedule_workspace_month_max"] = date(current_academic_year + 6, 6, 1)
    return context


def _interview_scoped_work_remaining(summary, selected_range):
    focused_shifts = tuple(
        shift for shift in summary.shifts if _interview_shift_is_focused(shift, selected_range)
    )
    focused_findings = _interview_focused_findings(summary.findings, selected_range)
    reasons = interview_work_reasons(
        summary,
        unassigned_count=sum(shift.physician_id is None for shift in focused_shifts),
        findings=focused_findings,
    )
    academic_year_range = InclusiveDateRange(*academic_year_bounds(summary.academic_year))
    items = (
        (
            ProjectedWorkItem(
                key=f"interview-{summary.academic_year}",
                area="interviews",
                label=f"Interview schedule for {summary.academic_year}",
                affected_range=academic_year_range,
                matters_on=selected_range.start,
                target_url=reverse("interview-list"),
                reasons=reasons,
            ),
        )
        if reasons
        else ()
    )
    return focused_work_remaining(items, selected_range)


def _interview_shift_is_focused(shift, selected_range):
    return focused_range_has_membership(InclusiveDateRange(shift.date, shift.date), selected_range)


def _interview_focused_findings(findings, selected_range):
    return tuple(
        finding
        for finding in findings
        if _interview_shift_is_focused(finding.shift, selected_range)
    )


def _interview_selected_range(academic_year, month):
    if month is None:
        return InclusiveDateRange(*academic_year_bounds(academic_year))
    return InclusiveDateRange(
        month, date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    )


def _interview_list_table(shifts, academic_year, academic_year_label):
    return DataTable(
        identity="interview-list",
        label="Scheduled interviews",
        width="compact",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Date", "Coverage", "Faculty", "Findings", "Actions")
        ),
        rows=tuple(_interview_list_row(shift, academic_year) for shift in shifts),
        empty_state=f"No scheduled Interviews for {academic_year_label}.",
    )


def _interview_list_row(shift, academic_year):
    if shift.has_unavailable_physician_finding:
        tone = "danger"
    elif shift.operational_attention and (not shift.physician_id or shift.findings):
        tone = "attention"
    else:
        tone = ""
    faculty = (
        shift.scope_assignment
        if shift.physician
        else "Assignment incomplete"
        if shift.operational_attention
        else "None recorded"
    )
    finding_text = "; ".join(finding.message for finding in shift.findings) or "None"
    return DataTableRow(
        (
            DataTableCell(
                shift.date.strftime("%a, %b ") + str(shift.date.day) + f", {shift.date.year}",
                kind="date",
                machine_value=shift.date.isoformat(),
                row_header=True,
            ),
            DataTableCell("AM"),
            DataTableCell(
                faculty, kind="status" if tone and not shift.physician else "text", tone=tone
            ),
            DataTableCell(finding_text, kind="status" if shift.findings else "metadata", tone=tone),
            DataTableCell(
                "Edit",
                kind="link",
                url=f"{reverse('interview-correct', args=(shift.pk,))}?academic_year={academic_year}&view=list",
            ),
        ),
        tone=tone,
    )


def _interview_calendar_data(
    month, shifts, academic_year, findings, attention_active, selected_range, scope_ids=()
):
    shift_by_date = {shift.date: shift for shift in shifts}
    findings_by_shift = {}
    for finding in findings:
        findings_by_shift.setdefault(finding.shift.pk, []).append(finding)
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    calendar_weeks = tuple(
        {
            "week_label": (
                f"{week[0].strftime('%B')} {week[0].day} through "
                f"{week[-1].strftime('%B')} {week[-1].day}, {week[-1].year}"
            ),
            "days": tuple(
                {
                    "date": day,
                    "date_label": f"{day.strftime('%b')} {day.day}",
                    "in_selected_month": day.year == month.year and day.month == month.month,
                    "shift": shift_by_date.get(day),
                    "operational_attention": (
                        attention_active
                        and shift_by_date.get(day) is not None
                        and _interview_shift_is_focused(shift_by_date[day], selected_range)
                    ),
                    "findings": tuple(findings_by_shift.get(shift_by_date.get(day).pk, ()))
                    if shift_by_date.get(day)
                    else (),
                }
                for day in week
            ),
        }
        for week in weeks
    )
    return {
        "interview_calendar_weeks": calendar_weeks,
        "calendar": _interview_calendar_lattice(
            month, calendar_weeks, academic_year, attention_active, scope_ids
        ),
    }


def _interview_calendar_lattice(month, weeks, academic_year, attention_active, scope_ids=()):
    def calendar_item(index, day):
        closed = holiday_closure_for(day["date"], "AM")
        if not day["shift"]:
            return CalendarItem(
                index,
                1,
                "schedules/calendar_interview_item.html",
                InterviewPresentation(
                    "",
                    (),
                    False,
                    "",
                    closed_label="Closed",
                    accessible_label=(
                        f"Interview AM for {day['date']:%A, %B} {day['date'].day}, "
                        f"{day['date'].year}: Closed for {closed.name}, {closed.coverage_label}."
                    ),
                ),
            )
        return CalendarItem(
            index,
            1,
            "schedules/calendar_interview_item.html",
            InterviewPresentation(
                day["shift"].physician.display_name
                if day["shift"].physician_id
                and (not scope_ids or day["shift"].physician_id in scope_ids)
                else (
                    "Assigned outside focus"
                    if day["shift"].physician_id
                    else ("Assignment incomplete" if day["operational_attention"] else "")
                ),
                tuple(
                    InterviewFindingPresentation(finding.message, finding.severity)
                    for finding in day["findings"]
                ),
                day["operational_attention"]
                and (not day["shift"].physician_id or bool(day["findings"])),
                f"{reverse('interview-correct', args=(day['shift'].pk,))}"
                f"?academic_year={academic_year}&view=calendar&month={month:%Y-%m}",
                "Assigned outside focus"
                if day["shift"].physician_id
                and scope_ids
                and day["shift"].physician_id not in scope_ids
                else "",
                finding_tone(day["findings"]),
            ),
        )

    return CalendarLattice(
        label=f"{month:%B %Y} Interviews calendar",
        columns=("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
        variant="seven-column",
        has_gutter=False,
        surface_classes="calendar-surface--interview",
        weeks=tuple(
            CalendarWeek(
                label=week["week_label"],
                days=tuple(
                    CalendarDay(
                        day["date"].isoformat(),
                        day["date_label"],
                        day["in_selected_month"],
                        CalendarAction(
                            _interview_action_url(
                                "interview-create",
                                {
                                    "academic_year": academic_year_for_date(day["date"]),
                                    "view": "calendar",
                                    "month": day["date"].replace(day=1),
                                },
                            )
                            + f"&date={day['date']:%Y-%m-%d}",
                            f"Schedule an Interview for {day['date']:%B} {day['date'].day}, "
                            f"{day['date'].year}",
                            "",
                            f"interview-add-{day['date']:%Y-%m-%d}",
                            "interview-date-add-link",
                        )
                        if not holiday_closure_for(day["date"], "AM")
                        else None,
                    )
                    for day in week["days"]
                ),
                lanes=(
                    CalendarLane(
                        "Interview AM",
                        tuple(
                            calendar_item(index, day)
                            for index, day in enumerate(week["days"], start=1)
                            if day["shift"] or holiday_closure_for(day["date"], "AM")
                        ),
                        period="AM",
                    ),
                ),
            )
            for week in weeks
        ),
    )
