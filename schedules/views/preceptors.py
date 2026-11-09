"""Preceptor scheduling HTTP workflows and workspace presentation."""

import calendar
from dataclasses import replace
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ..calendar_presentation import (
    AssignmentPresentation,
    AssignmentSlotPresentation,
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarWeek,
    FindingLinkPresentation,
    FindingPresentation,
    finding_tone,
)
from ..forms.preceptors import AssignmentForm, AssignmentReasonForm, FindingAcknowledgementForm
from ..models import FindingAcknowledgement
from ..models.late_docs import LateDocAssignment
from ..models.preceptors import PreceptorAssignment, PreceptorSession, SchedulePeriod
from ..pdf import build_precepting_calendar_pdf, precepting_calendar_filename
from ..physician_scope import physician_scope_ids
from ..precepting_content import assigned_identity, finding_content, finding_source_content
from ..precepting_presentation import (
    marker_for_position,
    precepting_calendar_legend,
    precepting_finding_targets,
)
from ..services._mutation import scheduling_operation
from ..services.finding_acknowledgements import (
    acknowledge_finding,
    finding_identity,
    readiness_for_findings,
    remove_finding_acknowledgement,
)
from ..services.holidays import holiday_closure_for
from ..services.operational_attention import (
    ORGANIZATION_TIME_ZONE,
    is_operational_date,
    is_operational_preceptor_week,
    organization_today,
)
from ..services.preceptors import (
    PRECEPTING_FINDING_WORK_SUMMARY_TYPE_KEYS,
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_finding_label,
    draft_findings_for_week,
    focused_draft_findings_for_range,
    initialize_preceptor_periods,
    operational_draft_findings_for_session,
    precepting_work_reasons,
    preceptor_date_has_focused_membership,
    preceptor_week_has_focused_membership,
    projected_draft_findings_for_weeks,
    remove_preceptor_assignment,
    session_assignments,
    set_preceptor_assignment_lock,
    work_week_start,
)
from ..services.work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    WorkRemainingRangeAction,
    focused_work_remaining,
)
from ._shared import _requested_month, _shift_month


@login_required
def monthly_workspace(request):
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    scope_ids = physician_scope_ids(request)
    month_week_starts = _month_work_week_starts(month)
    first_week = month_week_starts[0]
    last_week = month_week_starts[-1]
    selected_range = InclusiveDateRange(month, _shift_month(month, 1) - timedelta(days=1))
    # Edge weeks are made from the same persisted sessions as their adjacent months.
    periods = initialize_preceptor_periods(month, first_week, last_week + timedelta(days=4))
    selected_period = next(period for period in periods if period.month == month)
    sessions = {
        (session.date, session.period_of_day): session
        for session in PreceptorSession.objects.filter(
            date__range=(first_week, last_week + timedelta(days=4))
        )
    }
    month_sessions = tuple(
        session for session in sessions.values() if session.date.month == month.month
    )
    weekly_rows = _preceptor_week_rows(month_week_starts, month, selected_range=selected_range)
    week_findings = {row["week_start"]: row["focused_findings"] for row in weekly_rows}
    findings_by_session = {}
    for focused_findings in week_findings.values():
        for finding in focused_findings:
            if finding.session is not None:
                findings_by_session.setdefault(finding.session.pk, []).append(finding)
    for session in sessions.values():
        session_findings = findings_by_session.get(session.pk, ())
        session.focused_membership = _preceptor_date_has_focused_membership(
            session.date, selected_range
        )
        session.findings = tuple(session_findings) if session.focused_membership else ()
        session.faculty_coverage_conflict = (
            session.focused_membership and _has_month_faculty_coverage_conflict(session.findings)
        )
        session.assignment_slots = _prepared_assignment_slots(
            session,
            session_findings,
            operational_attention=session.focused_membership,
            scope_ids=scope_ids,
            overview=True,
            compact=True,
        )
    late_doc_slots = _late_doc_slots_for_sessions(
        sessions.values(),
        scope_ids,
        editable=True,
        return_month=month,
        overview=True,
        compact=True,
        focused_range=selected_range,
    )
    assignment_count = 0
    incomplete_session_count = 0
    for session in month_sessions:
        assignment_count += sum(slot["status"] is not None for slot in session.assignment_slots)
        if not holiday_closure_for(session.date, session.period_of_day) and any(
            slot["status"] is None
            for slot in session.assignment_slots
            if slot["position"].startswith("primary-")
        ):
            incomplete_session_count += 1

    weekly_work_items = _preceptor_week_work_items(weekly_rows)
    work_remaining = _preceptor_work_remaining(
        weekly_work_items,
        selected_range,
        schedule_bot_month=month,
    )
    weeks = _month_overview_weeks(month, sessions, late_doc_slots)
    return render(
        request,
        "schedules/monthly_workspace.html",
        {
            **_preceptor_month_workspace_context(month, request.user),
            "selected_month": month,
            "selected_period": selected_period,
            "organization_timezone": ORGANIZATION_TIME_ZONE,
            "previous_month": _shift_month(month, -1),
            "next_month": _shift_month(month, 1),
            "weeks": weeks,
            "calendar": _month_calendar_lattice(month, weeks),
            "precepting_calendar_pdf_url": _precepting_calendar_pdf_url(month, scope_ids),
            "session_count": len(month_sessions),
            "assignment_count": assignment_count,
            "incomplete_session_count": incomplete_session_count,
            "findings": tuple(
                finding
                for focused_findings in week_findings.values()
                for finding in focused_findings
            ),
            "week_findings": week_findings,
            "work_remaining": work_remaining,
        },
    )


@login_required
def precepting_calendar_pdf(request):
    """Export the current, scoped month calendar without changing schedule state."""
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    scope_ids = _requested_pdf_scope_ids(request.GET.getlist("physicians"))
    response = HttpResponse(
        build_precepting_calendar_pdf(
            month,
            _precepting_calendar_pdf_weeks(month, scope_ids),
            captured_at=timezone.localtime(timezone.now()),
            last_edited_at=SchedulePeriod.objects.filter(month=month)
            .values_list("last_edited_at", flat=True)
            .first(),
        ),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{precepting_calendar_filename(month)}"'
    )
    return response


