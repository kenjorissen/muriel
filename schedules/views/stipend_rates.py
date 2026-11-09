"""Stipend-rate inspection and administrator-only maintenance."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format

from ..forms.stipend_rates import (
    StipendRateCorrectionForm,
    StipendRateRemovalForm,
    StipendRateTransitionForm,
    StipendRateValueForm,
)
from ..models.stipend_rates import StipendRate
from ..services._mutation import (
    can_manage_scheduling_administration,
    require_scheduling_administrator,
)
from ..services.stipend_rates import (
    append_stipend_rate,
    correct_stipend_rate,
    remove_stipend_rate,
    set_initial_stipend_rate,
    stipend_rate_changes,
    stipend_rates,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from .reports import reports_workspace_context


def scheduling_administration_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        require_scheduling_administrator(request.user)
        return view(request, *args, **kwargs)

    return wrapped


def _timeline_context(request):
    community_preceptor_rates = tuple(stipend_rates(StipendRate.Kind.COMMUNITY_PRECEPTOR))
    late_doc_rates = tuple(stipend_rates(StipendRate.Kind.LATE_DOC))
    can_manage = can_manage_scheduling_administration(request.user)
    return {
        **reports_workspace_context(request, view="rates"),
        "community_preceptor_rates": community_preceptor_rates,
        "late_doc_rates": late_doc_rates,
        "can_manage_stipend_rates": can_manage,
        "community_preceptor_rates_table": _stipend_rate_table(
            "community-preceptor-rates",
            "Community Preceptor rates",
            community_preceptor_rates,
            can_manage,
        ),
        "late_doc_rates_table": _stipend_rate_table(
            "late-doc-rates", "Late Doc rates", late_doc_rates, can_manage
        ),
    }


def _stipend_rate_table(identity, label, rates, can_manage):
    columns = [
        DataTableColumn("Hourly rate"),
        DataTableColumn("Effective start"),
        DataTableColumn("Effective end"),
    ]
    if can_manage:
        columns.append(DataTableColumn("Actions"))
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    [
                        DataTableCell(f"${rate.hourly_rate}", row_header=True),
                        DataTableCell(
                            date_format(rate.effective_start)
                            if rate.effective_start
                            else "Unbounded"
                        ),
                        DataTableCell(
                            date_format(rate.effective_end) if rate.effective_end else "Ongoing"
                        ),
                    ]
                    + (
                        [
                            DataTableCell(
                                "",
                                kind="links",
                                links=(
                                    DataTableLink(
                                        "Edit"
                                        if len(rates) > 1
                                        else "Edit (At least one rate must remain.)",
                                        reverse("stipend-rate-correct", args=(rate.pk,)),
                                    ),
                                    *(
                                        (
                                            DataTableLink(
                                                "Remove",
                                                reverse("stipend-rate-remove", args=(rate.pk,)),
                                            ),
                                        )
                                        if len(rates) > 1
                                        else ()
                                    ),
                                ),
                            )
                        ]
                        if can_manage
                        else []
                    )
                )
            )
            for rate in rates
        ),
        empty_state=f"No {label.lower()} configured.",
    )


@login_required
def stipend_rate_workspace(request):
    return render(request, "schedules/stipend_rate_workspace.html", _timeline_context(request))


@login_required
@scheduling_administration_required
def stipend_rate_initial(request, kind):
    kind = _stipend_kind(kind)
    form = StipendRateValueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            set_initial_stipend_rate(kind=kind, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("stipend-rate-workspace")
    return render(
        request,
        "schedules/stipend_rate_form.html",
        {
            "form": form,
            "title": f"Set initial {StipendRate.Kind(kind).label} rate",
            "submit_label": "Set initial rate",
        },
    )


@login_required
@scheduling_administration_required
def stipend_rate_append(request, kind, direction):
    kind = _stipend_kind(kind)
    if direction not in {"earlier", "later"}:
        raise ValidationError("Choose whether to add the rate earlier or later.")
    form = StipendRateTransitionForm(request.POST or None)
    form.fields["transition_date"].help_text = (
        "The new rate ends on this date."
        if direction == "earlier"
        else "The new rate begins on this date."
    )
    if request.method == "POST" and form.is_valid():
        try:
            append_stipend_rate(
                kind=kind, direction=direction, scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("stipend-rate-workspace")
    return render(
        request,
        "schedules/stipend_rate_form.html",
        {
            "form": form,
            "title": f"Add {direction} {StipendRate.Kind(kind).label} rate",
            "submit_label": f"Add {direction} rate",
        },
    )


@login_required
@scheduling_administration_required
def stipend_rate_correct(request, rate_id):
    rate = get_object_or_404(StipendRate, pk=rate_id)
    form = StipendRateCorrectionForm(
        request.POST or None,
        rate=rate,
        initial={
            "hourly_rate": rate.hourly_rate,
            "effective_start": rate.effective_start,
            "effective_end": rate.effective_end,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_stipend_rate(rate=rate, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("stipend-rate-workspace")
    return render(
        request,
        "schedules/stipend_rate_form.html",
        {
            "form": form,
            "title": f"Edit {rate.get_kind_display()} rate",
            "rate": rate,
            "submit_label": "Save changes",
        },
    )


@login_required
@scheduling_administration_required
def stipend_rate_remove(request, rate_id):
    rate = get_object_or_404(StipendRate, pk=rate_id, retired_at__isnull=True)
    rates = list(stipend_rates(rate.kind))
    index = rates.index(rate)
    previous = rates[index - 1] if index else None
    following = rates[index + 1] if index + 1 < len(rates) else None
    can_remove = len(rates) > 1
    form = StipendRateRemovalForm(request.POST or None, previous=previous, following=following)
    if request.method == "POST" and form.is_valid():
        try:
            remove_stipend_rate(
                rate=rate,
                absorber=form.cleaned_data.get("absorber"),
                scheduler=request.user,
                note=form.cleaned_data["note"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("stipend-rate-workspace")
    return render(
        request,
        "schedules/stipend_rate_remove.html",
        {
            "form": form,
            "rate": rate,
            "previous": previous,
            "following": following,
            "can_remove": can_remove,
        },
    )


@login_required
def stipend_rate_history(request):
    return render(
        request,
        "schedules/stipend_rate_history.html",
        {
            "stipend_rate_history_table": _stipend_rate_history_table(stipend_rate_changes()),
            "can_manage_stipend_rates": can_manage_scheduling_administration(request.user),
        },
    )


def _stipend_rate_history_table(changes):
    return DataTable(
        identity="stipend-rate-history",
        label="Stipend rate history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Stipend type",
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
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.rate.get_kind_display(), row_header=True),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.note or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No stipend rate changes recorded.",
    )


def _stipend_kind(kind):
    if kind not in StipendRate.Kind.values:
        raise ValidationError("Unknown stipend type.")
    return kind
