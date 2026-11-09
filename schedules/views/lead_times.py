"""Lead-time policy inspection and administrator-only maintenance."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from ..forms.lead_times import LeadTimeForm
from ..models.lead_times import LeadTimeSetting
from ..services._mutation import (
    can_manage_scheduling_administration,
    require_scheduling_administrator,
)
from ..services.lead_times import lead_time_changes, lead_time_policy, set_lead_time
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
    history_timestamp,
)
from .work_remaining import schedule_navigation

_LEAD_TIME_ANCHORS = {
    LeadTimeSetting.Family.HOLIDAYS: "Before the holiday's nominal date.",
    LeadTimeSetting.Family.ACADEMIC_EVENTS: "Before the expected academic-year quarter.",
    LeadTimeSetting.Family.PRECEPTING: "Before the affected week.",
    LeadTimeSetting.Family.NORMAL_ROUNDING: "Before the affected week.",
    LeadTimeSetting.Family.INTERVIEWS: "Before July 1 of the interview academic year.",
}


def scheduling_administration_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        require_scheduling_administrator(request.user)
        return view(request, *args, **kwargs)

    return wrapped


@login_required
def lead_time_workspace(request):
    policy = lead_time_policy()
    can_manage = can_manage_scheduling_administration(request.user)
    return render(
        request,
        "schedules/lead_time_workspace.html",
        {
            "schedule_workspace_page_title": "Lead Times",
            "schedule_workspace_label": "Schedule",
            "schedule_workspace_navigation": schedule_navigation("lead-times"),
            "can_manage_lead_times": can_manage,
            "lead_time_table": _lead_time_table(policy, can_manage),
        },
    )


@login_required
@scheduling_administration_required
def lead_time_edit(request, family):
    family = _lead_time_family(family)
    months = lead_time_policy()[family]
    form = LeadTimeForm(request.POST or None, initial={"months": months})
    if request.method == "POST" and form.is_valid():
        try:
            set_lead_time(family=family, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("lead-time-workspace")
    return render(
        request,
        "schedules/lead_time_form.html",
        {
            "form": form,
            "family": LeadTimeSetting.Family(family),
            "anchor": _LEAD_TIME_ANCHORS[family],
        },
    )


@login_required
def lead_time_history(request):
    return render(
        request,
        "schedules/lead_time_history.html",
        {"lead_time_history_table": _lead_time_history_table(lead_time_changes())},
    )


def _lead_time_table(policy, can_manage):
    columns = [
        DataTableColumn("Work family"),
        DataTableColumn("Lead time"),
        DataTableColumn("Fixed anchor"),
    ]
    if can_manage:
        columns.append(DataTableColumn("Actions"))
    return DataTable(
        identity="lead-times",
        label="Lead-time policies",
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    [
                        DataTableCell(LeadTimeSetting.Family(family).label, row_header=True),
                        DataTableCell(f"{months} months"),
                        DataTableCell(_LEAD_TIME_ANCHORS[family]),
                    ]
                    + (
                        [
                            DataTableCell(
                                "",
                                kind="links",
                                links=(
                                    DataTableLink(
                                        "Edit", reverse("lead-time-edit", args=(family,))
                                    ),
                                ),
                            )
                        ]
                        if can_manage
                        else []
                    )
                )
            )
            for family, months in policy.items()
        ),
        empty_state="No lead-time policies are configured.",
    )


def _lead_time_history_table(changes):
    return DataTable(
        identity="lead-time-history",
        label="Lead-time history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Work family",
                "Prior value",
                "Resulting value",
                "Note",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.get_family_display(), row_header=True),
                    DataTableCell(f"{change.previous_months} months"),
                    DataTableCell(f"{change.resulting_months} months"),
                    DataTableCell(change.note or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No lead-time changes recorded.",
    )


def _lead_time_family(family):
    try:
        return LeadTimeSetting.Family(family)
    except ValueError as error:
        raise Http404("Unknown lead-time work family.") from error