@login_required
def monthly_list(request):
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    scope_ids = physician_scope_ids(request)
    month_week_starts = _month_work_week_starts(month)
    initialize_preceptor_periods(
        month,
        month_week_starts[0],
        month_week_starts[-1] + timedelta(days=4),
    )
    next_month = _shift_month(month, 1)
    selected_range = InclusiveDateRange(month, next_month - timedelta(days=1))
    sessions = tuple(
        PreceptorSession.objects.filter(date__gte=month, date__lt=next_month)
        .order_by("date", "period_of_day", "pk")
        .prefetch_related("assignments__physician")
    )
    week_starts = tuple(dict.fromkeys(work_week_start(session.date) for session in sessions))
    weekly_rows = _preceptor_week_rows(week_starts, month, selected_range=selected_range)
    findings_by_session = {}
    week_findings = {}
    for row in weekly_rows:
        for finding in row["focused_findings"]:
            if finding.session is None:
                week_findings.setdefault(finding.week or row["week_start"], []).append(finding)
            else:
                findings_by_session.setdefault(finding.session.pk, []).append(finding)
    for session in sessions:
        session.focused_membership = _preceptor_date_has_focused_membership(
            session.date, selected_range
        )
        session.findings = (
            tuple(findings_by_session.get(session.pk, ())) if session.focused_membership else ()
        )
        session.week_findings = (
            tuple(week_findings.get(work_week_start(session.date), ()))
            if session.focused_membership
            else ()
        )
        session.finding_tone = finding_tone((*session.findings, *session.week_findings))
        session.assignment_slots = _prepared_assignment_slots(
            session,
            session.findings,
            operational_attention=session.focused_membership,
            scope_ids=scope_ids,
        )
        session.week_start = work_week_start(session.date)
        closed = holiday_closure_for(session.date, session.period_of_day)
        position = next(
            (slot["position"] for slot in session.assignment_slots if not closed or slot["status"]),
            None,
        )
        session.detail_url = (
            reverse("session-assignment-edit", args=(session.pk, position))
            + f"?month={month:%Y-%m}"
            if position
            else f"{_preceptor_month_url(month)}#precepting-week-{session.week_start:%Y-%m-%d}"
        )
    late_doc_slots = _late_doc_slots_for_sessions(
        sessions, scope_ids, editable=False, focused_range=selected_range
    )
    for session in sessions:
        session.late_doc_slot = late_doc_slots.get(session.pk)
    weekly_work_items = _preceptor_week_work_items(weekly_rows)
    return render(
        request,
        "schedules/preceptor_schedule_list.html",
        {
            **_preceptor_list_workspace_context(month, request.user),
            "selected_month": month,
            "sessions": sessions,
            "work_remaining": _preceptor_work_remaining(
                weekly_work_items,
                selected_range,
                schedule_bot_month=month,
            ),
        },
    )


