"""Community Preceptor rule HTTP workflows and presentation."""

import calendar

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format

from ..calendar_presentation import (
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarWeek,
    CommunityOccurrencePresentation,
    CommunityPresentation,
)
from ..forms.community_preceptors import CommunityPreceptorRemovalForm, CommunityPreceptorRuleForm
from ..models.community_preceptors import CommunityPreceptorRule, CommunityPreceptorScheduleChange
from ..models.physicians import EffectiveRole, Physician
from ..physician_scope import physician_scope_ids
from ..services.community import (
    community_preceptor_occurrences,
    correct_community_preceptor_rule,
    create_community_preceptor_rule,
    remove_community_preceptor_rule,
)
from ..services.holidays import holiday_closure_for
from ..services.operational_attention import organization_today
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from ._shared import _requested_month, _shift_month


def _community_rule_form_context(*, form, current_rule=None, **context):
    if current_rule:
        recurrence_rules = CommunityPreceptorRule.objects.filter(physician=current_rule.physician)
    else:
        physicians = (
            Physician.objects.filter(
                roles__role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                roles__effective_start__lte=organization_today(),
            )
            .filter(
                Q(roles__effective_end__isnull=True)
                | Q(roles__effective_end__gte=organization_today())
            )
            .order_by(Lower("display_name"), "display_name", "pk")
            .distinct()
        )
        form.fields["physician"].queryset = physicians
        recurrence_rules = CommunityPreceptorRule.objects.filter(physician__in=physicians)
    recurrence_rules = tuple(recurrence_rules.select_related("physician"))
    return {
        "form": form,
        "current_rule": current_rule,
        "fixed_physician": None,
        "recurrence_rules": recurrence_rules,
        "recurrence_rules_show_coverage": True,
        "show_recurrence_rule_context": True,
        "recurrence_rule_context_table": _recurrence_rule_context_table(
            recurrence_rules, current_rule=current_rule, show_coverage=True
        ),
        **context,
    }


def _rule_values(rule):
    return {
        "physician": rule.physician_id,
        "weekday": rule.weekday,
        "ordinals": [str(value) for value in rule.ordinals],
        "periods": list(rule.periods),
        "effective_start": rule.effective_start,
        "effective_end": rule.effective_end,
    }


@login_required
def community_preceptor_workspace(request):
    month = _community_requested_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    return render(
        request,
        "schedules/community_preceptor_workspace.html",
        _community_workspace_context(month, "calendar", physician_scope_ids(request)),
    )


@login_required
def community_preceptor_list(request):
    month = _community_requested_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    return render(
        request,
        "schedules/community_preceptor_list.html",
        _community_workspace_context(month, "list", physician_scope_ids(request)),
    )


@login_required
def community_preceptor_rules(request):
    month = _community_requested_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    return render(
        request,
        "schedules/community_preceptor_rules.html",
        _community_workspace_context(month, "rules", physician_scope_ids(request)),
    )


