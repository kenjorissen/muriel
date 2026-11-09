"""Scheduling Administrator browser workflow for combined Precepting imports."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import render
from django.urls import reverse

from ..forms.preceptors import PreceptingWorkbookImportApplyForm, PreceptingWorkbookImportForm
from ..services._mutation import require_scheduling_administrator
from ..services.operational_attention import organization_today
from ..services.precepting_workbook_import import (
    PreceptingWorkbookImportError,
    apply_precepting_workbook_import,
    build_precepting_workbook_import_plan,
    load_precepting_workbook,
)
from ..services.precepting_workbook_import_preview import (
    PreceptingWorkbookImportPreviewError,
    create_precepting_workbook_import_preview_token,
    load_precepting_workbook_import_preview_token,
)
from .preceptors import preceptor_workspace_navigation


def _validation_message(error):
    return "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)


def _presentation(plan):
    def precepting_row(item):
        return {
            "date": item.on_date,
            "period": item.period_of_day,
            "state": item.state,
            "assignments": tuple(
                f"{assignment.physician.display_name} "
                f"({'secondary' if assignment.responsibility == 'secondary' else assignment.kind.replace('_', ' ')})"
                for assignment in item.assignments
            ),
        }

    def assignment_row(item):
        return {
            "date": item.on_date,
            "state": item.state,
            "physician": item.physician.display_name if item.physician else "No assignment",
        }

    return {
        "precepting": tuple(precepting_row(item) for item in plan.precepting),
        "late_docs": tuple(assignment_row(item) for item in plan.late_docs),
        "interviews": tuple(assignment_row(item) for item in plan.interviews),
        "names": tuple(
            {
                "name": item.name,
                "kind": item.kind.replace("_", " "),
                "state": item.state,
                "matches": ", ".join(person.display_name for person in item.physicians)
                or "No match",
            }
            for item in (*plan.unique_names, *plan.unmatched_names, *plan.ambiguous_names)
        ),
    }


@login_required
def precepting_workbook_import(request):
    require_scheduling_administrator(request.user)
    previewing = request.method == "POST" and request.POST.get("action") == "preview"
    applying = request.method == "POST" and request.POST.get("action") == "apply"
    form = PreceptingWorkbookImportForm(
        request.POST if previewing else None, request.FILES if previewing else None
    )
    apply_form = PreceptingWorkbookImportApplyForm(request.POST if applying else None)
    plan = presentation = None
    preview_token = None
    applied = False
    requested_action = None
    if previewing and form.is_valid():
        requested_action = "preview"
        workbook = form.cleaned_data["workbook"]
        try:
            plan = build_precepting_workbook_import_plan(
                load_precepting_workbook(workbook, source_name=workbook.name)
            )
            presentation = _presentation(plan)
            if not plan.blockers:
                preview_token = create_precepting_workbook_import_preview_token(
                    data=plan.data, user=request.user, workflow="precepting"
                )
        except PreceptingWorkbookImportError as error:
            form.add_error("workbook", str(error))
    elif applying:
        requested_action = "apply"
        if apply_form.is_valid():
            try:
                data = load_precepting_workbook_import_preview_token(
                    token=apply_form.cleaned_data["preview_token"],
                    user=request.user,
                    workflow="precepting",
                )
                plan = build_precepting_workbook_import_plan(data)
                presentation = _presentation(plan)
                if plan.blockers:
                    raise PreceptingWorkbookImportPreviewError(
                        "Nothing was changed. Resolve every blocker and preview the workbook again."
                    )
                apply_precepting_workbook_import(plan=plan, scheduler=request.user)
            except (
                PermissionDenied,
                PreceptingWorkbookImportError,
                PreceptingWorkbookImportPreviewError,
                ValidationError,
                ValueError,
            ) as error:
                apply_form.add_error(None, _validation_message(error))
            else:
                applied = True
        else:
            apply_form.add_error(None, "A preview is required. Preview the workbook again.")
    today = organization_today()
    return render(
        request,
        "schedules/precepting_workbook_import.html",
        {
            "form": form,
            "apply_form": apply_form,
            "plan": plan,
            "presentation": presentation,
            "preview_token": preview_token,
            "applied": applied,
            "requested_action": requested_action,
            "return_url": reverse("monthly-workspace"),
            "schedule_workspace_page_title": "Import Schedule",
            "schedule_workspace_label": "Preceptor Schedule",
            "schedule_workspace_navigation": preceptor_workspace_navigation(
                current="import",
                month=today.replace(day=1),
                user=request.user,
            ),
        },
    )