@login_required
def session_assignment_edit(request, session_id, position):
    session = get_object_or_404(PreceptorSession, pk=session_id)
    responsibility, primary_position = _assignment_position(position)
    findings_context = _assignment_editor_findings(session, position)
    assignment = PreceptorAssignment.objects.filter(
        session=session,
        responsibility=responsibility,
        primary_position=primary_position,
    ).first()
    alternatives = assignment_alternatives_for_session(session, responsibility, primary_position)
    cleanup_closure = (
        holiday_closure_for(session.date, session.period_of_day)
        if is_operational_date(session.date)
        else None
    )
    if cleanup_closure:
        if assignment is None:
            raise Http404("This clinic holiday closure has no retained assignment to remove.")
        if request.method == "POST":
            return HttpResponseBadRequest(
                f"{cleanup_closure.name} closes this {cleanup_closure.coverage_label} clinic shift."
            )
        return render(
            request,
            "schedules/assignment_form.html",
            {
                **_preceptor_drilldown_workspace_context(session, request),
                "session": session,
                "assignment": assignment,
                "alternatives": alternatives,
                "cleanup_closure": cleanup_closure,
                "position_label": _assignment_position_label(position),
                "position": position,
                "remove_form": AssignmentReasonForm(),
                "return_url": _workspace_return_url(request, session),
                "return_query": _editor_return_query(request),
                **findings_context,
            },
        )
    form = AssignmentForm(
        request.POST or None,
        session=session,
        assignment=assignment,
        alternatives=alternatives,
        initial={
            "physician": assignment.physician_id if assignment else None,
            "is_locked": assignment.is_locked if assignment else False,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            with scheduling_operation():
                if not assignment or assignment.physician_id != form.cleaned_data["physician"].pk:
                    assignment = assign_preceptor(
                        session=session,
                        physician=form.cleaned_data["physician"],
                        responsibility=responsibility,
                        primary_position=primary_position,
                        scheduler=request.user,
                        reason=form.cleaned_data["reason"],
                    )
                set_preceptor_assignment_lock(
                    assignment=assignment,
                    is_locked=form.cleaned_data["is_locked"],
                    scheduler=request.user,
                    reason=form.cleaned_data["reason"],
                )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_workspace_return_url(request, session))
    return render(
        request,
        "schedules/assignment_form.html",
        {
            **_preceptor_drilldown_workspace_context(session, request),
            "form": form,
            "session": session,
            "assignment": assignment,
            "alternatives": alternatives,
            "position_label": _assignment_position_label(position),
            "position": position,
            "remove_form": AssignmentReasonForm(),
            "return_url": _workspace_return_url(request, session),
            "return_query": _editor_return_query(request),
            "form_action": _assignment_editor_url(request, session, position),
            **findings_context,
        },
    )


@login_required
def session_assignment_remove(request, session_id, position):
    session = get_object_or_404(PreceptorSession, pk=session_id)
    responsibility, primary_position = _assignment_position(position)
    assignment = get_object_or_404(
        PreceptorAssignment,
        session=session,
        responsibility=responsibility,
        primary_position=primary_position,
    )
    alternatives = assignment_alternatives_for_session(session, responsibility, primary_position)
    if request.method != "POST":
        return redirect(_assignment_editor_url(request, session, position))
    form = AssignmentReasonForm(request.POST)
    if form.is_valid():
        remove_preceptor_assignment(
            assignment=assignment, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect(_workspace_return_url(request, session))
    return render(
        request,
        "schedules/assignment_form.html",
        {
            **_preceptor_drilldown_workspace_context(session, request),
            "form": AssignmentForm(
                session=session,
                assignment=assignment,
                alternatives=alternatives,
            ),
            "session": session,
            "assignment": assignment,
            "alternatives": alternatives,
            "position_label": _assignment_position_label(position),
            "position": position,
            "remove_form": form,
            "return_url": _workspace_return_url(request, session),
            "return_query": _editor_return_query(request),
            "form_action": _assignment_editor_url(request, session, position),
            **_assignment_editor_findings(session, position),
        },
    )


@login_required
def session_assignment_history(request, session_id):
    session = get_object_or_404(PreceptorSession, pk=session_id)
    return render(
        request,
        "schedules/assignment_history.html",
        {
            **_preceptor_drilldown_workspace_context(session, request),
            "session": session,
            "changes": session.assignment_changes.select_related("changed_by"),
            "return_url": _workspace_return_url(request, session),
        },
    )


@login_required
def session_finding_acknowledge(request, session_id):
    """Acknowledge one still-current hard finding from its relevant session editor."""
    session = get_object_or_404(PreceptorSession, pk=session_id)
    finding_position = request.POST.get("position")
    finding_scope = request.POST.get("scope")
    finding = next(
        (
            candidate
            for candidate in _assignment_editor_exception_findings(session)
            if candidate.severity == "hard"
            and candidate.code == request.POST.get("code")
            and (
                (finding_scope == "week" and candidate.session is None)
                or (
                    finding_scope != "week"
                    and candidate.session is not None
                    and (candidate.explanation.get("position") or "") == (finding_position or "")
                )
            )
        ),
        None,
    )
    if request.method != "POST" or finding is None:
        messages.error(request, "That finding no longer applies and cannot be acknowledged.")
    else:
        form = FindingAcknowledgementForm(request.POST)
        if form.is_valid():
            try:
                acknowledge_finding(
                    finding=finding,
                    scheduler=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as error:
                messages.error(request, error.messages[0])
            else:
                messages.success(request, "Exception acknowledged. The rule remains unmet.")
        else:
            messages.error(request, form.errors["reason"][0])
    return redirect(
        _assignment_editor_url(request, session, request.POST.get("editor_position") or "primary-1")
    )


@login_required
def session_finding_override_remove(request, session_id):
    """Remove one acknowledgement only while its exact hard finding is still live."""
    session = get_object_or_404(PreceptorSession, pk=session_id)
    finding_position = request.POST.get("position")
    finding_scope = request.POST.get("scope")
    finding = next(
        (
            candidate
            for candidate in _assignment_editor_exception_findings(session)
            if candidate.severity == "hard"
            and candidate.code == request.POST.get("code")
            and (
                (finding_scope == "week" and candidate.session is None)
                or (
                    finding_scope != "week"
                    and candidate.session is not None
                    and (candidate.explanation.get("position") or "") == (finding_position or "")
                )
            )
        ),
        None,
    )
    if request.method != "POST" or finding is None:
        messages.error(request, "That override is no longer current and cannot be removed.")
    else:
        try:
            remove_finding_acknowledgement(
                finding=finding,
                acknowledgement_id=request.POST.get("acknowledgement_id"),
                scheduler=request.user,
            )
        except ValidationError as error:
            messages.error(request, error.messages[0])
        else:
            messages.success(
                request,
                "Override removed. This hard finding is required work again.",
            )
    return redirect(
        _assignment_editor_url(request, session, request.POST.get("editor_position") or "primary-1")
    )


@login_required
def scheduling_exceptions(request):
    """Show retained exceptions without exposing internal finding identities."""
    acknowledgements = FindingAcknowledgement.objects.select_related(
        "acknowledged_by", "removed_by"
    )
    exceptions = sorted(
        (_exception_history_presentation(item) for item in acknowledgements),
        key=lambda item: (item["occurrence_date"], item["acknowledged_at"]),
        reverse=True,
    )
    return render(
        request,
        "schedules/scheduling_exceptions.html",
        {
            **_preceptor_exceptions_workspace_context(request.user),
            "exceptions": exceptions,
        },
    )


def _preceptor_month_workspace_context(month, user):
    return {
        "schedule_workspace_page_title": f"{month:%B %Y}",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current="calendar", month=month, user=user
        ),
        "schedule_workspace_selected_month": month,
        "schedule_workspace_previous_month_url": _preceptor_month_url(_shift_month(month, -1)),
        "schedule_workspace_next_month_url": _preceptor_month_url(_shift_month(month, 1)),
        "schedule_workspace_month_picker_action": reverse("monthly-workspace"),
        "schedule_workspace_month_picker_preserved_query": {},
    }


def _preceptor_drilldown_workspace_context(session, request):
    month = _return_month(request) or session.date.replace(day=1)
    return {
        "schedule_workspace_page_title": "Preceptor Schedule",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current="calendar",
            month=month,
            user=request.user,
        ),
    }


def _preceptor_list_workspace_context(month, user):
    return {
        "schedule_workspace_page_title": f"{month:%B %Y}",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current="list", month=month, user=user
        ),
        "schedule_workspace_selected_month": month,
        "schedule_workspace_previous_month_url": _preceptor_list_url(_shift_month(month, -1)),
        "schedule_workspace_next_month_url": _preceptor_list_url(_shift_month(month, 1)),
        "schedule_workspace_month_picker_action": reverse("preceptor-list"),
        "schedule_workspace_month_picker_preserved_query": {},
    }