@login_required
def community_preceptor_rule_create(request):
    return_url = _community_preceptor_return_url(request)
    form = CommunityPreceptorRuleForm(
        request.POST or None, initial={"effective_start": organization_today()}
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_community_preceptor_rule(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(return_url)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        _community_rule_form_context(
            form=form, title="Add Community Preceptor rule", return_url=return_url
        ),
    )


@login_required
def community_preceptor_rule_correct(request, rule_id):
    rule = get_object_or_404(CommunityPreceptorRule, pk=rule_id)
    return_url = _community_preceptor_return_url(request)
    form = CommunityPreceptorRuleForm(request.POST or None, initial=_rule_values(rule))
    if request.method == "POST" and form.is_valid():
        try:
            correct_community_preceptor_rule(rule=rule, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(return_url)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        _community_rule_form_context(
            form=form,
            current_rule=rule,
            rule=rule,
            title="Edit Community Preceptor rule",
            return_url=return_url,
            remove_action=_community_preceptor_action_url(
                "community-preceptor-rule-remove", request, rule.pk
            ),
        ),
    )


@login_required
def community_preceptor_rule_remove(request, rule_id):
    rule = get_object_or_404(CommunityPreceptorRule, pk=rule_id)
    return_url = _community_preceptor_return_url(request)
    if request.method != "POST":
        return redirect(
            _community_preceptor_action_url("community-preceptor-rule-correct", request, rule.pk)
        )
    form = CommunityPreceptorRemovalForm(request.POST)
    if form.is_valid():
        remove_community_preceptor_rule(
            rule=rule, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect(return_url)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        {
            "form": CommunityPreceptorRuleForm(initial=_rule_values(rule)),
            "remove_form": form,
            "title": "Remove Community Preceptor rule",
            "rule": rule,
            "return_url": return_url,
            "remove_action": _community_preceptor_action_url(
                "community-preceptor-rule-remove", request, rule.pk
            ),
        },
    )


@login_required
def community_preceptor_history(request):
    return render(
        request,
        "schedules/community_preceptor_history.html",
        {
            "community_preceptor_history_table": _community_preceptor_history_table(
                CommunityPreceptorScheduleChange.objects.select_related(
                    "community_preceptor_rule", "physician", "changed_by"
                )
            ),
            "return_url": _community_preceptor_return_url(request),
        },
    )


def _community_preceptor_history_table(changes):
    return DataTable(
        identity="community-preceptor-history",
        label="Community Preceptor Schedule history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Physician",
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
                    DataTableCell(change.physician.display_name, row_header=True),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No Community Preceptor Schedule changes recorded.",
    )


def _community_requested_month(request):
    value = request.GET.get("month")
    month = _requested_month(value)
    return month if not value or month is not None else None


def _community_workspace_context(month, view, scope_ids=()):
    rules = (
        CommunityPreceptorRule.objects.select_related("physician")
        .filter(**({"physician_id__in": scope_ids} if scope_ids else {}))
        .order_by(Lower("physician__display_name"), "physician__display_name", "pk")
    )
    context = {
        "schedule_workspace_page_title": "Community Precepting",
        "schedule_workspace_label": "Community Precepting",
        "schedule_workspace_navigation": tuple(
            {
                "label": label,
                "url": _community_workspace_url(target_view, month),
                "current": view == target_view,
            }
            for label, target_view in (
                ("Calendar", "calendar"),
                ("List", "list"),
                ("Rules", "rules"),
            )
        ),
        "community_preceptor_view": view,
        "community_preceptor_selected_month": month,
        "community_preceptor_history_url": _community_preceptor_action_url_for(
            "community-preceptor-history", view, month
        ),
        "community_preceptor_add_url": _community_preceptor_action_url_for(
            "community-preceptor-rule-create", view, month
        ),
        "community_preceptor_rules": rules,
        "community_preceptor_rules_table": _community_preceptor_rules_table(rules, month),
    }
    if view in {"calendar", "list"}:
        context.update(
            {
                "schedule_workspace_selected_month": month,
                "schedule_workspace_previous_month_url": _community_workspace_url(
                    view, _shift_month(month, -1)
                ),
                "schedule_workspace_next_month_url": _community_workspace_url(
                    view, _shift_month(month, 1)
                ),
                "schedule_workspace_month_picker_action": reverse(
                    "community-preceptor-workspace"
                    if view == "calendar"
                    else "community-preceptor-list"
                ),
                "schedule_workspace_month_picker_preserved_query": {},
            }
        )
    if view == "calendar":
        weeks = _community_calendar_weeks(month, scope_ids)
        context["community_preceptor_calendar_weeks"] = weeks
        context["calendar"] = _community_calendar_lattice(month, weeks)
    elif view == "list":
        occurrences = community_preceptor_occurrences(
            month, month.replace(day=calendar.monthrange(month.year, month.month)[1])
        )
        occurrences = tuple(
            occurrence
            for occurrence in occurrences
            if not scope_ids or occurrence.physician_id in scope_ids
        )
        context["community_preceptor_occurrences"] = occurrences
        context["community_preceptor_list_table"] = _community_preceptor_list_table(
            occurrences, month
        )
    return context


def _community_preceptor_rules_table(rules, month):
    return DataTable(
        identity="community-preceptor-rules",
        label="Community Preceptor rules",
        width="compact",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Physician",
                "Weekday",
                "Occurrences",
                "Periods",
                "Effective range",
                "Actions",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        rule.physician.display_name,
                        kind="link",
                        url=reverse("physician-detail", args=(rule.physician_id,)),
                        row_header=True,
                    ),
                    DataTableCell(rule.get_weekday_display()),
                    DataTableCell(rule.ordinal_display),
                    DataTableCell(rule.periods_display),
                    DataTableCell(_effective_range(rule.effective_start, rule.effective_end)),
                    DataTableCell(
                        "",
                        kind="links",
                        links=(
                            DataTableLink(
                                "Edit",
                                f"{reverse('community-preceptor-rule-correct', args=(rule.pk,))}?view=rules&month={month:%Y-%m}",
                            ),
                            DataTableLink(
                                "Remove",
                                f"{reverse('community-preceptor-rule-remove', args=(rule.pk,))}?view=rules&month={month:%Y-%m}",
                            ),
                        ),
                    ),
                )
            )
            for rule in rules
        ),
        empty_state="No Community Preceptor rules recorded.",
    )


