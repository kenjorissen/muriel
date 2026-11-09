"""Scheduling Administrator upload workflow for the legacy vacation calendar."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import render

from ..forms.vacations import VacationCalendarImportForm
from ..services._mutation import require_scheduling_administrator
from ..services.vacation_calendar_import import (
    IGNORED_MARKERS,
    VacationCalendarImportError,
    apply_vacation_calendar_import,
    build_vacation_calendar_import_plan,
    load_vacation_calendar,
)
from .vacation_navigation import vacation_workspace_navigation


def _import_presentation(plan):
    source_by_key = {source.key: source for source in plan.data.faculty}
    ignored_totals = {}
    ignored_details = []
    for summary in plan.data.ignored_markers:
        ignored_totals[summary.marker] = ignored_totals.get(summary.marker, 0) + len(summary.dates)
        ignored_details.append(
            {
                "marker": summary.marker,
                "label": IGNORED_MARKERS[summary.marker].title(),
                "physician": source_by_key[summary.faculty_key].display_name,
                "count": len(summary.dates),
            }
        )
    ignored_summary = tuple(
        {
            "marker": marker,
            "label": IGNORED_MARKERS[marker].title(),
            "count": count,
        }
        for marker, count in sorted(ignored_totals.items())
    )
    operations = []
    for item in plan.create:
        operations.append(
            {
                "action": "Create",
                "kind": "create",
                "physician": item.physician.display_name,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "coverage": "Whole day",
            }
        )
    for item in plan.reuse:
        operations.append(
            {
                "action": "Reuse",
                "kind": "reuse",
                "physician": item.physician.display_name,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "coverage": item.get_period_of_day_display(),
            }
        )
    for item in plan.remove:
        operations.append(
            {
                "action": "Remove",
                "kind": "remove",
                "physician": item.physician.display_name,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "coverage": item.get_period_of_day_display(),
            }
        )
    vacation_days = sum(
        (vacation.end_date - vacation.start_date).days + 1 for vacation in plan.data.vacations
    )
    return {
        "vacation_days": vacation_days,
        "ignored_summary": ignored_summary,
        "ignored_details": tuple(ignored_details),
        "operations": tuple(operations),
    }


def _validation_message(error):
    return "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)


@login_required
def vacation_calendar_import(request):
    require_scheduling_administrator(request.user)
    form = VacationCalendarImportForm(
        request.POST or None,
        request.FILES or None,
    )
    plan = None
    presentation = None
    applied = False
    requested_action = None
    if request.method == "POST" and form.is_valid():
        requested_action = request.POST.get("action")
        if requested_action not in {"preview", "apply"}:
            form.add_error(None, "Choose Preview workbook or Apply import.")
        else:
            workbook = form.cleaned_data["workbook"]
            replace = form.cleaned_data["replace"]
            try:
                data = load_vacation_calendar(workbook, source_name=workbook.name)
            except VacationCalendarImportError as error:
                form.add_error("workbook", str(error))
            else:
                plan = build_vacation_calendar_import_plan(data, replace=replace)
                presentation = _import_presentation(plan)
                if requested_action == "apply":
                    if plan.blockers:
                        form.add_error(
                            None,
                            "Nothing was changed. Resolve every blocker before applying the import.",
                        )
                    else:
                        try:
                            apply_vacation_calendar_import(plan, scheduler=request.user)
                        except (PermissionDenied, ValidationError, ValueError) as error:
                            form.add_error(None, _validation_message(error))
                        else:
                            applied = True
    return render(
        request,
        "schedules/vacation_calendar_import.html",
        {
            "form": form,
            "plan": plan,
            "presentation": presentation,
            "applied": applied,
            "requested_action": requested_action,
            "schedule_workspace_page_title": "Import Vacation Calendar",
            "schedule_workspace_label": "Vacation",
            "schedule_workspace_navigation": vacation_workspace_navigation(
                "import", can_import=True
            ),
        },
    )