def _preceptor_exceptions_workspace_context(user):
    return {
        "schedule_workspace_page_title": "Exceptions",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current="exceptions", month=organization_today().replace(day=1), user=user
        ),
    }


def preceptor_workspace_navigation(*, current, month=None, user=None):
    navigation = [
        {
            "label": "Calendar",
            "url": _preceptor_month_url(month),
            "current": current == "calendar",
        },
        {
            "label": "List",
            "url": _preceptor_list_url(month),
            "current": current == "list",
        },
        {
            "label": "Exceptions",
            "url": reverse("scheduling-exceptions"),
            "current": current == "exceptions",
        },
        {
            "label": "Schedule Bot",
            "url": reverse("precepting-solver-workspace"),
            "current": current == "solve",
        },
    ]
    if user and user.has_perm("schedules.manage_scheduling_administration"):
        navigation.append(
            {
                "label": "Import Schedule",
                "url": reverse("precepting-workbook-import"),
                "current": current == "import",
            }
        )
    return tuple(navigation)


def _preceptor_month_url(month):
    return f"{reverse('monthly-workspace')}?month={month:%Y-%m}"


def _precepting_calendar_pdf_url(month, scope_ids):
    query = [
        ("month", f"{month:%Y-%m}"),
        *(("physicians", identifier) for identifier in sorted(scope_ids)),
    ]
    return f"{reverse('precepting-calendar-pdf')}?{urlencode(query)}"


def _requested_pdf_scope_ids(values):
    scope_ids = set()
    for value in values:
        try:
            identifier = int(value)
        except ValueError:
            continue
        if str(identifier) == value and identifier > 0:
            scope_ids.add(identifier)
    return scope_ids


def _preceptor_list_url(month):
    return f"{reverse('preceptor-list')}?month={month:%Y-%m}"


def _month_work_week_starts(month):
    return tuple(
        week_dates[0]
        for week_dates in calendar.Calendar().monthdatescalendar(month.year, month.month)
        if any(day.month == month.month for day in week_dates[:5])
    )


def _precepting_calendar_pdf_weeks(month, scope_ids):
    """Project the calendar export from retained schedule state without initialization."""
    week_starts = _month_work_week_starts(month)
    first_week, last_week = week_starts[0], week_starts[-1]
    sessions = {
        (session.date, session.period_of_day): session
        for session in PreceptorSession.objects.filter(
            date__range=(first_week, last_week + timedelta(days=4))
        )
    }
    late_docs = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(date__in=week_starts).select_related(
            "physician"
        )
    }
    selected_range = InclusiveDateRange(month, _shift_month(month, 1) - timedelta(days=1))
    weeks = []
    for week_start in week_starts:
        days = []
        for offset in range(5):
            on_date = week_start + timedelta(days=offset)
            sessions_by_period = {}
            has_holiday_closure = False
            for period in PreceptorSession.PeriodOfDay.values:
                session = sessions.get((on_date, period))
                closure = holiday_closure_for(on_date, period)
                has_holiday_closure = has_holiday_closure or bool(closure)
                sessions_by_period[period] = _precepting_calendar_pdf_assignments(
                    session,
                    closure,
                    scope_ids,
                    operational_attention=_preceptor_date_has_focused_membership(
                        on_date, selected_range
                    ),
                )
            late_doc = late_docs.get(on_date) if on_date.weekday() == 0 else None
            late_doc_closure = holiday_closure_for(on_date, PreceptorSession.PeriodOfDay.PM)
            days.append(
                {
                    "date": on_date,
                    "has_holiday_closure": has_holiday_closure,
                    "am": sessions_by_period[PreceptorSession.PeriodOfDay.AM],
                    "pm": sessions_by_period[PreceptorSession.PeriodOfDay.PM],
                    "late_doc": _precepting_calendar_pdf_late_doc(
                        late_doc,
                        late_doc_closure,
                        scope_ids,
                        operational_attention=_preceptor_date_has_focused_membership(
                            on_date, selected_range
                        ),
                    )
                    if on_date.weekday() == 0
                    else (),
                }
            )
        weeks.append(
            {
                "week_start": week_start,
                "week_end": week_start + timedelta(days=4),
                "days": tuple(days),
            }
        )
    return tuple(weeks)


def _precepting_calendar_pdf_assignments(session, closure, scope_ids, *, operational_attention):
    if session is None:
        value = "Closed" if closure else "Unfilled" if operational_attention else "-"
        return (("Preceptor 1", value), ("Preceptor 2", value), ("Secondary", value))
    slots = _prepared_assignment_slots(
        session,
        (),
        operational_attention=operational_attention,
        scope_ids=scope_ids,
        overview=True,
        compact=True,
    )
    return tuple((slot["label"], slot["display"]) for slot in slots)


def _precepting_calendar_pdf_late_doc(assignment, closure, scope_ids, *, operational_attention):
    if assignment is None:
        return (
            ("Late Doc", "Closed" if closure else "Unfilled" if operational_attention else "-"),
        )
    display = (
        "Occupied"
        if scope_ids and assignment.physician_id not in scope_ids
        else assignment.physician.display_name
    )
    return (("Late Doc", display),)


def _preceptor_date_has_focused_membership(on_date, selected_range):
    return preceptor_date_has_focused_membership(on_date, selected_range)


def _preceptor_week_has_focused_membership(week_start, selected_range):
    return preceptor_week_has_focused_membership(week_start, selected_range)