def _community_preceptor_list_table(occurrences, month):
    return DataTable(
        identity="community-preceptor-list",
        label="Projected Community Preceptor commitments",
        width="compact",
        columns=tuple(DataTableColumn(label) for label in ("Date", "Coverage", "Physician")),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        date_format(occurrence.date),
                        kind="date",
                        machine_value=occurrence.date.isoformat(),
                        row_header=True,
                    ),
                    DataTableCell(occurrence.period),
                    DataTableCell(
                        occurrence.physician_display_name,
                        kind="link",
                        url=f"{reverse('community-preceptor-rule-correct', args=(occurrence.rule_id,))}?view=list&month={month:%Y-%m}",
                    ),
                )
            )
            for occurrence in occurrences
        ),
        empty_state=f"No projected Community Preceptor commitments for {month:%B %Y}.",
    )


def _recurrence_rule_context_table(rules, *, current_rule, show_coverage):
    columns = [
        DataTableColumn("Physician"),
        DataTableColumn("Weekday"),
        DataTableColumn("Recurrence"),
    ]
    if show_coverage:
        columns.append(DataTableColumn("Coverage"))
    columns.extend((DataTableColumn("Effective range"), DataTableColumn("Context")))
    return DataTable(
        identity="community-preceptor-rule-context",
        label="Existing Community Preceptor rules",
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    [
                        DataTableCell(rule.physician.display_name, row_header=True),
                        DataTableCell(rule.get_weekday_display()),
                        DataTableCell(rule.ordinal_display),
                    ]
                    + ([DataTableCell(rule.periods_display)] if show_coverage else [])
                    + [
                        DataTableCell(_effective_range(rule.effective_start, rule.effective_end)),
                        DataTableCell(
                            "Editing this rule"
                            if current_rule and rule.pk == current_rule.pk
                            else "Retained rule",
                            kind="status"
                            if current_rule and rule.pk == current_rule.pk
                            else "metadata",
                            tone="attention" if current_rule and rule.pk == current_rule.pk else "",
                        ),
                    ]
                ),
                tone="attention" if current_rule and rule.pk == current_rule.pk else "",
            )
            for rule in rules
        ),
        empty_state="No retained rules for this physician.",
    )


def _effective_range(start, end):
    return (
        f"{date_format(start, 'N j, Y') if start else 'Unbounded past'} through "
        f"{date_format(end, 'N j, Y') if end else 'Unbounded future'}"
    )


