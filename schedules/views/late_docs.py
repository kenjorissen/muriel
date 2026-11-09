"""Late Doc editing and retained history in the Preceptor Schedule workflow."""

from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse

from ..calendar_presentation import FindingLinkPresentation, FindingPresentation
from ..forms.late_docs import LateDocAssignmentForm, LateDocRemovalForm
from ..services.holidays import holiday_closure_for
from ..services.late_docs import (
    assign_late_doc,
    late_doc_assignment_candidates_on,
    late_doc_assignment_changes,
    late_doc_assignment_for_date,
    remove_late_doc_assignment,
)
from ..services.operational_attention import is_operational_date
from ..services.preceptors import (
    initialize_preceptor_periods,
    operational_draft_findings_for_week,
)
from ._shared import _requested_month
from .preceptors import preceptor_workspace_navigation


def _monday(value):
    try:
        on_date = date.fromisoformat(value)
    except ValueError:
        raise Http404("Late Doc date must be a Monday.") from None
    if on_date.weekday() != 0:
        raise Http404("Late Doc date must be a Monday.")
    return on_date


def _return_url(request, on_date):
    month = _requested_month(request.GET.get("month")) if request.GET.get("month") else None
    month = month or on_date.replace(day=1)
    return f"{reverse('monthly-workspace')}?month={month:%Y-%m}"


def _late_doc_workspace_context(on_date, request):
    month = _requested_month(request.GET.get("month")) if request.GET.get("month") else None
    return_query = f"?month={month:%Y-%m}" if month else ""
    return {
        "return_query": return_query,
        "form_action": reverse("late-doc-edit", args=(on_date.isoformat(),)) + return_query,
        "schedule_workspace_page_title": "Preceptor Schedule",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current="calendar",
            month=month or on_date.replace(day=1),
            user=request.user,
        ),
    }


def _late_doc_editor_findings(on_date):
    initialize_preceptor_periods(on_date)
    return tuple(
        _late_doc_finding_presentation(finding, on_date)
        for finding in operational_draft_findings_for_week(on_date).findings
        if finding.explanation.get("position") == "late-doc"
    )


def _late_doc_finding_presentation(finding, on_date):
    physicians = tuple(
        FindingLinkPresentation(
            physician.display_name,
            f"{reverse('physician-detail', args=(physician.pk,))}?date={on_date:%Y-%m-%d}",
        )
        for physician in finding.physicians
    )
    source_links = tuple(_finding_source_link(source) for source in finding.sources)
    return FindingPresentation(
        finding.message,
        finding.severity,
        physicians + source_links,
        physicians,
        source_links,
    )


def _finding_source_link(source):
    view_name = {
        "faculty_template": "physician-faculty-availability-correct",
        "monthly_availability_rule": "physician-monthly-availability-rule-correct",
        "community_preceptor_rule": "community-preceptor-rule-correct",
        "vacation": "vacation-correct",
        "academic_event": "academic-event-correct",
        "normal_rounder": "normal-rounder-correct",
        "casual_rounder_rule": "casual-rounder-rule-correct",
    }[source.source_type]
    args = (
        (source.record.physician_id, source.source_id)
        if source.source_type in {"faculty_template", "monthly_availability_rule"}
        else (source.source_id,)
    )
    return FindingLinkPresentation(source.label, reverse(view_name, args=args))


@login_required
def late_doc_edit(request, on_date):
    on_date = _monday(on_date)
    candidates = late_doc_assignment_candidates_on(on_date)
    current_findings = _late_doc_editor_findings(on_date)
    cleanup_closure = holiday_closure_for(on_date, "PM") if is_operational_date(on_date) else None
    if cleanup_closure:
        assignment = late_doc_assignment_for_date(on_date)
        if assignment is None:
            raise Http404(
                "This clinic holiday closure has no retained Late Doc assignment to remove."
            )
        if request.method == "POST":
            return HttpResponseBadRequest(
                f"{cleanup_closure.name} closes this {cleanup_closure.coverage_label} clinic shift."
            )
        return render(
            request,
            "schedules/late_doc_form.html",
            {
                **_late_doc_workspace_context(on_date, request),
                "on_date": on_date,
                "candidates": candidates,
                "assignment": assignment,
                "cleanup_closure": cleanup_closure,
                "remove_form": LateDocRemovalForm(),
                "return_url": _return_url(request, on_date),
                "current_findings": current_findings,
            },
        )
    form = LateDocAssignmentForm(
        request.POST or None,
        candidates=candidates,
        initial={"physician": candidates.current.physician.pk if candidates.current else None},
    )
    if request.method == "POST" and form.is_valid():
        try:
            assign_late_doc(
                on_date=on_date,
                physician=form.cleaned_data["physician"],
                scheduler=request.user,
                note=form.cleaned_data["note"],
                override=form.cleaned_data["override"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_return_url(request, on_date))
    return render(
        request,
        "schedules/late_doc_form.html",
        {
            **_late_doc_workspace_context(on_date, request),
            "on_date": on_date,
            "candidates": candidates,
            "assignment": candidates.current.assignment if candidates.current else None,
            "form": form,
            "remove_form": LateDocRemovalForm(),
            "return_url": _return_url(request, on_date),
            "current_findings": current_findings,
        },
    )


@login_required
def late_doc_remove(request, on_date):
    on_date = _monday(on_date)
    if request.method != "POST":
        return redirect(_late_doc_workspace_context(on_date, request)["form_action"])
    form = LateDocRemovalForm(request.POST)
    assignment = late_doc_assignment_for_date(on_date)
    if assignment is None:
        raise Http404("No Late Doc assignment is recorded for this Monday.")
    if form.is_valid():
        remove_late_doc_assignment(
            assignment=assignment, scheduler=request.user, note=form.cleaned_data["note"]
        )
        return redirect(_return_url(request, on_date))
    candidates = late_doc_assignment_candidates_on(on_date)
    return render(
        request,
        "schedules/late_doc_form.html",
        {
            **_late_doc_workspace_context(on_date, request),
            "on_date": on_date,
            "candidates": candidates,
            "assignment": assignment,
            "form": LateDocAssignmentForm(candidates=candidates),
            "remove_form": form,
            "return_url": _return_url(request, on_date),
            "current_findings": _late_doc_editor_findings(on_date),
        },
    )


@login_required
def late_doc_history(request, on_date):
    on_date = _monday(on_date)
    return render(
        request,
        "schedules/late_doc_history.html",
        {
            **_late_doc_workspace_context(on_date, request),
            "on_date": on_date,
            "changes": late_doc_assignment_changes().filter(date=on_date),
            "return_url": _return_url(request, on_date),
        },
    )