def _preceptor_week_rows(week_starts, month, *, selected_range=None):
    """Project every displayed work week for the weekly list tables."""
    late_docs = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(date__in=week_starts).select_related(
            "physician"
        )
    }
    rows = []
    for week_start in week_starts:
        findings = projected_draft_findings_for_weeks((week_start,), complete_calendar=True)
        focused_membership = selected_range is None or _preceptor_week_has_focused_membership(
            week_start, selected_range
        )
        focused_findings = (
            findings.findings
            if selected_range is None
            else focused_draft_findings_for_range(findings, selected_range).findings
        )
        finding_readiness = readiness_for_findings(focused_findings)
        unacknowledged_hard_findings = finding_readiness.unacknowledged_hard_findings
        assignment_gaps = sum(
            finding.code == "missing_primary_position" for finding in unacknowledged_hard_findings
        )
        missing_late_doc = any(
            finding.code == "missing_late_doc" for finding in unacknowledged_hard_findings
        )
        hard_finding_counts = {
            code: sum(finding.code == code for finding in unacknowledged_hard_findings)
            for code in {finding.code for finding in unacknowledged_hard_findings}
        }
        hard_findings = len(unacknowledged_hard_findings)
        preference_findings = sum(finding.severity == "preference" for finding in focused_findings)
        actionable_date_set: set[date] = set()
        for finding in unacknowledged_hard_findings:
            on_date = finding.session.date if finding.session is not None else finding.week
            if on_date is not None:
                actionable_date_set.add(on_date)
        actionable_dates = tuple(sorted(actionable_date_set))
        if hard_findings:
            readiness_state, readiness_label = "blocked", "Blocked by hard findings"
        elif preference_findings:
            readiness_state, readiness_label = "attention", "Needs review for non-blocking findings"
        else:
            readiness_state, readiness_label = "clear", "No findings"
        rows.append(
            {
                "week_start": week_start,
                "week_end": week_start + timedelta(days=4),
                "assignment_gaps": assignment_gaps if focused_membership else 0,
                "late_doc": late_docs.get(week_start),
                "missing_late_doc": missing_late_doc if focused_membership else False,
                "hard_findings": hard_findings,
                "acknowledged_hard_findings": finding_readiness.acknowledged_hard_findings,
                "hard_finding_counts": hard_finding_counts,
                "actionable_dates": actionable_dates if focused_membership else (),
                "preference_findings": preference_findings,
                "readiness_state": readiness_state if focused_membership else "clear",
                "readiness_label": readiness_label if focused_membership else "Retained record",
                "calendar_url": f"{_preceptor_month_url(month)}#precepting-week-{week_start:%Y-%m-%d}",
                "focused_findings": focused_findings,
                "finding_readiness": finding_readiness,
            }
        )
    return tuple(rows)


def _actionable_preceptor_week_rows(rows):
    return tuple(
        row
        for row in rows
        if row["assignment_gaps"]
        or row["missing_late_doc"]
        or row["hard_findings"]
        or row["preference_findings"]
    )


def _preceptor_week_work_item(row):
    """Adapt the existing monthly week projection to the shared work vocabulary."""
    return ProjectedWorkItem(
        key=f"precepting-week-{row['week_start']:%Y-%m-%d}",
        area="precepting",
        label=f"Week of {row['week_start']:%b} {row['week_start'].day}",
        affected_range=InclusiveDateRange(row["week_start"], row["week_end"]),
        matters_on=row["week_start"],
        target_url=row["calendar_url"],
        reasons=precepting_work_reasons(
            hard_finding_counts=row["hard_finding_counts"],
            advisory_finding_count=row["preference_findings"],
        ),
        actionable_dates=row["actionable_dates"],
    )


def _preceptor_week_work_items(rows):
    """Project the actionable displayed weeks once for every month consumer."""
    actionable_rows = _actionable_preceptor_week_rows(rows)
    return tuple(_preceptor_week_work_item(row) for row in actionable_rows)


def _preceptor_work_remaining(items, selected_range, *, schedule_bot_month=None):
    summary = focused_work_remaining(
        items,
        selected_range,
    )
    if schedule_bot_month and any(
        item.key in PRECEPTING_FINDING_WORK_SUMMARY_TYPE_KEYS
        for item in summary.required_summary_items
    ):
        return replace(summary, range_action=_preceptor_schedule_bot_action(schedule_bot_month))
    return summary


def _preceptor_schedule_bot_action(month):
    return WorkRemainingRangeAction(
        label="Open in Schedule Bot",
        url=f"{reverse('precepting-solver-workspace')}?range_kind=month&month={month:%Y-%m}",
    )


def _month_overview_weeks(month, sessions, late_doc_slots):
    weeks = []
    for week_dates in calendar.Calendar().monthdatescalendar(month.year, month.month):
        week_start = week_dates[0]
        if not any(day.month == month.month for day in week_dates[:5]):
            continue
        weeks.append(
            {
                "week_start": week_start,
                "week_end": week_start + timedelta(days=4),
                "days": tuple(
                    {
                        "date": day,
                        "in_selected_month": day.month == month.month,
                        "sessions": tuple(
                            sessions[(day, period_of_day)]
                            for period_of_day in PreceptorSession.PeriodOfDay.values
                        ),
                        "late_doc_slot": late_doc_slots.get(
                            sessions[(day, PreceptorSession.PeriodOfDay.PM)].pk
                        ),
                    }
                    for day in week_dates[:5]
                ),
            }
        )
    return weeks


def _month_calendar_lattice(month, weeks):
    return CalendarLattice(
        label=f"{month:%B %Y} weekday schedule",
        columns=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
        variant="five-column",
        has_gutter=True,
        surface_classes="calendar-surface--assignment-month",
        weeks=tuple(
            CalendarWeek(
                label=(
                    f"{week['week_start']:%B} {week['week_start'].day} through "
                    f"{week['week_end']:%B} {week['week_end'].day}, {week['week_end'].year}"
                ),
                days=tuple(
                    CalendarDay(
                        day["date"].isoformat(),
                        f"{day['date']:%b} {day['date'].day}",
                        day["in_selected_month"],
                    )
                    for day in week["days"]
                ),
                lanes=(
                    *(
                        CalendarLane(
                            period,
                            tuple(
                                CalendarItem(
                                    index,
                                    1,
                                    "schedules/calendar_assignment_item.html",
                                    _assignment_presentation(
                                        day["sessions"][session_index],
                                        editable=True,
                                        return_month=month,
                                        compact=True,
                                    ),
                                )
                                for index, day in enumerate(week["days"], start=1)
                            ),
                            period=period,
                        )
                        for period, session_index in (("AM", 0), ("PM", 1))
                    ),
                    CalendarLane(
                        "Late Doc",
                        tuple(
                            CalendarItem(
                                index,
                                1,
                                "schedules/calendar_late_doc_item.html",
                                day["late_doc_slot"],
                            )
                            for index, day in enumerate(week["days"], start=1)
                        ),
                        variant="compact",
                        period="PM",
                    ),
                ),
                target=f"precepting-week-{week['week_start']:%Y-%m-%d}",
            )
            for week in weeks
        ),
        precepting_legend=precepting_calendar_legend(),
    )


