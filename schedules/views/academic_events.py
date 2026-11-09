"""Academic Event and definition HTTP workflows and projections."""

import calendar
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format

from ..academic_years import academic_year_bounds
from ..calendar_presentation import (
    AcademicEventPresentation,
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarWeek,
)
from ..forms.academic_events import (
    AcademicEventAdditionalRangeFormSet,
    AcademicEventAssignmentForm,
    AcademicEventDefinitionForm,
    AcademicEventDefinitionImportForm,
    AcademicEventForm,
    AcademicEventImportApplyForm,
    AcademicEventMonthlyRuleForm,
    AcademicEventMonthlyRuleRemovalForm,
    AcademicEventRemovalForm,
    AcademicYearSelectionForm,
)
from ..models.academic_events import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
)
from ..physician_scope import physician_scope_ids
from ..services._mutation import (
    can_manage_scheduling_administration,
    require_scheduling_administrator,
)
from ..services.academic_event_definition_import import (
    AcademicEventDefinitionImportError,
    apply_academic_event_definition_import,
    build_academic_event_definition_import_plan,
    load_academic_event_definition_import,
)
from ..services.academic_event_import_preview import (
    AcademicEventImportPreviewError,
    create_academic_event_import_preview_token,
    load_academic_event_import_preview_token,
)
from ..services.academic_event_schedule_import import (
    AcademicEventScheduleImportError,
    apply_academic_event_schedule_import,
    build_academic_event_schedule_import_plan,
)
from ..services.academic_events import (
    academic_event_definition_work_key,
    academic_event_definitions_for,
    academic_event_projected_work_items,
    academic_event_work_key,
    academic_events_overlapping,
    correct_academic_event_definition,
    correct_academic_event_monthly_rule,
    correct_academic_event_occurrence,
    create_academic_event_definition,
    create_academic_event_definition_with_initial_monthly_rule,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    deactivate_academic_event_definition,
    record_academic_event_not_occurring,
    remove_academic_event_monthly_rule,
    remove_academic_event_occurrence,
    reverse_academic_event_not_occurring,
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
from ._shared import (
    _requested_month,
    academic_year_month,
    academic_year_month_navigation_context,
    academic_year_navigation_context,
    bounded_academic_years,
    requested_bounded_academic_year,
)


@login_required
def academic_event_workspace(request):
    academic_year, month = _requested_academic_event_period(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    if month is None:
        return HttpResponseBadRequest("Choose a month within the selected academic year.")
    events = _academic_events_for_year(academic_year)
    scope_ids = physician_scope_ids(request)
    display_events = _scoped_academic_events(events, scope_ids)
    work_remaining = _academic_event_scoped_work_remaining(
        academic_year, events, _academic_event_calendar_range(month)
    )
    calendar_data = _academic_event_calendar_data(
        month,
        display_events,
        focused_work_keys={item.key for item in work_remaining.items},
    )
    return render(
        request,
        "schedules/academic_event_workspace.html",
        _academic_event_workspace_context(
            request_user=request.user,
            academic_year=academic_year,
            academic_event_view="calendar",
            month=month,
            events=events,
            display_events=display_events,
            scope_ids=scope_ids,
            work_remaining=work_remaining,
            **calendar_data,
        ),
    )


@login_required
def academic_event_list(request):
    academic_year = _requested_academic_event_year(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    events = _academic_events_for_year(academic_year)
    scope_ids = physician_scope_ids(request)
    context = _academic_event_workspace_context(
        request_user=request.user,
        academic_year=academic_year,
        academic_event_view="list",
        events=events,
        display_events=_scoped_academic_events(events, scope_ids),
        scope_ids=scope_ids,
    )
    monthly_events = tuple(
        event
        for event in context["academic_event_dated_events"]
        if event.definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
    )
    monthly_unfilled_events = tuple(
        event
        for event in monthly_events
        if not event.all_available_faculty
        and event.assigned_count < event.required_participant_count
    )
    monthly_filled_events = tuple(
        event
        for event in monthly_events
        if event.all_available_faculty or event.assigned_count >= event.required_participant_count
    )
    yearly_events = tuple(
        event
        for event in context["academic_event_dated_events"]
        if event.definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
    )
    context["academic_event_monthly_filled_events_table"] = _academic_event_list_table(
        monthly_filled_events,
        academic_year,
        scope_ids,
        context["academic_year_label"],
        identity="academic-event-monthly-filled-events",
        label="Filled Monthly Events",
        empty_state=f"No filled Monthly Events for {context['academic_year_label']}.",
    )
    context["academic_event_monthly_unfilled_events_table"] = _academic_event_list_table(
        monthly_unfilled_events,
        academic_year,
        scope_ids,
        context["academic_year_label"],
        identity="academic-event-monthly-unfilled-events",
        label="Unfilled Monthly Events",
        empty_state=f"No unfilled Monthly Events for {context['academic_year_label']}.",
    )
    context["academic_event_yearly_events_table"] = _academic_event_list_table(
        yearly_events,
        academic_year,
        scope_ids,
        context["academic_year_label"],
        identity="academic-event-yearly-events",
        label="Scheduled yearly events",
        empty_state=f"No scheduled yearly events for {context['academic_year_label']}.",
    )
    return render(
        request,
        "schedules/academic_event_list.html",
        context,
    )


def _academic_event_form_context(form):
    definitions = list(form.fields["definition"].queryset)
    selected_definition = next(
        (
            definition
            for definition in definitions
            if str(definition.pk) == str(form["definition"].value())
        ),
        None,
    )
    suffix = (form["suffix"].value() or "").strip()
    displayed_title = ""
    if selected_definition:
        displayed_title = selected_definition.base_name
        if suffix:
            displayed_title = f"{displayed_title} - {suffix}"
    return {
        "event_definition_data": {
            definition.pk: {
                "base_name": definition.base_name,
                "expected_quarter": definition.expected_quarter,
                "duration_days": definition.default_duration_days,
                "period_of_day": definition.default_period_of_day,
                "precepting_impact": definition.default_precepting_impact,
                "required_participant_count": definition.default_required_participant_count,
            }
            for definition in definitions
        },
        "displayed_event_title": displayed_title,
    }


@login_required
def academic_event_create(request):
    academic_year = _requested_academic_event_year(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    return_context = _academic_event_return_context(request, academic_year)
    current_year = _current_academic_year()
    initial = {"academic_year": academic_year}
    selection_form = AcademicEventForm(
        initial=initial,
        current_academic_year=current_year,
        existing_academic_year=academic_year,
    )
    fixed_definition = None
    if "definition" in request.GET:
        try:
            definition_id = int(request.GET["definition"])
        except TypeError, ValueError:
            return HttpResponseBadRequest("Choose an eligible event definition.")
        fixed_definition = (
            selection_form.fields["definition"].queryset.filter(pk=definition_id).first()
        )
        if fixed_definition is None:
            return HttpResponseBadRequest("Choose an eligible event definition.")
        initial["definition"] = fixed_definition
    if request.method == "GET":
        form = (
            selection_form
            if fixed_definition is None
            else AcademicEventForm(
                initial=initial,
                current_academic_year=current_year,
                existing_academic_year=academic_year,
            )
        )
    else:
        form_data = request.POST.copy()
        form_data["academic_year"] = str(academic_year)
        if fixed_definition is not None:
            # A selected definition is part of the scheduling context, not editable form data.
            form_data["definition"] = str(fixed_definition.pk)
        form = AcademicEventForm(
            form_data,
            apply_create_defaults=True,
            current_academic_year=current_year,
            existing_academic_year=academic_year,
        )
    additional_ranges = _additional_ranges_formset(
        request.POST if request.method == "POST" else None
    )
    if (
        request.method == "POST"
        and form.is_valid()
        and _additional_ranges_are_valid(additional_ranges)
    ):
        try:
            create_academic_event_occurrence(
                scheduler=request.user,
                date_ranges=_event_date_ranges(form, additional_ranges),
                **form.cleaned_data,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_academic_event_redirect_url(request, return_context))
    return render(
        request,
        "schedules/academic_event_form.html",
        {
            "form": form,
            "title": "Schedule Academic Event",
            "is_create": True,
            "academic_event_return_url": _academic_event_return_url(return_context),
            "form_action": _academic_event_action_url("academic-event-create", return_context)
            + (f"&definition={fixed_definition.pk}" if fixed_definition else ""),
            "fixed_academic_year": academic_year,
            "fixed_definition": fixed_definition,
            "submit_label": "Schedule",
            "additional_ranges": additional_ranges,
            **_academic_event_form_context(form),
        },
    )


@login_required
def academic_event_correct(request, event_id):
    event = get_object_or_404(AcademicEvent, pk=event_id)
    if event.monthly_rule_source_id:
        return redirect(_monthly_occurrence_assignment_url(event))
    if event.resolution != AcademicEvent.Resolution.DATED or not event.is_active:
        raise Http404("Only a live dated occurrence can be edited.")
    return_context = _academic_event_return_context(request, event.academic_year)
    current_year = _current_academic_year()
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        # The annual identity is fixed by the selected workspace, not posted form values.
        form_data["definition"] = str(event.definition_id)
        form_data["academic_year"] = str(event.academic_year)
    primary_start_date, primary_end_date = event.date_ranges[0]
    form = AcademicEventForm(
        form_data,
        allow_resolved_definition=True,
        allow_monthly_definition=bool(event.monthly_rule_source_id),
        current_academic_year=current_year,
        existing_academic_year=event.academic_year,
        initial={
            "definition": event.definition_id,
            "academic_year": event.academic_year,
            "suffix": event.suffix,
            "note": event.note,
            "required_participant_count": event.required_participant_count,
            "start_date": primary_start_date,
            "end_date": primary_end_date,
            "period_of_day": event.period_of_day,
            "precepting_impact": event.precepting_impact,
            "participants": event.participants.values_list("pk", flat=True),
            "all_available_faculty": event.all_available_faculty,
        },
    )
    additional_ranges = _additional_ranges_formset(
        form_data,
        initial=[{"start_date": start, "end_date": end} for start, end in event.date_ranges[1:]],
    )
    if request.method == "POST" and len(event.date_ranges) > 1 and not additional_ranges.is_bound:
        form.add_error(None, "Additional date ranges are required when editing this event.")
    if (
        request.method == "POST"
        and form.is_valid()
        and _additional_ranges_are_valid(additional_ranges)
    ):
        try:
            values = form.cleaned_data.copy()
            values.pop("definition")
            values.pop("academic_year")
            correct_academic_event_occurrence(
                event=event,
                scheduler=request.user,
                date_ranges=_event_date_ranges(form, additional_ranges),
                **values,
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_academic_event_redirect_url(request, return_context))
    return render(
        request,
        "schedules/academic_event_form.html",
        {
            "form": form,
            "title": "Edit Academic Event",
            "event": event,
            "academic_event_return_url": _academic_event_return_url(return_context),
            "form_action": _academic_event_action_url(
                "academic-event-correct", return_context, event.pk
            ),
            "remove_action": _academic_event_action_url(
                "academic-event-remove", return_context, event.pk
            ),
            "fixed_academic_year": event.academic_year,
            "fixed_definition": event.definition,
            "submit_label": "Save Academic Event",
            "additional_ranges": additional_ranges,
            **_academic_event_form_context(form),
        },
    )


@login_required
def academic_event_monthly_assignment_correct(request, event_id):
    event = get_object_or_404(
        AcademicEvent,
        pk=event_id,
        is_active=True,
        resolution=AcademicEvent.Resolution.DATED,
        monthly_rule_source_id__isnull=False,
    )
    return_context = _academic_event_return_context(request, event.academic_year)
    form = AcademicEventAssignmentForm(
        request.POST or None,
        initial={
            "participants": event.participants.values_list("pk", flat=True),
            "all_available_faculty": event.all_available_faculty,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_academic_event_occurrence(
                event=event,
                start_date=event.start_date,
                end_date=event.end_date,
                date_ranges=event.date_ranges,
                participants=form.cleaned_data["participants"],
                all_available_faculty=form.cleaned_data["all_available_faculty"],
                suffix=event.suffix,
                note=event.note,
                required_participant_count=event.required_participant_count,
                period_of_day=event.period_of_day,
                precepting_impact=event.precepting_impact,
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_academic_event_redirect_url(request, return_context))
    return render(
        request,
        "schedules/academic_event_monthly_assignment_form.html",
        {
            "form": form,
            "event": event,
            "academic_event_return_url": _academic_event_return_url(return_context),
            "form_action": _academic_event_action_url(
                "academic-event-monthly-assignment-correct", return_context, event.pk
            ),
            "rule_management_url": _monthly_occurrence_management_url(event),
        },
    )


@login_required
def academic_event_remove(request, event_id):
    event = get_object_or_404(AcademicEvent, pk=event_id)
    if event.monthly_rule_source_id:
        return redirect(_monthly_occurrence_management_url(event))
    return_context = _academic_event_return_context(request, event.academic_year)
    if request.method != "POST":
        return redirect(
            _academic_event_action_url("academic-event-correct", return_context, event.pk)
        )
    form = AcademicEventRemovalForm(request.POST)
    if form.is_valid():
        remove_academic_event_occurrence(
            event=event, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect(_academic_event_redirect_url(request, return_context))
    from ..academic_years import academic_year_for_date

    event_form = AcademicEventForm(
        allow_resolved_definition=True,
        current_academic_year=academic_year_for_date(organization_today()),
        existing_academic_year=event.academic_year,
        initial={
            "definition": event.definition_id,
            "academic_year": event.academic_year,
            "suffix": event.suffix,
            "note": event.note,
            "required_participant_count": event.required_participant_count,
            "start_date": event.start_date,
            "end_date": event.end_date,
            "period_of_day": event.period_of_day,
            "precepting_impact": event.precepting_impact,
            "participants": event.participants.values_list("pk", flat=True),
            "all_available_faculty": event.all_available_faculty,
        },
    )
    additional_ranges = AcademicEventAdditionalRangeFormSet(
        prefix="additional_ranges",
        initial=[{"start_date": start, "end_date": end} for start, end in event.date_ranges[1:]],
    )
    return render(
        request,
        "schedules/academic_event_form.html",
        {
            "form": event_form,
            "remove_form": form,
            "title": "Remove Academic Event",
            "event": event,
            "academic_event_return_url": _academic_event_return_url(return_context),
            "form_action": _academic_event_action_url(
                "academic-event-correct", return_context, event.pk
            ),
            "remove_action": _academic_event_action_url(
                "academic-event-remove", return_context, event.pk
            ),
            "fixed_academic_year": event.academic_year,
            "fixed_definition": event.definition,
            "submit_label": "Save Academic Event",
            "additional_ranges": additional_ranges,
            **_academic_event_form_context(event_form),
        },
    )


@login_required
def academic_event_mark_not_occurring(request, definition_id):
    academic_year = _requested_academic_event_year(request)
    if academic_year is None:
        return HttpResponseBadRequest("Choose a valid academic year.")
    return_context = _academic_event_return_context(request, academic_year)
    if request.method != "POST":
        return redirect(_academic_event_return_url(return_context))
    definition = get_object_or_404(AcademicEventDefinition, pk=definition_id, is_active=True)
    try:
        record_academic_event_not_occurring(
            definition=definition,
            academic_year=academic_year,
            scheduler=request.user,
        )
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    return redirect(_academic_event_return_url(return_context))


@login_required
def academic_event_reverse_not_occurring(request, event_id):
    event = get_object_or_404(
        AcademicEvent,
        pk=event_id,
        is_active=True,
        resolution=AcademicEvent.Resolution.NOT_OCCURRING,
    )
    return_context = _academic_event_return_context(request, event.academic_year)
    if request.method != "POST":
        return redirect(_academic_event_return_url(return_context))
    reverse_academic_event_not_occurring(event=event, scheduler=request.user)
    return redirect(_academic_event_return_url(return_context))


@login_required
def academic_event_history(request):
    return_url = reverse("academic-event-workspace")
    if request.GET.get("academic_year") or request.GET.get("month"):
        academic_year, month = _requested_academic_event_period(request)
        if academic_year is None or month is None:
            return HttpResponseBadRequest("Choose a valid academic year and month.")
        return_url = _academic_event_workspace_url(academic_year, "calendar", month)
    return render(
        request,
        "schedules/academic_event_history.html",
        {
            "academic_event_history_table": _academic_event_history_table(
                AcademicEventChange.objects.select_related("event", "changed_by")
            ),
            "return_url": return_url,
        },
    )


@login_required
def academic_event_definition_workspace(request):
    from ..academic_years import academic_year_label

    current_year = _current_academic_year()
    form = AcademicYearSelectionForm(
        request.GET if request.GET else {"academic_year": current_year}
    )
    if not form.is_valid():
        return HttpResponseBadRequest("Choose a valid academic year.")
    selected_year = form.cleaned_data["academic_year"]
    academic_year_choices = bounded_academic_years(current_year)
    definitions = academic_event_definitions_for(selected_year)
    monthly_rule_definitions = tuple(
        definition
        for definition in definitions
        if definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
    )
    yearly_definitions = tuple(
        definition
        for definition in definitions
        if definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
    )
    return render(
        request,
        "schedules/academic_event_definition_workspace.html",
        {
            "academic_event_definitions": definitions,
            "academic_event_monthly_rule_definitions_table": _academic_event_definition_table(
                monthly_rule_definitions,
                academic_year_label(selected_year),
                identity="academic-event-monthly-rule-definitions",
                label="Monthly-rule definitions",
            ),
            "academic_event_yearly_definitions_table": _academic_event_definition_table(
                yearly_definitions,
                academic_year_label(selected_year),
                identity="academic-event-yearly-definitions",
                label="Yearly definitions",
            ),
            "schedule_workspace_page_title": "Event definitions",
            "schedule_workspace_label": "Academic Events",
            "schedule_workspace_year_form": AcademicYearSelectionForm(
                initial={"academic_year": selected_year},
                years=academic_year_choices,
            ),
            "academic_year": selected_year,
            "academic_year_label": academic_year_label(selected_year),
            "schedule_workspace_year_picker_action": reverse("academic-event-definition-workspace"),
            "schedule_workspace_year_picker_preserves_month": False,
            "schedule_workspace_navigation": _academic_event_navigation(
                request.user, selected_year, "definitions"
            ),
            **academic_year_navigation_context(
                selected_year,
                academic_year_choices,
                lambda year: (
                    f"{reverse('academic-event-definition-workspace')}?academic_year={year}"
                ),
            ),
        },
    )


def _definition_import_presentation(plan):
    def row(source, action, detail="", *, item_type="Definition"):
        return {
            "row_number": source.row_number,
            "action": action,
            "item_type": item_type,
            "base_name": source.base_name,
            "expected_quarter": source.expected_quarter or "All year",
            "duration_days": source.default_duration_days,
            "typically_scheduled": source.typically_scheduled or "Not specified",
            "detail": detail,
        }

    return {
        "exact": tuple(row(item.source, "Exact match") for item in plan.exact),
        "different": tuple(
            row(item.source, "Different", "; ".join(item.differences)) for item in plan.different
        ),
        "create": tuple(row(source, "Create") for source in plan.create),
        "conflicts": tuple(row(item.source, "Conflict", item.message) for item in plan.conflicts),
        "unsupported": tuple(
            row(item.source, "Not imported", item.reason) for item in plan.data.unsupported
        ),
        "rule_exact": tuple(
            row(item.source, "Exact match", item_type="Recurrence rule") for item in plan.rule_exact
        ),
        "rule_different": tuple(
            row(
                item.source,
                "Different",
                "; ".join(item.differences),
                item_type="Recurrence rule",
            )
            for item in plan.rule_different
        ),
        "rule_create": tuple(
            row(source, "Create", item_type="Recurrence rule") for source in plan.rule_create
        ),
        "rule_conflicts": tuple(
            row(item.source, "Conflict", item.message, item_type="Recurrence rule")
            for item in plan.rule_conflicts
        ),
    }


@login_required
def academic_event_definition_import(request):
    require_scheduling_administrator(request.user)
    academic_year = _requested_academic_event_year(request) or _current_academic_year()
    previewing = request.method == "POST" and request.POST.get("action") == "preview"
    applying = request.method == "POST" and request.POST.get("action") == "apply"
    form = AcademicEventDefinitionImportForm(
        request.POST if previewing else None, request.FILES if previewing else None
    )
    apply_form = AcademicEventImportApplyForm(request.POST if applying else None)
    plan = None
    presentation = None
    applied = False
    requested_action = None
    preview_token = None
    if previewing and form.is_valid():
        requested_action = request.POST.get("action")
        workbook = form.cleaned_data["workbook"]
        try:
            data = load_academic_event_definition_import(workbook, source_name=workbook.name)
        except AcademicEventDefinitionImportError as error:
            form.add_error("workbook", str(error))
        else:
            plan = build_academic_event_definition_import_plan(data)
            presentation = _definition_import_presentation(plan)
            if not plan.blockers:
                preview_token = create_academic_event_import_preview_token(
                    data=data, user=request.user, workflow="definition"
                )
    elif applying:
        requested_action = "apply"
        if apply_form.is_valid():
            try:
                data = load_academic_event_import_preview_token(
                    token=apply_form.cleaned_data["preview_token"],
                    user=request.user,
                    workflow="definition",
                )
                plan = build_academic_event_definition_import_plan(data)
                presentation = _definition_import_presentation(plan)
                if plan.blockers:
                    raise AcademicEventImportPreviewError(
                        "Nothing was changed. Resolve every current conflict and preview the workbook again."
                    )
                apply_academic_event_definition_import(plan=plan, scheduler=request.user)
            except (
                AcademicEventImportPreviewError,
                PermissionDenied,
                ValidationError,
                ValueError,
            ) as error:
                apply_form.add_error(None, _validation_message(error))
            else:
                applied = True
        else:
            apply_form.add_error(None, "A preview is required. Preview the workbook again.")
    return render(
        request,
        "schedules/academic_event_definition_import.html",
        {
            "form": form,
            "apply_form": apply_form,
            "plan": plan,
            "presentation": presentation,
            "applied": applied,
            "requested_action": requested_action,
            "preview_token": preview_token,
            "schedule_workspace_page_title": "Import Event Definitions",
            "schedule_workspace_label": "Academic Events",
            "schedule_workspace_navigation": _academic_event_navigation(
                request.user, academic_year, "import-definitions"
            ),
        },
    )


def _schedule_import_presentation(plan):
    def row(source, action, detail="", *, date_ranges=(), participants=()):
        dates = "; ".join(f"{start} through {end}" for start, end in date_ranges)
        return {
            "row_number": source.row_number,
            "action": action,
            "base_name": source.base_name,
            "dates": dates or "No dates",
            "coverage": source.period or "Whole day",
            "faculty": (
                "All available Faculty"
                if source in plan.all_available_faculty
                else ", ".join(person.display_name for person in participants)
                or source.faculty
                or "None"
            ),
            "detail": detail,
        }

    planned = (*plan.exact, *plan.correct, *plan.create)
    by_source = {item.source: item for item in planned}
    return {
        "exact": tuple(
            row(
                item.source,
                "Exact match",
                date_ranges=item.date_ranges,
                participants=item.participants,
            )
            for item in plan.exact
        ),
        "correct": tuple(
            row(
                item.source, "Correct", date_ranges=item.date_ranges, participants=item.participants
            )
            for item in plan.correct
        ),
        "create": tuple(
            row(item.source, "Create", date_ranges=item.date_ranges, participants=item.participants)
            for item in plan.create
        ),
        "undated": tuple(row(source, "Unscheduled") for source in plan.undated),
        "unsupported": tuple(
            row(item.source, "Not imported", item.reason) for item in plan.unsupported
        ),
        "names": tuple(
            row(
                item.source,
                item.state.title() + " Faculty name",
                f"{item.token}: "
                + (
                    ", ".join(person.display_name for person in item.physicians)
                    if item.physicians
                    else "no match"
                ),
                date_ranges=by_source[item.source].date_ranges if item.source in by_source else (),
            )
            for item in (*plan.unique_names, *plan.ambiguous_names, *plan.unmatched_names)
        ),
    }


@login_required
def academic_event_schedule_import(request):
    require_scheduling_administrator(request.user)
    academic_year = _requested_academic_event_year(request) or _current_academic_year()
    previewing = request.method == "POST" and request.POST.get("action") == "preview"
    applying = request.method == "POST" and request.POST.get("action") == "apply"
    form = AcademicEventDefinitionImportForm(
        request.POST if previewing else None, request.FILES if previewing else None
    )
    apply_form = AcademicEventImportApplyForm(request.POST if applying else None)
    plan = None
    presentation = None
    applied = False
    requested_action = None
    preview_token = None
    if previewing and form.is_valid():
        requested_action = request.POST.get("action")
        workbook = form.cleaned_data["workbook"]
        try:
            data = load_academic_event_definition_import(workbook, source_name=workbook.name)
            plan = build_academic_event_schedule_import_plan(data)
        except (AcademicEventDefinitionImportError, AcademicEventScheduleImportError) as error:
            form.add_error("workbook", str(error))
        else:
            presentation = _schedule_import_presentation(plan)
            if not plan.blockers:
                preview_token = create_academic_event_import_preview_token(
                    data=data, user=request.user, workflow="schedule"
                )
    elif applying:
        requested_action = "apply"
        if apply_form.is_valid():
            try:
                data = load_academic_event_import_preview_token(
                    token=apply_form.cleaned_data["preview_token"],
                    user=request.user,
                    workflow="schedule",
                )
                plan = build_academic_event_schedule_import_plan(data)
                presentation = _schedule_import_presentation(plan)
                if plan.blockers:
                    raise AcademicEventImportPreviewError(
                        "Nothing was changed. Resolve every current blocker and preview the workbook again."
                    )
                apply_academic_event_schedule_import(plan=plan, scheduler=request.user)
            except (
                AcademicEventImportPreviewError,
                PermissionDenied,
                ValidationError,
                ValueError,
            ) as error:
                apply_form.add_error(None, _validation_message(error))
            else:
                applied = True
        else:
            apply_form.add_error(None, "A preview is required. Preview the workbook again.")
    return render(
        request,
        "schedules/academic_event_schedule_import.html",
        {
            "form": form,
            "apply_form": apply_form,
            "plan": plan,
            "presentation": presentation,
            "applied": applied,
            "requested_action": requested_action,
            "preview_token": preview_token,
            "schedule_workspace_page_title": "Import Annual Event Schedules",
            "schedule_workspace_label": "Academic Events",
            "schedule_workspace_navigation": _academic_event_navigation(
                request.user, academic_year, "import-schedules"
            ),
        },
    )


@login_required
def academic_event_definition_create(request):
    from ..academic_years import academic_year_for_date

    current_year = academic_year_for_date(organization_today())
    form = AcademicEventDefinitionForm(request.POST or None, current_academic_year=current_year)
    initial_monthly_rule_form = AcademicEventMonthlyRuleForm(
        request.POST or None, initial={"effective_start": organization_today()}
    )
    initial_monthly_rule_selected = (
        form.data.get("scheduling_mode") == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
    )
    definition_is_valid = request.method == "POST" and form.is_valid()
    rule_is_valid = (
        request.method == "POST"
        and initial_monthly_rule_selected
        and initial_monthly_rule_form.is_valid()
    )
    if (
        request.method == "POST"
        and definition_is_valid
        and (not initial_monthly_rule_selected or rule_is_valid)
    ):
        try:
            definition_values = form.cleaned_data.copy()
            if initial_monthly_rule_selected:
                # Monthly rules own occurrence dates, so a hidden yearly duration is irrelevant.
                definition_values.pop("default_duration_days")
                definition = create_academic_event_definition_with_initial_monthly_rule(
                    scheduler=request.user,
                    initial_rule=initial_monthly_rule_form.cleaned_data,
                    **definition_values,
                )
            else:
                definition = create_academic_event_definition(
                    scheduler=request.user, **definition_values
                )
        except ValidationError as error:
            (initial_monthly_rule_form if initial_monthly_rule_selected else form).add_error(
                None, error
            )
        else:
            if initial_monthly_rule_selected:
                return redirect("academic-event-definition-correct", definition_id=definition.pk)
            return redirect("academic-event-definition-workspace")
    return render(
        request,
        "schedules/academic_event_definition_form.html",
        {
            "form": form,
            "initial_monthly_rule_form": initial_monthly_rule_form,
            "initial_monthly_rule_selected": initial_monthly_rule_selected,
            "title": "Add Academic Event definition",
        },
    )


@login_required
def academic_event_definition_correct(request, definition_id):
    from ..academic_years import academic_year_for_date

    definition = get_object_or_404(AcademicEventDefinition, pk=definition_id, is_active=True)
    form = AcademicEventDefinitionForm(
        request.POST or None,
        current_academic_year=academic_year_for_date(organization_today()),
        existing_start_academic_year=definition.start_academic_year,
        existing_end_academic_year=definition.end_academic_year,
        initial={
            "base_name": definition.base_name,
            "scheduling_mode": definition.scheduling_mode,
            "expected_quarter": definition.expected_quarter,
            "typically_scheduled": definition.typically_scheduled,
            "default_duration_days": definition.default_duration_days,
            "default_period_of_day": definition.default_period_of_day,
            "default_precepting_impact": definition.default_precepting_impact,
            "start_academic_year": definition.start_academic_year,
            "end_academic_year": definition.end_academic_year,
            "default_required_participant_count": definition.default_required_participant_count,
            "all_available_faculty": definition.all_available_faculty,
            "default_participants": definition.default_participants.values_list("pk", flat=True),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_academic_event_definition(
                definition=definition, scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("academic-event-definition-workspace")
    return render(
        request,
        "schedules/academic_event_definition_form.html",
        {"form": form, "title": "Edit Event definition", "definition": definition},
    )


@login_required
def academic_event_definition_remove(request, definition_id):
    definition = get_object_or_404(AcademicEventDefinition, pk=definition_id, is_active=True)
    if request.method != "POST":
        return redirect("academic-event-definition-correct", definition_id=definition.pk)
    try:
        deactivate_academic_event_definition(definition=definition, scheduler=request.user)
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        return redirect("academic-event-definition-correct", definition_id=definition.pk)
    return redirect("academic-event-definition-workspace")


@login_required
def academic_event_definition_history(request):
    return render(
        request,
        "schedules/academic_event_definition_history.html",
        {
            "academic_event_definition_history_table": _academic_event_definition_history_table(
                AcademicEventDefinitionChange.objects.select_related("definition", "changed_by")
            )
        },
    )


def _monthly_rule_values(rule):
    return {
        "weekday": rule.weekday,
        "ordinals": [str(value) for value in rule.ordinals],
        "effective_start": rule.effective_start,
        "effective_end": rule.effective_end,
    }


@login_required
def academic_event_monthly_rule_create(request, definition_id):
    definition = get_object_or_404(
        AcademicEventDefinition,
        pk=definition_id,
        is_active=True,
        scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
    )
    form = AcademicEventMonthlyRuleForm(
        request.POST or None, initial={"effective_start": organization_today()}
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_academic_event_monthly_rule(
                definition=definition, scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("academic-event-definition-correct", definition_id=definition.pk)
    return render(
        request,
        "schedules/academic_event_monthly_rule_form.html",
        {
            "form": form,
            "definition": definition,
            "rules": definition.monthly_rules.all(),
            "title": "Add Academic Event rule",
        },
    )


@login_required
def academic_event_monthly_rule_correct(request, rule_id):
    rule = get_object_or_404(
        AcademicEventMonthlyRule.objects.select_related("definition"), pk=rule_id
    )
    form = AcademicEventMonthlyRuleForm(request.POST or None, initial=_monthly_rule_values(rule))
    if request.method == "POST" and form.is_valid():
        try:
            correct_academic_event_monthly_rule(
                rule=rule, scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("academic-event-definition-correct", definition_id=rule.definition_id)
    return render(
        request,
        "schedules/academic_event_monthly_rule_form.html",
        {
            "form": form,
            "definition": rule.definition,
            "rule": rule,
            "rules": rule.definition.monthly_rules.all(),
            "title": "Edit Academic Event rule",
        },
    )


@login_required
def academic_event_monthly_rule_remove(request, rule_id):
    rule = get_object_or_404(AcademicEventMonthlyRule, pk=rule_id)
    if request.method == "POST":
        form = AcademicEventMonthlyRuleRemovalForm(request.POST)
        if form.is_valid():
            remove_academic_event_monthly_rule(
                rule=rule, scheduler=request.user, reason=form.cleaned_data["reason"]
            )
            return redirect("academic-event-definition-correct", definition_id=rule.definition_id)
    return redirect("academic-event-monthly-rule-correct", rule_id=rule.pk)


def _validation_message(error):
    return "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)


def _academic_event_history_table(changes):
    return DataTable(
        identity="academic-event-history",
        label="Academic Event Schedule history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Event",
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
                    DataTableCell(
                        (
                            f"{change.event.title} (Monthly rule occurrence)"
                            if change.event and change.event.monthly_rule_source_id
                            else change.event.title
                            if change.event
                            else f"Event #{change.event_id_at_recording}"
                        ),
                        row_header=True,
                    ),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No Academic Event changes recorded.",
    )


def _academic_event_definition_history_table(changes):
    return DataTable(
        identity="academic-event-definition-history",
        label="Event definition history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Definition",
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
                        change.definition.base_name
                        if change.definition
                        else f"Definition #{change.definition_id_at_recording}",
                        row_header=True,
                    ),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                )
            )
            for change in changes
        ),
        empty_state="No Event definition changes recorded.",
    )


def _current_academic_year():
    from ..academic_years import academic_year_for_date

    return academic_year_for_date(organization_today())


def _requested_academic_event_year(request):
    return requested_bounded_academic_year(request, _current_academic_year())


def _requested_academic_event_period(request):
    from ..academic_years import academic_year_for_date

    picker = request.GET.get("picker")
    if picker == "academic_year":
        academic_year = _requested_academic_event_year(request)
        if academic_year is None:
            return None, None
        value = request.GET.get("month")
        if not value:
            return academic_year, _requested_academic_event_month(request, academic_year)
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

    academic_year = _requested_academic_event_year(request)
    if academic_year is None:
        return None, None
    return academic_year, _requested_academic_event_month(request, academic_year)


def _requested_academic_event_month(request, academic_year):
    from ..academic_years import academic_year_for_date

    value = request.GET.get("month")
    if value:
        month = _requested_month(value)
        if month is None or month.strftime("%Y-%m") != value:
            return None
    else:
        today = organization_today()
        month = (
            today.replace(day=1)
            if academic_year_for_date(today) == academic_year
            else date(academic_year, 7, 1)
        )
    return month if academic_year_for_date(month) == academic_year else None


def _academic_event_return_context(request, academic_year):
    requested_year = _requested_academic_event_year(request)
    if requested_year != academic_year:
        requested_year = academic_year
    view = request.GET.get("view", "calendar")
    if view not in {"calendar", "list"}:
        view = "calendar"
    month = _requested_academic_event_month(request, requested_year) if view == "calendar" else None
    return {"academic_year": requested_year, "view": view, "month": month}


def _academic_event_workspace_url(academic_year, view="calendar", month=None):
    view_name = "academic-event-list" if view == "list" else "academic-event-workspace"
    url = f"{reverse(view_name)}?academic_year={academic_year}"
    return f"{url}&month={month:%Y-%m}" if view == "calendar" and month else url


def _academic_event_navigation(user, academic_year, current_view, month=None):
    navigation = [
        {
            "label": "Calendar",
            "url": _academic_event_workspace_url(academic_year, "calendar", month),
            "current": current_view == "calendar",
        },
        {
            "label": "List",
            "url": _academic_event_workspace_url(academic_year, "list"),
            "current": current_view == "list",
        },
        {
            "label": "Definitions",
            "url": f"{reverse('academic-event-definition-workspace')}?academic_year={academic_year}",
            "current": current_view == "definitions",
        },
    ]
    if can_manage_scheduling_administration(user):
        navigation.extend(
            (
                {
                    "label": "Import Definitions",
                    "url": f"{reverse('academic-event-definition-import')}?academic_year={academic_year}",
                    "current": current_view == "import-definitions",
                },
                {
                    "label": "Import Schedules",
                    "url": f"{reverse('academic-event-schedule-import')}?academic_year={academic_year}",
                    "current": current_view == "import-schedules",
                },
            )
        )
    return tuple(navigation)


def _academic_event_return_url(return_context):
    return _academic_event_workspace_url(**return_context)


def _academic_event_redirect_url(request, return_context):
    if any(request.GET.get(key) for key in ("academic_year", "view", "month")):
        return _academic_event_return_url(return_context)
    return reverse("academic-event-workspace")


def _academic_event_action_url(view_name, return_context, *args):
    url = reverse(view_name, args=args)
    query = f"academic_year={return_context['academic_year']}&view={return_context['view']}"
    if return_context["view"] == "calendar" and return_context["month"]:
        query += f"&month={return_context['month']:%Y-%m}"
    return f"{url}?{query}"


def _academic_events_for_year(academic_year):
    yearly = tuple(
        AcademicEvent.objects.filter(
            is_active=True,
            academic_year=academic_year,
            monthly_rule__isnull=True,
            monthly_rule_source_id__isnull=True,
        )
        .select_related("definition")
        .prefetch_related("participants", "ranges")
    )
    monthly = tuple(
        event
        for event in academic_events_overlapping(*academic_year_bounds(academic_year))
        if event.monthly_rule_source_id
    )
    return tuple(
        sorted(
            (*yearly, *monthly),
            key=lambda event: (event.start_date or date.max, event.base_name.casefold(), event.pk),
        )
    )


def _academic_event_workspace_context(
    *,
    request_user,
    academic_year,
    academic_event_view,
    events,
    display_events=None,
    scope_ids=(),
    month=None,
    work_remaining=None,
    **calendar_data,
):
    from ..academic_years import academic_year_for_date, academic_year_label

    selected_range = (
        _academic_event_calendar_range(month)
        if academic_event_view == "calendar"
        else InclusiveDateRange(*academic_year_bounds(academic_year))
    )
    if work_remaining is None:
        work_remaining = _academic_event_scoped_work_remaining(
            academic_year, events, selected_range
        )
    work_keys = {item.key for item in work_remaining.items}
    dated_events = tuple(
        event
        for event in (display_events if display_events is not None else events)
        if event.resolution == AcademicEvent.Resolution.DATED
    )
    for event in dated_events:
        event.assigned_count = len(_academic_event_participants(event))
        event.operational_attention = academic_event_work_key(event) in work_keys
        event.remaining_count = (
            max(0, event.required_participant_count - event.assigned_count)
            if event.operational_attention
            else 0
        )
    not_occurring_events = tuple(
        sorted(
            (
                event
                for event in events
                if event.resolution == AcademicEvent.Resolution.NOT_OCCURRING
                and event.definition.scheduling_mode
                == AcademicEventDefinition.SchedulingMode.YEARLY
            ),
            key=lambda event: (
                event.expected_quarter,
                event.definition.typically_scheduled == "",
                event.definition.typically_scheduled.casefold(),
                event.definition.typically_scheduled,
                event.base_name.casefold(),
                event.base_name,
                event.pk,
            ),
        )
    )
    schedulable_definitions = tuple(
        definition
        for definition in academic_event_definitions_for(academic_year)
        if definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
        and academic_event_definition_work_key(definition.pk, academic_year) in work_keys
        and not any(event.definition_id == definition.pk for event in events)
    )
    underfilled_events = tuple(
        event for event in events if academic_event_work_key(event) in work_keys
    )
    unpopulated_events = tuple(
        event for event in underfilled_events if not _academic_event_participants(event)
    )
    partial_events = tuple(
        event for event in underfilled_events if _academic_event_participants(event)
    )
    physicians_needed = sum(
        event.required_participant_count - len(_academic_event_participants(event))
        for event in underfilled_events
    )
    current_academic_year = _current_academic_year()
    academic_year_choices = bounded_academic_years(current_academic_year)
    context = {
        "academic_year": academic_year,
        "academic_year_label": academic_year_label(academic_year),
        "schedule_workspace_page_title": "Academic Events",
        "schedule_workspace_label": "Academic Events",
        "schedule_workspace_year_picker_action": reverse(
            "academic-event-workspace"
            if academic_event_view == "calendar"
            else "academic-event-list"
        ),
        "schedule_workspace_year_picker_preserves_month": academic_event_view == "calendar",
        "academic_event_selected_month": month,
        "academic_event_view": academic_event_view,
        "schedule_workspace_navigation": _academic_event_navigation(
            request_user, academic_year, academic_event_view, month
        ),
        "schedule_workspace_month_picker_action": reverse("academic-event-workspace"),
        "schedule_workspace_month_picker_preserved_query": {
            "academic_year": academic_year,
            "picker": "month",
        },
        "schedule_workspace_year_form": AcademicYearSelectionForm(
            initial={"academic_year": academic_year},
            years=academic_year_choices,
        ),
        "academic_event_dated_events": dated_events,
        "academic_event_not_occurring_events": not_occurring_events,
        "academic_event_schedulable_definitions": schedulable_definitions,
        "academic_event_underfilled_events": underfilled_events,
        "academic_event_unpopulated_events": unpopulated_events,
        "academic_event_partial_events": partial_events,
        "academic_event_physicians_needed": physicians_needed,
        "work_remaining": work_remaining,
        "academic_event_add_action": _academic_event_action_url(
            "academic-event-create",
            {"academic_year": academic_year, "view": academic_event_view, "month": month},
        ),
        "academic_event_action_title": "Schedule an event",
        "academic_event_history_url": (
            f"{reverse('academic-event-history')}?academic_year={academic_year}&month={month:%Y-%m}"
            if month
            else reverse("academic-event-history")
        ),
        "academic_event_history_label": "Academic Event History",
        **academic_year_navigation_context(
            academic_year,
            academic_year_choices,
            lambda selected_year: _academic_event_workspace_url(
                selected_year,
                academic_event_view,
                academic_year_month(selected_year, month) if month else None,
            ),
        ),
        **(
            academic_year_month_navigation_context(
                academic_year_choices,
                month,
                lambda selected_month: _academic_event_workspace_url(
                    academic_year_for_date(selected_month), "calendar", selected_month
                ),
            )
            if month
            else {}
        ),
        **calendar_data,
    }
    if academic_event_view == "calendar":
        context["schedule_workspace_selected_month"] = month
        context["schedule_workspace_month_min"] = date(current_academic_year - 5, 7, 1)
        context["schedule_workspace_month_max"] = date(current_academic_year + 6, 6, 1)
        context["schedule_workspace_surface_label"] = "Academic Event schedule"
    return context


def _academic_event_scoped_work_remaining(academic_year, events, selected_range):
    return focused_work_remaining(
        academic_event_projected_work_items(academic_year, events), selected_range
    )


def _academic_event_calendar_range(month):
    return InclusiveDateRange(
        month, date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    )


def _scoped_academic_events(events, scope_ids):
    if not scope_ids:
        return events
    displayed = []
    for event in events:
        if event.resolution != AcademicEvent.Resolution.DATED:
            displayed.append(event)
            continue
        participants = _academic_event_participants(event)
        focused = tuple(participant for participant in participants if participant.pk in scope_ids)
        event.focused_participants = focused
        event.outside_focus_participant_count = len(participants) - len(focused)
        if focused or len(participants) < event.required_participant_count:
            displayed.append(event)
    return tuple(displayed)


def _academic_event_definition_table(
    definitions,
    academic_year_label,
    *,
    identity="academic-event-definitions",
    label="Academic Event definitions",
):
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Base name",
                "Expected quarter",
                "Typically scheduled",
                "Default duration",
                "Default coverage",
                "Effective academic years",
                "Actions",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(definition.base_name, row_header=True),
                    DataTableCell(definition.expected_quarter or "Not applicable"),
                    DataTableCell(definition.typically_scheduled or "Not specified"),
                    DataTableCell(
                        f"{definition.default_duration_days} day"
                        f"{'s' if definition.default_duration_days != 1 else ''}"
                    ),
                    DataTableCell(definition.get_default_period_of_day_display()),
                    DataTableCell(definition.effective_range_label),
                    DataTableCell(
                        "Edit",
                        kind="link",
                        url=reverse("academic-event-definition-correct", args=(definition.pk,)),
                    ),
                )
            )
            for definition in definitions
        ),
        empty_state=f"No {label.lower()} apply to {academic_year_label}.",
    )


def _academic_event_list_table(
    events,
    academic_year,
    scope_ids,
    academic_year_label,
    *,
    identity="academic-event-list",
    label="Scheduled Academic Events",
    empty_state=None,
):
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Event",
                "Type",
                "Typically scheduled",
                "Inclusive dates",
                "Coverage",
                "Impact",
                "Participants",
                "Assigned",
                "Status",
                "Actions",
            )
        ),
        rows=tuple(_academic_event_list_row(event, academic_year, scope_ids) for event in events),
        empty_state=empty_state or f"No scheduled events for {academic_year_label}.",
    )


def _academic_event_list_row(event, academic_year, scope_ids):
    participants = (
        tuple(getattr(event, "focused_participants", ()))
        if scope_ids
        else _academic_event_participants(event)
    )
    participant_names = ", ".join(participant.display_name for participant in participants)
    if event.all_available_faculty:
        participant_names = "All available Faculty"
    elif scope_ids and not participants:
        participant_names = "No selected participants"
    elif not participant_names:
        participant_names = "None"
    outside_count = getattr(event, "outside_focus_participant_count", 0)
    if outside_count:
        participant_names += f"; {outside_count} outside focus"
    is_monthly = (
        event.definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
    )
    attention = event.operational_attention and event.participant_status != "Complete"
    required_work = attention
    if (
        not event.all_available_faculty
        and event.assigned_count < event.required_participant_count
        and (is_monthly or event.operational_attention)
    ):
        # TEXT-CONTRACT: academic-event-list-assignment-incomplete-status —
        # docs/design.md, Academic Events assignment-state presentation, requires
        # the exact List label and remaining-count form;
        # capitalization, punctuation, and whitespace matter. Protected by
        # schedules/tests/test_views.py::AcademicEventWorkspaceTests.test_list_uses_scheduler_wording_and_compact_semantic_resolution_actions.
        status = DataTableCell(
            "Assignment incomplete "
            f"({event.required_participant_count - event.assigned_count} needed)",
            kind="status",
            tone="danger" if required_work else "",
        )
    elif is_monthly:
        # TEXT-CONTRACT: academic-event-list-filled-status —
        # docs/design.md, Academic Events assignment-state presentation, requires
        # the exact List status label Filled; capitalization and whitespace matter.
        # Protected by schedules/tests/test_monthly_academic_event_rules.py::MonthlyAcademicEventRuleTests.test_monthly_browser_simplifies_event_card_assignment_details.
        status = DataTableCell("Filled", kind="status")
    elif event.operational_attention:
        status = DataTableCell("Filled", kind="status")
    else:
        status = DataTableCell("Recorded", kind="status")
    action_url = (
        _monthly_occurrence_assignment_url(
            event, {"academic_year": academic_year, "view": "list", "month": None}
        )
        if is_monthly
        else f"{reverse('academic-event-correct', args=(event.pk,))}?academic_year={academic_year}&view=list"
    )
    return DataTableRow(
        (
            DataTableCell(event.title, row_header=True),
            DataTableCell(
                "Monthly Event"
                if event.definition.scheduling_mode
                == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
                else "Yearly"
            ),
            DataTableCell(event.definition.typically_scheduled or "Not specified"),
            DataTableCell(_date_ranges_label(event)),
            DataTableCell(
                event.get_period_of_day_display()
                if is_monthly
                else event.presentation_coverage_label
            ),
            DataTableCell(event.get_precepting_impact_display(), kind="status"),
            DataTableCell(participant_names),
            DataTableCell(
                "All Faculty"
                if event.all_available_faculty
                else f"{event.assigned_count}/{event.required_participant_count}"
            ),
            status,
            DataTableCell(
                ("Edit assignments" if is_monthly else "Edit"),
                kind="link",
                url=action_url,
            ),
        ),
        tone="danger" if required_work else "",
    )


def _monthly_occurrence_assignment_url(event, return_context=None):
    if return_context is None:
        return reverse("academic-event-monthly-assignment-correct", args=(event.pk,))
    return _academic_event_action_url(
        "academic-event-monthly-assignment-correct", return_context, event.pk
    )


def _monthly_occurrence_management_url(event):
    if event.monthly_rule_id:
        return reverse("academic-event-monthly-rule-correct", args=(event.monthly_rule_id,))
    return reverse("academic-event-definition-correct", args=(event.definition_id,))


def _academic_event_calendar_data(month, events, *, focused_work_keys):
    displayed_weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    calendar_start = displayed_weeks[0][0]
    calendar_end = displayed_weeks[-1][-1]
    events = tuple(
        event
        for event in events
        if event.resolution == AcademicEvent.Resolution.DATED
        and any(start <= calendar_end and end >= calendar_start for start, end in event.date_ranges)
    )
    weeks = tuple(
        _academic_event_calendar_week(month, week, events, focused_work_keys)
        for week in displayed_weeks
    )
    return {
        "academic_event_calendar_weeks": weeks,
        "calendar": _academic_event_calendar_lattice(month, weeks),
    }


def _academic_event_calendar_week(month, week_dates, events, focused_work_keys):
    week_start, week_end = week_dates[0], week_dates[-1]
    segments = []
    for event in events:
        for range_start, range_end in event.date_ranges:
            if range_start > week_end or range_end < week_start:
                continue
            start_date = max(range_start, week_start)
            end_date = min(range_end, week_end)
            is_monthly = (
                event.definition.scheduling_mode
                == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
            )
            coverage_label = (
                event.get_period_of_day_display()
                if is_monthly
                else event.presentation_coverage_label
            )
            assigned_count = len(_academic_event_participants(event))
            assignment_incomplete = (
                not event.all_available_faculty
                and assigned_count < event.required_participant_count
            )
            segments.append(
                {
                    "event": event,
                    "title": event.title,
                    "start_date": start_date,
                    "end_date": end_date,
                    "coverage_label": coverage_label,
                    "participant_status": event.participant_status,
                    "assigned_count": assigned_count,
                    "required_count": event.required_participant_count,
                    "needs_participants": (
                        assignment_incomplete
                        and academic_event_work_key(event) in focused_work_keys
                    ),
                    "assignment_status": (
                        "Assignment incomplete"
                        if assignment_incomplete
                        and (is_monthly or academic_event_work_key(event) in focused_work_keys)
                        else ""
                    ),
                }
            )
    segments.sort(
        key=lambda segment: (
            segment["start_date"],
            segment["title"].casefold(),
            segment["title"],
            segment["event"].pk,
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
        segment["grid_column"] = (
            f"{(segment['start_date'] - week_start).days + 1} / span "
            f"{(segment['end_date'] - segment['start_date']).days + 1}"
        )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "week_label": (
            f"{week_start.strftime('%B')} {week_start.day} through "
            f"{week_end.strftime('%B')} {week_end.day}, {week_end.year}"
        ),
        "days": tuple(
            {
                "date": day,
                "date_label": f"{day.strftime('%b')} {day.day}",
                "in_selected_month": day.year == month.year and day.month == month.month,
            }
            for day in week_dates
        ),
        "segments": tuple(segments),
        "lane_rows": max(1, len(lanes)),
    }


def _academic_event_calendar_lattice(month, weeks):
    return CalendarLattice(
        label=f"{month:%B %Y} Academic Events calendar",
        columns=("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
        variant="seven-column",
        has_gutter=False,
        holiday_header_labels=False,
        weeks=tuple(
            CalendarWeek(
                label=week["week_label"],
                days=tuple(
                    CalendarDay(
                        day["date"].isoformat(), day["date_label"], day["in_selected_month"]
                    )
                    for day in week["days"]
                ),
                lanes=(
                    CalendarLane(
                        "Academic Event entries",
                        tuple(
                            CalendarItem(
                                (segment["start_date"] - week["week_start"]).days + 1,
                                (segment["end_date"] - segment["start_date"]).days + 1,
                                "schedules/calendar_academic_event_item.html",
                                AcademicEventPresentation(
                                    segment["title"],
                                    segment["coverage_label"],
                                    segment["assigned_count"],
                                    segment["required_count"],
                                    segment["event"].all_available_faculty,
                                    segment["needs_participants"],
                                    segment["assignment_status"],
                                    (
                                        _monthly_occurrence_assignment_url(
                                            segment["event"],
                                            {
                                                "academic_year": segment["event"].academic_year,
                                                "view": "calendar",
                                                "month": month,
                                            },
                                        )
                                        if segment["event"].monthly_rule_source_id
                                        else f"{reverse('academic-event-correct', args=(segment['event'].pk,))}?academic_year={segment['event'].academic_year}&view=calendar&month={month:%Y-%m}"
                                    ),
                                    _academic_event_participant_detail(segment["event"]),
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


def _academic_event_participant_detail(event):
    if not hasattr(event, "focused_participants"):
        return ""
    names = ", ".join(participant.display_name for participant in event.focused_participants)
    outside = event.outside_focus_participant_count
    if outside:
        return f"{names + '; ' if names else ''}{outside} participant(s) outside focus"
    return names


def _academic_event_participants(event):
    return tuple(event.participants.all())


def _event_date_ranges(form, additional_ranges):
    date_ranges = [(form.cleaned_data["start_date"], form.cleaned_data["end_date"])]
    if additional_ranges.is_bound:
        date_ranges.extend(
            (range_form.cleaned_data["start_date"], range_form.cleaned_data["end_date"])
            for range_form in additional_ranges
            if range_form.cleaned_data
            and not range_form.cleaned_data.get("DELETE")
            and range_form.cleaned_data.get("start_date")
        )
    return tuple(date_ranges)


def _additional_ranges_formset(data, *, initial=None):
    prefix = "additional_ranges"
    return AcademicEventAdditionalRangeFormSet(
        data if data is not None and f"{prefix}-TOTAL_FORMS" in data else None,
        prefix=prefix,
        initial=initial,
    )


def _additional_ranges_are_valid(additional_ranges):
    return not additional_ranges.is_bound or additional_ranges.is_valid()


def _date_ranges_label(event):
    return "; ".join(
        f"{date_format(start, 'N j, Y')} through {date_format(end, 'N j, Y')}"
        for start, end in event.date_ranges
    )