def _community_calendar_weeks(month, scope_ids=()):
    displayed_weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    occurrences = community_preceptor_occurrences(displayed_weeks[0][0], displayed_weeks[-1][-1])
    if scope_ids:
        occurrences = tuple(
            occurrence for occurrence in occurrences if occurrence.physician_id in scope_ids
        )
    occurrences_by_date = {}
    for occurrence in occurrences:
        occurrences_by_date.setdefault(occurrence.date, []).append(occurrence)
    return tuple(
        {
            "week_label": (
                f"{week_dates[0].strftime('%B')} {week_dates[0].day} through "
                f"{week_dates[-1].strftime('%B')} {week_dates[-1].day}, {week_dates[-1].year}"
            ),
            "days": tuple(
                {
                    "date": day,
                    "date_label": f"{day.strftime('%b')} {day.day}",
                    "in_selected_month": day.year == month.year and day.month == month.month,
                    "occurrences": tuple(occurrences_by_date.get(day, ())),
                }
                for day in week_dates
            ),
        }
        for week_dates in displayed_weeks
    )


def _community_calendar_lattice(month, weeks):
    return CalendarLattice(
        label=f"{month:%B %Y} Community Precepting calendar",
        columns=("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
        variant="seven-column",
        has_gutter=True,
        weeks=tuple(
            CalendarWeek(
                label=week["week_label"],
                days=tuple(
                    CalendarDay(
                        day["date"].isoformat(), day["date_label"], day["in_selected_month"]
                    )
                    for day in week["days"]
                ),
                lanes=tuple(
                    CalendarLane(
                        period,
                        tuple(
                            CalendarItem(
                                index,
                                1,
                                "schedules/calendar_community_item.html",
                                CommunityPresentation(
                                    tuple(
                                        CommunityOccurrencePresentation(
                                            occurrence.physician_display_name,
                                            occurrence.period,
                                            f"{reverse('community-preceptor-rule-correct', args=(occurrence.rule_id,))}"
                                            f"?view=calendar&month={month:%Y-%m}",
                                        )
                                        for occurrence in day["occurrences"]
                                        if occurrence.period == period
                                    ),
                                    closed_label=(
                                        "Closed" if holiday_closure_for(day["date"], period) else ""
                                    ),
                                    accessible_label=(
                                        f"Community Precepting {period} for "
                                        f"{day['date']:%A, %B} {day['date'].day}, "
                                        f"{day['date'].year}: Closed for "
                                        f"{holiday_closure_for(day['date'], period).name}, "
                                        f"{holiday_closure_for(day['date'], period).coverage_label}."
                                        if holiday_closure_for(day["date"], period)
                                        else ""
                                    ),
                                ),
                            )
                            for index, day in enumerate(week["days"], start=1)
                            if any(occurrence.period == period for occurrence in day["occurrences"])
                            or holiday_closure_for(day["date"], period)
                        ),
                        period=period,
                    )
                    for period in ("AM", "PM")
                ),
            )
            for week in weeks
        ),
    )


def _community_workspace_url(view, month):
    view_name = {
        "calendar": "community-preceptor-workspace",
        "list": "community-preceptor-list",
        "rules": "community-preceptor-rules",
    }[view]
    return f"{reverse(view_name)}?month={month:%Y-%m}"


def _community_preceptor_return_url(request):
    view = request.GET.get("view", "calendar")
    if view not in {"calendar", "list", "rules"}:
        view = "calendar"
    month = _community_requested_month(request)
    if request.GET.get("month") and month:
        return _community_workspace_url(view, month)
    return reverse(
        {
            "calendar": "community-preceptor-workspace",
            "list": "community-preceptor-list",
            "rules": "community-preceptor-rules",
        }[view]
    )


def _community_preceptor_action_url(view_name, request, *args):
    view = request.GET.get("view", "calendar")
    if view not in {"calendar", "list", "rules"}:
        view = "calendar"
    month = _community_requested_month(request)
    return _community_preceptor_action_url_for(view_name, view, month, *args)


def _community_preceptor_action_url_for(view_name, view, month, *args):
    url = reverse(view_name, args=args)
    if month:
        return f"{url}?view={view}&month={month:%Y-%m}"
    return url