def week_calendar_lattice(
    week_start,
    days,
    *,
    assignment_content,
    late_doc_content,
    status=None,
    legend=(),
    precepting_legend=(),
    header_variant="two-level",
    header_finding=None,
    header_required_work_count=0,
    columns=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
):
    """Render any editable Precepting week through the common lattice and item partials."""
    week_end = week_start + timedelta(days=4)
    calendar_days = tuple(
        CalendarDay(
            day["date"].isoformat(),
            (
                f"{day['date']:%a, %b} {day['date'].day}"
                if header_variant == "single-row"
                else f"{day['date']:%a %b} {day['date'].day}"
            ),
            day.get("actionable", True),
        )
        for day in days
    )
    return CalendarLattice(
        label=(
            f"{week_start:%B} {week_start.day}, {week_start.year} to "
            f"{week_end:%B} {week_end.day}, {week_end.year} weekday schedule"
        ),
        columns=columns,
        variant="five-column",
        has_gutter=True,
        surface_classes="calendar-surface--assignment-week",
        weeks=(
            CalendarWeek(
                label=(
                    f"{week_start:%B} {week_start.day} through {week_end:%B} "
                    f"{week_end.day}, {week_end.year}"
                ),
                days=calendar_days,
                lanes=(
                    *(
                        CalendarLane(
                            period,
                            tuple(
                                CalendarItem(
                                    index,
                                    1,
                                    "schedules/calendar_assignment_item.html",
                                    assignment_content(day, session_index),
                                )
                                for index, day in enumerate(days, start=1)
                            ),
                            period=period,
                        )
                        for period, session_index in (("AM", 0), ("PM", 1))
                    ),
                    CalendarLane(
                        "Late Doc",
                        tuple(
                            CalendarItem(
                                index,
                                1,
                                "schedules/calendar_late_doc_item.html",
                                late_doc_content(day),
                            )
                            for index, day in enumerate(days, start=1)
                        ),
                        variant="compact",
                        period="PM",
                    ),
                ),
                status=status,
            ),
        ),
        legend=legend,
        precepting_legend=precepting_legend,
        header_variant=header_variant,
        header_days=calendar_days if header_variant == "single-row" else (),
        header_status=status if header_variant == "single-row" else None,
        header_required_work_count=(
            header_required_work_count if header_variant == "single-row" else 0
        ),
        header_finding=header_finding if header_variant == "single-row" else None,
    )


def _assignment_presentation(session, *, editable=False, return_month=None, compact=False):
    closed = holiday_closure_for(session.date, session.period_of_day)
    if closed and not any(slot["status"] for slot in session.assignment_slots):
        return AssignmentPresentation(
            period=session.period_of_day,
            date_label=f"{session.date:%A, %b} {session.date.day}",
            slots=(
                AssignmentSlotPresentation(
                    "Closed",
                    "holiday-closed",
                    f"{session.period_of_day} assignments for {session.date:%A, %B} "
                    f"{session.date.day}, {session.date.year}: Closed for {closed.name}, "
                    f"{closed.coverage_label}.",
                ),
            ),
            faculty_coverage_conflict=False,
            compact_findings=compact,
        )
    slots = []
    for slot in session.assignment_slots:
        status = slot["status"]
        url = action_label = lock_label = ""
        cleanup = closed and status is not None
        if (editable and not closed) or cleanup:
            url = reverse("session-assignment-edit", args=(session.pk, slot["position"]))
            if return_month:
                url += f"?month={return_month:%Y-%m}"
            action = (
                "Remove retained"
                if cleanup
                else "Edit"
                if status
                else ("Assign" if slot["state"] == "gap" else "Add")
            )
            action_label = (
                f"{action} {slot['accessible_label']} for {session.period_of_day} "
                f"on {session.date:%B} {session.date.day}, {session.date.year}"
            )
            if status and not cleanup:
                lock_label = "Locked" if status.is_locked else ""
        if slot["finding"]:
            action_label = f"{action_label}. {slot['finding'].summary}"
        slots.append(
            AssignmentSlotPresentation(
                slot["display"],
                slot["state"],
                slot["accessible_label"],
                url,
                action_label,
                lock_label,
                finding=slot["finding"],
                finding_tone=slot["finding_tone"],
            )
        )
    finding_targets = precepting_finding_targets(session, session.findings)
    return AssignmentPresentation(
        period=session.period_of_day,
        date_label=f"{session.date:%A, %b} {session.date.day}",
        slots=tuple(slots),
        faculty_coverage_conflict=getattr(session, "faculty_coverage_conflict", False),
        findings=tuple(
            _finding_presentation(finding, session) for finding in getattr(session, "findings", ())
        ),
        compact_findings=compact,
        session_finding=(marker_for_position(finding_targets, None) if compact else None),
        session_finding_url=slots[0].url if compact and slots else "",
    )


def _assignment_editor_findings(session, position):
    """Assemble the editor's complete, acknowledgement-aware finding presentation once."""
    candidates = []
    for finding in operational_draft_findings_for_session(session).findings:
        finding_position = finding.explanation.get("position")
        is_current_diagnostic = (
            finding_position == position
            or finding_position is None
            or finding.code == "missing_primary_position"
        )
        if finding.severity == "hard" or is_current_diagnostic:
            candidates.append(finding)

    week_start = work_week_start(session.date)
    if is_operational_preceptor_week(week_start):
        candidates.extend(
            finding
            for finding in draft_findings_for_week(week_start).findings
            if finding.session is None
        )

    findings = []
    identities = set()
    for finding in candidates:
        identity_key = finding_identity(finding).key
        if identity_key not in identities:
            identities.add(identity_key)
            findings.append(finding)

    readiness = readiness_for_findings(findings)
    readiness_by_finding = {id(entry.finding): entry for entry in readiness.entries}
    entries = tuple(
        {
            "finding": _finding_presentation(finding, session),
            "code": finding.code,
            "position": finding.explanation.get("position") or "",
            "scope": "week" if finding.session is None else "session",
            "rounding_url": (
                f"{reverse('rounding-workspace')}?month={finding.week:%Y-%m}"
                if finding.session is None
                else ""
            ),
            "acknowledgement": readiness_by_finding[id(finding)].acknowledgement,
        }
        for finding in findings
    )
    return {"finding_entries": entries}


def _assignment_editor_exception_findings(session):
    """Return current hard findings the session editor can directly contextualize."""
    findings = [
        finding
        for finding in operational_draft_findings_for_session(session).findings
        if finding.severity == "hard"
    ]
    week_start = work_week_start(session.date)
    if is_operational_preceptor_week(week_start):
        findings.extend(
            finding
            for finding in draft_findings_for_week(week_start).findings
            if finding.severity == "hard" and finding.session is None
        )
    return tuple(findings)


def _exception_history_presentation(acknowledgement):
    identity = acknowledgement.identity
    owner = identity["owner"]
    occurrence = identity["occurrence"]
    code = identity["code"]
    if owner["kind"] == "preceptor_session":
        on_date = date.fromisoformat(occurrence["date"])
        context = (
            f"Preceptor Schedule · {on_date:%B} {on_date.day}, {on_date.year} "
            f"{occurrence['period_of_day']}"
        )
        finding_type = draft_finding_label(code)
    elif owner["kind"] == "preceptor_week":
        week = date.fromisoformat(occurrence["week_start"])
        context = f"Preceptor Schedule · week of {week:%B} {week.day}, {week.year}"
        finding_type = draft_finding_label(code)
    else:
        on_date = date.fromisoformat(occurrence["date"])
        context = f"Interview Schedule · {on_date:%B} {on_date.day}, {on_date.year}"
        finding_type = code.replace("_", " ").capitalize()
    return {
        "finding_type": finding_type,
        "context": context,
        "reason": acknowledgement.reason,
        "actor": acknowledgement.acknowledged_by.get_username(),
        "acknowledged_at": acknowledgement.acknowledged_at,
        "current": acknowledgement.is_current,
        "no_longer_applies_at": acknowledgement.no_longer_applies_at,
        "removed_by": (
            acknowledgement.removed_by.get_username() if acknowledgement.removed_by else None
        ),
        "removed_at": acknowledgement.removed_at,
        "occurrence_date": on_date if owner["kind"] != "preceptor_week" else week,
    }


def _finding_presentation(finding, session):
    content = finding_content(finding)
    physicians = tuple(
        FindingLinkPresentation(
            physician.display_name,
            f"{reverse('physician-detail', args=(physician.pk,))}?date={session.date:%Y-%m-%d}",
        )
        for physician in finding.physicians
    )
    source_links = tuple(_finding_source_link(source) for source in finding.sources)
    return FindingPresentation(
        content.message,
        content.severity,
        physicians + source_links,
        physicians,
        source_links,
    )


def _finding_source_link(source):
    view_name = {
        "faculty_template": "physician-faculty-availability-correct",
        "monthly_availability_rule": "physician-monthly-availability-rule-correct",
        "external_patient_care": "physician-role-correct",
        "community_preceptor_rule": "community-preceptor-rule-correct",
        "vacation": "vacation-correct",
        "academic_event": "academic-event-correct",
        "normal_rounder": "normal-rounder-correct",
        "casual_rounder_rule": "casual-rounder-rule-correct",
    }[source.source_type]
    args = (
        (source.record.physician_id, source.source_id)
        if source.source_type
        in {"faculty_template", "monthly_availability_rule", "external_patient_care"}
        else (source.source_id,)
    )
    return FindingLinkPresentation(finding_source_content(source), reverse(view_name, args=args))


def _assignment_slots(session):
    statuses = {
        (status.responsibility, status.primary_position): status
        for status in session_assignments(session)
    }
    return (
        {
            "position": "primary-1",
            "label": "Preceptor 1",
            "status": statuses.get((PreceptorAssignment.Responsibility.PRIMARY, 1)),
        },
        {
            "position": "primary-2",
            "label": "Preceptor 2",
            "status": statuses.get((PreceptorAssignment.Responsibility.PRIMARY, 2)),
        },
        {
            "position": "secondary",
            "label": "Secondary",
            "status": statuses.get((PreceptorAssignment.Responsibility.SECONDARY, None)),
        },
    )


def _prepared_assignment_slots(
    session, findings, *, operational_attention=True, scope_ids=(), overview=False, compact=False
):
    prepared_slots = []
    faculty_coverage_conflict = _has_month_faculty_coverage_conflict(findings)
    position_meanings = {
        "primary-1": "Preceptor 1",
        "primary-2": "Preceptor 2",
        "secondary": "Optional secondary",
    }
    finding_targets = precepting_finding_targets(session, findings)
    for slot in _assignment_slots(session):
        slot_finding = marker_for_position(finding_targets, slot["position"])
        state = (
            "conflict"
            if slot_finding and slot_finding.severity == "hard"
            else "preference"
            if slot_finding
            else "assigned"
        )
        if holiday_closure_for(session.date, session.period_of_day) and slot["status"] is None:
            state, display = "optional-empty", "Closed"
        elif (
            slot["status"] is None
            and slot["position"].startswith("primary-")
            and operational_attention
        ):
            state, display = "gap", "Unfilled"
        elif slot["status"] is None:
            # TEXT-CONTRACT: precepting-calendar-secondary-empty-marker
            # The calendar uses "-" for an actual empty optional Secondary, distinct
            # from a required Unfilled slot; scope: one hyphen, no surrounding
            # whitespace. Protecting method:
            # test_month_grid_renders_closed_and_empty_assignment_slots_with_blank_weekday_late_docs
            # in schedules/tests/test_precepting_calendar_pdf.py (PreceptingCalendarPdfTests).
            state, display = "optional-empty", "-"
        else:
            display = assigned_identity(slot["status"].assignment).display_name
            if not overview and not compact:
                display = _slot_display(display, state)
        if scope_ids and slot["status"] and slot["status"].physician.pk not in scope_ids:
            display = "Occupied"
        slot_finding_tone = slot_finding.severity if slot_finding else ""
        accessible_display = (
            _accessible_slot_display(display, state, slot_finding_tone) if overview else display
        )
        prepared_slots.append(
            {
                **slot,
                "state": state,
                "display": display,
                "accessible_label": (
                    f"{position_meanings[slot['position']]}: "
                    f"{accessible_display}"
                    f"{'. Session hard conflict: Faculty coverage gap.' if faculty_coverage_conflict else ''}"
                ),
                "finding": slot_finding if compact else None,
                "finding_tone": slot_finding_tone,
            }
        )
    return tuple(prepared_slots)


def _late_doc_slots_for_sessions(
    sessions,
    scope_ids,
    *,
    editable,
    return_month=None,
    overview=False,
    compact=False,
    focused_range=None,
):
    pm_sessions = {
        session.date: session
        for session in sessions
        if session.date.weekday() == 0 and session.period_of_day == PreceptorSession.PeriodOfDay.PM
    }
    assignments = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(date__in=pm_sessions).select_related(
            "physician"
        )
    }
    slots = {}
    for on_date, session in pm_sessions.items():
        assignment = assignments.get(on_date)
        closed = holiday_closure_for(on_date, "PM")
        finding_targets = precepting_finding_targets(session, getattr(session, "findings", ()))
        finding = marker_for_position(finding_targets, "late-doc")
        findings = tuple(
            item
            for item in getattr(session, "findings", ())
            if item.explanation.get("position") == "late-doc"
        )
        if assignment is None:
            state, display = (
                ("optional-empty", "Closed")
                if closed
                else ("gap", "Unfilled")
                if focused_range is None
                or _preceptor_date_has_focused_membership(on_date, focused_range)
                else ("optional-empty", "-")
            )
            if findings and not overview and not display:
                display = "Unfilled"
        else:
            state = (
                "conflict"
                if finding and finding.severity == "hard"
                else "preference"
                if finding
                else "assigned"
            )
            display = assigned_identity(assignment).display_name
            if not overview and not compact:
                display = _slot_display(display, state)
        if scope_ids and assignment and assignment.physician_id not in scope_ids:
            display = "Occupied"
        slot_finding_tone = finding.severity if finding else ""
        accessible_display = (
            _accessible_slot_display(display, state, slot_finding_tone) if overview else display
        )
        if closed and assignment is None:
            accessible_display += f" for {closed.name}, {closed.coverage_label}."
        actionable_cleanup = closed and assignment and editable
        url = ""
        action_label = ""
        if (editable and not closed) or actionable_cleanup:
            url = reverse("late-doc-edit", args=(on_date.isoformat(),))
            if return_month:
                url += f"?month={return_month:%Y-%m}"
            action_label = (
                f"Remove retained Late Doc for Monday {on_date:%B} {on_date.day}, {on_date.year}"
                if actionable_cleanup
                else f"Edit Late Doc for Monday {on_date:%B} {on_date.day}, {on_date.year}"
            )
        slots[session.pk] = {
            "display": display,
            "state": state,
            "accessible_label": (f"Late Doc Monday 5-6 PM: {accessible_display}"),
            "findings": (
                ()
                if overview
                else tuple(
                    FindingPresentation(finding.message, finding.severity) for finding in findings
                )
            ),
            "finding": finding if compact else None,
            "finding_tone": slot_finding_tone,
            "compact_findings": compact,
            "url": url,
            "action_label": action_label,
            "history_url": f"{reverse('late-doc-history', args=(on_date.isoformat(),))}",
        }
    return slots


def _slot_display(display, state):
    if state == "conflict":
        return f"Conflict: {display}"
    if state == "preference":
        return f"Check: {display}"
    return display


def _accessible_slot_display(display, state, finding_tone_value=""):
    if state == "conflict":
        return f"Hard conflict: {display}"
    if state == "preference":
        return f"Non-blocking finding: {display}"
    if finding_tone_value == "hard":
        return f"Hard finding: {display}"
    if finding_tone_value == "preference":
        return f"Non-blocking finding: {display}"
    return display


def _has_month_faculty_coverage_conflict(findings):
    return any(finding.code == "insufficient_faculty_equivalent_coverage" for finding in findings)


def _assignment_position(position):
    positions = {
        "primary-1": (PreceptorAssignment.Responsibility.PRIMARY, 1),
        "primary-2": (PreceptorAssignment.Responsibility.PRIMARY, 2),
        "secondary": (PreceptorAssignment.Responsibility.SECONDARY, None),
    }
    try:
        return positions[position]
    except KeyError:
        raise Http404("Unknown assignment position.") from None


def _assignment_position_label(position):
    if position.startswith("primary-"):
        return f"Preceptor {position.removeprefix('primary-')}"
    return "Secondary"


def _workspace_return_url(request, session):
    month = _return_month(request) or session.date
    return f"{reverse('monthly-workspace')}?month={month:%Y-%m}"


def _return_month(request):
    value = request.GET.get("month")
    return _requested_month(value) if value else None


def _editor_return_query(request):
    params = {}
    if month := _return_month(request):
        params["month"] = f"{month:%Y-%m}"
    return f"?{urlencode(params)}" if params else ""


def _assignment_editor_url(request, session, position):
    url = reverse("session-assignment-edit", args=(session.pk, position))
    return url + _editor_return_query(request)
