"""Rounding HTTP workflows and workspace presentation."""

from dataclasses import dataclass
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
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
    RoundingCasualOccurrencePresentation,
    RoundingCasualPresentation,
    RoundingNormalPresentation,
)
from ..forms.rounding import (
    CasualRounderRuleForm,
    NormalRounderForm,
    RoundingRemovalForm,
    RoundingWeekRangeForm,
)
from ..models.physicians import EffectiveRole, Physician
from ..models.rounding import CasualRounderRule, NormalRounder, RoundingScheduleChange
from ..physician_scope import physician_scope_ids
from ..services.operational_attention import organization_today
from ..services.rounding import (
    correct_casual_rounder_rule,
    create_casual_rounder_rule,
    focused_rounding_month_projection,
    normal_rounder_statuses_for_weeks,
    record_normal_rounder,
    remove_casual_rounder_rule,
    remove_normal_rounder,
    rounding_week_has_focused_membership,
    rounding_week_start,
    rounding_weeks,
)
from ..services.work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    focused_work_remaining,
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
from ._shared import _requested_month, _shift_month


@dataclass(frozen=True)
class RoundingNormalRowPresentation:
    """Table tone for one normal-rounder week in either Rounding source view."""

    table_tone: str = ""

    @property
    def table_row_class(self):
        return {"danger": "danger-row", "attention": "incomplete"}.get(self.table_tone, "")


def _rounding_normal_row_presentation(status, *, focused_membership=True):
    if not focused_membership:
        return RoundingNormalRowPresentation()
    if status.normal_rounder is None:
        return RoundingNormalRowPresentation("danger")
    if status.inconsistent:
        return RoundingNormalRowPresentation("attention")
    return RoundingNormalRowPresentation()


def _casual_rule_form_context(*, form, current_rule=None, fixed_physician=None, **context):
    physician = fixed_physician or (current_rule.physician if current_rule else None)
    recurrence_rules = CasualRounderRule.objects.filter(physician=physician).order_by(
        "weekday", "effective_start", "pk"
    )
    recurrence_rules = tuple(recurrence_rules.select_related("physician"))
    return {
        "form": form,
        "current_rule": current_rule,
        "fixed_physician": fixed_physician,
        "recurrence_rules": recurrence_rules,
        "recurrence_rules_show_coverage": False,
        "show_recurrence_rule_context": True,
        "recurrence_rule_edit_url_name": "casual-rounder-rule-correct",
        "recurrence_rule_context_table": _casual_rule_context_table(
            recurrence_rules,
            current_rule=current_rule,
            fixed_physician=fixed_physician,
            edit_query=context.get("recurrence_rule_edit_query", ""),
        ),
        **context,
    }


def _current_casual_rounders():
    today = organization_today()
    current_rules = CasualRounderRule.objects.filter(
        Q(effective_start__isnull=True) | Q(effective_start__lte=today)
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
    return (
        Physician.objects.filter(
            roles__role=EffectiveRole.Role.CASUAL_ROUNDER,
            roles__effective_start__lte=today,
        )
        .filter(Q(roles__effective_end__isnull=True) | Q(roles__effective_end__gte=today))
        .order_by(Lower("display_name"), "display_name", "pk")
        .distinct()
        .prefetch_related(
            Prefetch(
                "casual_rounder_rules",
                queryset=current_rules.order_by("weekday", "pk"),
                to_attr="current_casual_rules",
            )
        )
    )


def _casual_rounder_roster(request, rounding_context=None):
    roster = list(_current_casual_rounders())
    scope_ids = physician_scope_ids(request)
    if scope_ids:
        roster = [physician for physician in roster if physician.pk in scope_ids]
    context = rounding_context or _rounding_return_context(request)
    for physician in roster:
        physician.casual_rule_add_url = (
            f"{reverse('casual-rounder-rule-create-for-physician', args=(physician.pk,))}?"
            f"{_rounding_return_query(context)}"
        )
    return roster


@login_required
def rounding_workspace(request):
    month = _requested_rounding_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    scope_ids = physician_scope_ids(request)
    projection = focused_rounding_month_projection(month)
    return render(
        request,
        "schedules/rounding_calendar.html",
        _rounding_workspace_context(
            rounding_view="calendar",
            month=month,
            projection=projection,
            scope_ids=scope_ids,
        ),
    )


@login_required
def rounding_list(request):
    month = _requested_rounding_month(request)
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    scope_ids = physician_scope_ids(request)
    projection = focused_rounding_month_projection(month)
    return render(
        request,
        "schedules/rounding_list.html",
        _rounding_workspace_context(
            rounding_view="list",
            month=month,
            projection=projection,
            scope_ids=scope_ids,
        ),
    )


@login_required
def normal_rounder_workspace(request):
    today = organization_today()
    default_start_date = rounding_week_start(today)
    form = RoundingWeekRangeForm(
        request.GET
        if request.GET
        else {
            "start_date": default_start_date,
            "end_date": _twelve_month_horizon_end(default_start_date),
        }
    )
    if not form.is_valid():
        return HttpResponseBadRequest(
            "Rounding Schedule range needs valid inclusive start and end dates."
        )
    weeks = rounding_weeks(form.cleaned_data["start_date"], form.cleaned_data["end_date"])
    statuses = {status.week: status for status in normal_rounder_statuses_for_weeks(weeks)}
    scope_ids = physician_scope_ids(request)
    selected_range = InclusiveDateRange(
        form.cleaned_data["start_date"], form.cleaned_data["end_date"]
    )
    rounding_week_rows = tuple(
        {
            "week": week,
            "normal_rounder": statuses[week].normal_rounder,
            "normal_rounder_status": statuses[week],
            "focused_membership": rounding_week_has_focused_membership(week, selected_range),
            "presentation": _rounding_normal_row_presentation(
                statuses[week],
                focused_membership=rounding_week_has_focused_membership(week, selected_range),
            ),
            "scope_display": _normal_rounder_scope_display(statuses[week], scope_ids),
        }
        for week in weeks
    )
    rounding_context = {
        "view": "normal",
        "month": None,
        "start_date": form.cleaned_data["start_date"],
        "end_date": form.cleaned_data["end_date"],
    }
    return render(
        request,
        "schedules/normal_rounder_workspace.html",
        {
            **_rounding_workspace_context(
                rounding_view="normal",
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
            ),
            "form": form,
            "rounding_weeks": rounding_week_rows,
            "work_remaining": _normal_rounding_work_remaining(
                rounding_week_rows, selected_range, rounding_context
            ),
            "normal_rounder_table": _normal_rounder_table(rounding_week_rows, rounding_context),
        },
    )


@login_required
def casual_rounder_workspace(request):
    casual_rounders = _casual_rounder_roster(
        request,
        {"view": "casual", "month": None, "start_date": None, "end_date": None},
    )
    return render(
        request,
        "schedules/casual_rounder_workspace.html",
        {
            **_rounding_workspace_context(rounding_view="casual"),
            "casual_rounders": casual_rounders,
            "casual_rounder_roster_table": _casual_rounder_roster_table(casual_rounders),
        },
    )


@login_required
def normal_rounder_record(request):
    week = _rounding_prefilled_week(request.GET.get("week"))
    if not week:
        return redirect(reverse("normal-rounder-workspace"))
    if assignment := NormalRounder.objects.filter(week=week).first():
        return redirect(_rounding_action_url("normal-rounder-correct", request, assignment.pk))
    add_next_context = _normal_rounder_add_next_context(request, week)
    form = NormalRounderForm(
        request.POST or None,
        fixed_week=week,
    )
    if request.method == "POST" and form.is_valid():
        try:
            record_normal_rounder(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if request.POST.get("save_and_add_next") and add_next_context:
                if next_week := _next_unassigned_normal_rounding_week(add_next_context, week):
                    return redirect(
                        f"{reverse('normal-rounder-record')}?"
                        f"{_rounding_return_query(add_next_context, week=next_week)}"
                    )
                messages.success(
                    request,
                    "Normal rounder saved. No unassigned weeks remain in this range.",
                )
            return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request,
            form=form,
            title="Record normal rounder",
            fixed_week=week,
            show_save_and_add_next=bool(add_next_context),
        ),
    )


@login_required
def normal_rounder_correct(request, normal_rounder_id):
    assignment = get_object_or_404(NormalRounder, pk=normal_rounder_id)
    form = NormalRounderForm(
        request.POST or None,
        initial={"week": assignment.week, "physician": assignment.physician_id},
    )
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["week"] != assignment.week:
            form.add_error("week", "A correction cannot move a normal rounder to a different week.")
        else:
            try:
                record_normal_rounder(scheduler=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request, form=form, title="Edit normal rounder", normal_rounder=assignment
        ),
    )


@login_required
def normal_rounder_remove(request, normal_rounder_id):
    assignment = get_object_or_404(NormalRounder, pk=normal_rounder_id)
    if request.method != "POST":
        return redirect(_rounding_action_url("normal-rounder-correct", request, assignment.pk))
    form = RoundingRemovalForm(request.POST)
    if form.is_valid():
        remove_normal_rounder(
            normal_rounder=assignment, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request,
            form=NormalRounderForm(
                initial={"week": assignment.week, "physician": assignment.physician_id}
            ),
            remove_form=form,
            title="Remove normal rounder",
            normal_rounder=assignment,
        ),
    )


def _casual_rule_values(rule):
    return {
        "physician": rule.physician_id,
        "weekday": rule.weekday,
        "ordinals": [str(value) for value in rule.ordinals],
        "effective_start": rule.effective_start,
        "effective_end": rule.effective_end,
    }


@login_required
def casual_rounder_rule_create(request):
    casual_rounders = _casual_rounder_roster(request)
    return render(
        request,
        "schedules/casual_rounder_rule_select.html",
        _rounding_form_context(
            request,
            title="Add casual rounder rule",
            casual_rounders=casual_rounders,
            casual_rounder_roster_table=_casual_rounder_roster_table(casual_rounders),
        ),
    )


@login_required
def casual_rounder_rule_create_for_physician(request, physician_id):
    physician = get_object_or_404(_current_casual_rounders(), pk=physician_id)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        form_data["physician"] = physician.pk
    form = CasualRounderRuleForm(
        form_data,
        initial={"physician": physician.pk, "effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_casual_rounder_rule(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request,
            **_casual_rule_form_context(
                form=form,
                fixed_physician=physician,
                title="Add casual rounder rule",
                recurrence_rule_edit_query=_rounding_return_query(
                    _rounding_return_context(request)
                ),
            ),
        ),
    )


@login_required
def casual_rounder_rule_correct(request, rule_id):
    rule = get_object_or_404(CasualRounderRule, pk=rule_id)
    form = CasualRounderRuleForm(request.POST or None, initial=_casual_rule_values(rule))
    if request.method == "POST" and form.is_valid():
        try:
            correct_casual_rounder_rule(rule=rule, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request,
            **_casual_rule_form_context(
                form=form,
                current_rule=rule,
                casual_rule=rule,
                title="Edit casual rounder rule",
                recurrence_rule_edit_query=_rounding_return_query(
                    _rounding_return_context(request)
                ),
            ),
        ),
    )


@login_required
def casual_rounder_rule_remove(request, rule_id):
    rule = get_object_or_404(CasualRounderRule, pk=rule_id)
    if request.method != "POST":
        return redirect(_rounding_action_url("casual-rounder-rule-correct", request, rule.pk))
    form = RoundingRemovalForm(request.POST)
    if form.is_valid():
        remove_casual_rounder_rule(
            rule=rule, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect(_rounding_redirect_url(request))
    return render(
        request,
        "schedules/rounding_form.html",
        _rounding_form_context(
            request,
            form=CasualRounderRuleForm(initial=_casual_rule_values(rule)),
            remove_form=form,
            title="Remove casual rounder rule",
            casual_rule=rule,
        ),
    )


@login_required
def rounding_history(request):
    changes = RoundingScheduleChange.objects.select_related("physician", "changed_by")
    return render(
        request,
        "schedules/rounding_history.html",
        {
            "rounding_history_table": _rounding_history_table(changes),
            "return_url": _rounding_redirect_url(request),
        },
    )


def _requested_rounding_month(request):
    value = request.GET.get("month")
    month = _requested_month(value)
    return None if value and (month is None or month.strftime("%Y-%m") != value) else month


def _rounding_workspace_url(view="calendar", month=None, start_date=None, end_date=None):
    url = reverse(
        {
            "calendar": "rounding-workspace",
            "list": "rounding-list",
            "normal": "normal-rounder-workspace",
            "casual": "casual-rounder-workspace",
        }[view]
    )
    if view in {"calendar", "list"} and month:
        return f"{url}?month={month:%Y-%m}"
    if view == "normal" and start_date and end_date:
        return f"{url}?start_date={start_date:%Y-%m-%d}&end_date={end_date:%Y-%m-%d}"
    return url


def _rounding_return_context(request):
    view = request.GET.get("view", "calendar")
    view = view if view in {"calendar", "list", "normal", "casual"} else "calendar"
    context = {
        "view": view,
        "month": _requested_rounding_month(request) if view in {"calendar", "list"} else None,
        "start_date": None,
        "end_date": None,
    }
    if view == "normal":
        form = RoundingWeekRangeForm(
            {"start_date": request.GET.get("start_date"), "end_date": request.GET.get("end_date")}
        )
        if form.is_valid():
            context.update(form.cleaned_data)
    return context


def _rounding_redirect_url(request):
    context = _rounding_return_context(request)
    return (
        _rounding_workspace_url(**context)
        if any(request.GET.get(key) for key in ("view", "month", "start_date", "end_date"))
        else reverse("rounding-workspace")
    )


def _rounding_action_url(view_name, request, *args, week=None):
    context = _rounding_return_context(request)
    return f"{reverse(view_name, args=args)}?{_rounding_return_query(context, week=week)}"


def _rounding_return_query(context, *, week=None):
    query = [f"view={context['view']}"]
    if context["month"]:
        query.append(f"month={context['month']:%Y-%m}")
    if context["start_date"] and context["end_date"]:
        query.extend(
            (
                f"start_date={context['start_date']:%Y-%m-%d}",
                f"end_date={context['end_date']:%Y-%m-%d}",
            )
        )
    if week:
        query.append(f"week={week:%Y-%m-%d}")
    return "&".join(query)


def _rounding_prefilled_week(value):
    try:
        week = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    return rounding_week_start(week) if week else None


def _normal_rounder_add_next_context(request, week):
    context = _rounding_return_context(request)
    if (
        context["view"] != "normal"
        or not context["start_date"]
        or week not in rounding_weeks(context["start_date"], context["end_date"])
    ):
        return None
    return context


def _next_unassigned_normal_rounding_week(context, after_week):
    weeks = tuple(
        week
        for week in rounding_weeks(context["start_date"], context["end_date"])
        if week > after_week
    )
    assigned_weeks = set(
        NormalRounder.objects.filter(week__in=weeks).values_list("week", flat=True)
    )
    return next((week for week in weeks if week not in assigned_weeks), None)


def _twelve_month_horizon_end(start_date):
    try:
        next_year = start_date.replace(year=start_date.year + 1)
    except ValueError:
        next_year = date(start_date.year + 1, 2, 28)
    return next_year - timedelta(days=1)


def _normal_rounder_scope_display(status, scope_ids):
    if status.normal_rounder is None:
        return ""
    if not scope_ids or status.normal_rounder.physician_id in scope_ids:
        return status.normal_rounder.physician.display_name
    return "Assigned outside focus"


def _rounding_workspace_context(
    *, rounding_view, month=None, start_date=None, end_date=None, projection=None, scope_ids=()
):
    action_query = "?" + _rounding_return_query(
        {
            "view": rounding_view,
            "month": month,
            "start_date": start_date,
            "end_date": end_date,
        }
    )
    (
        primary_action_url,
        primary_action_label,
        secondary_action_url,
        secondary_action_label,
    ) = {
        "calendar": (
            reverse("normal-rounder-workspace"),
            "Edit normal rounder",
            f"{reverse('casual-rounder-rule-create')}{action_query}",
            "Add casual rounder rule",
        ),
        "list": (
            reverse("normal-rounder-workspace"),
            "Edit normal rounder",
            f"{reverse('casual-rounder-rule-create')}{action_query}",
            "Add casual rounder rule",
        ),
        "normal": (
            None,
            None,
            None,
            None,
        ),
        "casual": (
            f"{reverse('casual-rounder-rule-create')}{action_query}",
            "Add casual rounder rule",
            None,
            None,
        ),
    }[rounding_view]
    context = {
        "schedule_workspace_page_title": "Rounding",
        "schedule_workspace_label": "Rounding",
        "rounding_view": rounding_view,
        "rounding_selected_month": month,
        "rounding_action_query": action_query,
        "schedule_workspace_navigation": tuple(
            {
                "label": label,
                "url": _rounding_workspace_url(view, month, start_date, end_date),
                "current": rounding_view == view,
            }
            for label, view in (
                ("Calendar", "calendar"),
                ("List", "list"),
                ("Normal Rounding", "normal"),
                ("Casual Rules", "casual"),
            )
        ),
        "rounding_history_url": f"{reverse('rounding-history')}{action_query}",
        "rounding_primary_action_url": primary_action_url,
        "rounding_primary_action_label": primary_action_label,
        "rounding_secondary_action_url": secondary_action_url,
        "rounding_secondary_action_label": secondary_action_label,
    }
    if month:
        scoped_list_rows = _scoped_rounding_list_rows(projection.list_rows, scope_ids)
        context.update(
            {
                "schedule_workspace_previous_month_url": _rounding_workspace_url(
                    rounding_view, _shift_month(month, -1)
                ),
                "schedule_workspace_next_month_url": _rounding_workspace_url(
                    rounding_view, _shift_month(month, 1)
                ),
                "schedule_workspace_month_picker_action": _rounding_workspace_url(rounding_view),
                "schedule_workspace_month_picker_preserved_query": {},
                "schedule_workspace_month_min": date(2000, 1, 1),
                "schedule_workspace_month_max": date(2100, 12, 1),
                "schedule_workspace_selected_month": month,
                "rounding_projection": projection,
                "work_remaining": _rounding_month_work_remaining(projection, month),
                "rounding_list_rows": scoped_list_rows,
                "rounding_list_table": _rounding_list_table(scoped_list_rows, month),
                "calendar": _rounding_calendar_lattice(month, projection, scope_ids),
            }
        )
    return context


def _rounding_month_work_remaining(projection, month):
    selected_range = InclusiveDateRange(
        month,
        _shift_month(month, 1) - timedelta(days=1),
    )
    items = tuple(
        ProjectedWorkItem(
            key=f"rounding-week-{week.week:%Y-%m-%d}",
            area="rounding",
            label=f"Week of {week.week:%b} {week.week.day}",
            affected_range=InclusiveDateRange(week.week, week.week + timedelta(days=6)),
            matters_on=week.week,
            target_url=_normal_rounder_target_url(
                week.normal_rounder,
                context={
                    "view": "normal",
                    "month": None,
                    "start_date": week.week,
                    "end_date": week.week + timedelta(days=6),
                },
                week=week.week,
            ),
            reasons=(
                ProjectedWorkReason(
                    "required",
                    "Normal rounder is missing"
                    if week.missing
                    else "Normal rounder is inconsistent",
                    1,
                ),
            ),
        )
        for week in projection.normal_weeks
        if week.missing or week.inconsistent
    )
    return focused_work_remaining(
        items,
        selected_range,
    )


def _normal_rounding_work_remaining(rows, selected_range, rounding_context):
    items = tuple(
        ProjectedWorkItem(
            key=f"rounding-week-{item['week']:%Y-%m-%d}",
            area="rounding",
            label=f"Week of {item['week']:%b} {item['week'].day}",
            affected_range=InclusiveDateRange(item["week"], item["week"] + timedelta(days=6)),
            matters_on=item["week"],
            target_url=_normal_rounder_target_url(
                item["normal_rounder"], context=rounding_context, week=item["week"]
            ),
            reasons=(
                ProjectedWorkReason(
                    "required",
                    "Normal rounder is missing"
                    if item["normal_rounder_status"].normal_rounder is None
                    else "Normal rounder is inconsistent",
                    1,
                ),
            ),
        )
        for item in rows
        if (
            item["normal_rounder_status"].normal_rounder is None
            or item["normal_rounder_status"].inconsistent
        )
    )
    return focused_work_remaining(
        items,
        selected_range,
    )


def _scoped_rounding_list_rows(rows, scope_ids):
    return tuple(
        {
            "row": row,
            "presentation": _rounding_normal_row_presentation(
                row.normal_week.normal_rounder_status,
                focused_membership=row.normal_week.focused_membership,
            ),
            "normal_rounder_display": _normal_rounder_scope_display(
                row.normal_week.normal_rounder_status, scope_ids
            )
            or "None",
            "casual_occurrences": tuple(
                RoundingCasualOccurrencePresentation(
                    occurrence.rule.physician.display_name,
                    f"{reverse('casual-rounder-rule-correct', args=(occurrence.rule.pk,))}"
                    f"?view=list&month={occurrence.date:%Y-%m}",
                    occurrence.date,
                )
                for occurrence in row.casual_rounder_occurrences
                if not scope_ids or occurrence.rule.physician_id in scope_ids
            ),
        }
        for row in rows
    )


def _rounding_list_table(rows, selected_month):
    return DataTable(
        identity="rounding-list",
        label="Rounding shifts",
        columns=(
            DataTableColumn("Week"),
            DataTableColumn("Normal rounder"),
            DataTableColumn("Casual rounder(s)"),
            DataTableColumn("Status"),
            DataTableColumn("Actions"),
        ),
        rows=tuple(_rounding_list_table_row(item, selected_month) for item in rows),
        empty_state="No Rounding shifts are available for this month.",
    )


def _normal_rounder_target_url(normal_rounder, *, context, week):
    if normal_rounder:
        return (
            f"{reverse('normal-rounder-correct', args=(normal_rounder.pk,))}?"
            f"{_rounding_return_query(context)}"
        )
    return f"{reverse('normal-rounder-record')}?{_rounding_return_query(context, week=week)}"


def _normal_rounder_table(rows, rounding_context):
    return DataTable(
        identity="normal-rounding",
        label="Normal Rounding shifts",
        columns=tuple(
            DataTableColumn(label) for label in ("Week starting", "Normal rounder", "Actions")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        date_format(item["week"], "N j, Y"),
                        kind="date",
                        machine_value=item["week"].isoformat(),
                        row_header=True,
                    ),
                    DataTableCell(
                        (
                            f"{item['scope_display']} - INCONSISTENT: {item['normal_rounder_status'].reason}"
                            if (
                                item["normal_rounder"]
                                and item["normal_rounder_status"].inconsistent
                                and item["focused_membership"]
                            )
                            else item["scope_display"]
                            if item["normal_rounder"]
                            else (
                                "INCOMPLETE: no normal rounder recorded"
                                if item["focused_membership"]
                                else "No normal rounder recorded"
                            )
                        ),
                        kind="status" if item["presentation"].table_tone else "text",
                        tone=item["presentation"].table_tone,
                    ),
                    DataTableCell(
                        "Edit",
                        kind="link",
                        url=_normal_rounder_target_url(
                            item["normal_rounder"], context=rounding_context, week=item["week"]
                        ),
                    ),
                ),
                tone=item["presentation"].table_tone,
            )
            for item in rows
        ),
        empty_state="No Rounding shifts are available for this range.",
    )


def _casual_rounder_roster_table(physicians):
    return DataTable(
        identity="casual-rounder-roster",
        label="Current casual rounders",
        columns=tuple(
            DataTableColumn(label) for label in ("Physician", "Current rule status", "Actions")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        physician.display_name,
                        kind="link",
                        url=reverse("physician-detail", args=(physician.pk,)),
                        row_header=True,
                    ),
                    DataTableCell(
                        f"{len(physician.current_casual_rules)} current rule"
                        f"{'s' if len(physician.current_casual_rules) != 1 else ''}"
                        if physician.current_casual_rules
                        else "Not configured"
                    ),
                    DataTableCell("Add rule", kind="link", url=physician.casual_rule_add_url),
                )
            )
            for physician in physicians
        ),
        empty_state="No physicians currently have a Casual Rounder role.",
    )


def _casual_rule_context_table(rules, *, current_rule, fixed_physician, edit_query):
    columns = [] if fixed_physician else [DataTableColumn("Physician")]
    columns.extend(
        (
            DataTableColumn("Weekday"),
            DataTableColumn("Recurrence"),
            DataTableColumn("Effective range"),
            DataTableColumn("Context"),
        )
    )
    return DataTable(
        identity="casual-rounder-rule-context",
        label="Existing casual rounder rules",
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    (
                        []
                        if fixed_physician
                        else [DataTableCell(rule.physician.display_name, row_header=True)]
                    )
                    + [
                        DataTableCell(rule.get_weekday_display()),
                        DataTableCell(rule.ordinal_display),
                        DataTableCell(
                            f"{date_format(rule.effective_start, 'N j, Y') if rule.effective_start else 'Unbounded past'} through "
                            f"{date_format(rule.effective_end, 'N j, Y') if rule.effective_end else 'Unbounded future'}"
                        ),
                        DataTableCell(
                            "Editing this rule"
                            if current_rule and rule.pk == current_rule.pk
                            else "Retained rule: Edit",
                            kind="status"
                            if current_rule and rule.pk == current_rule.pk
                            else "link",
                            url=(
                                ""
                                if current_rule and rule.pk == current_rule.pk
                                else f"{reverse('casual-rounder-rule-correct', args=(rule.pk,))}?{edit_query}"
                            ),
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


def _rounding_list_table_row(item, selected_month):
    row = item["row"]
    normal_week = row.normal_week
    if normal_week.focused_membership and normal_week.missing:
        status = DataTableCell("Missing normal rounder", kind="status", tone="danger")
    elif normal_week.focused_membership and normal_week.inconsistent:
        status = DataTableCell(
            f"Inconsistent: {normal_week.inconsistency_reason}", kind="status", tone="attention"
        )
    elif normal_week.normal_rounder:
        status = DataTableCell("Filled" if normal_week.focused_membership else "Recorded")
    else:
        status = DataTableCell("No normal rounder recorded", kind="metadata")
    action_url = (
        f"{reverse('normal-rounder-correct', args=(normal_week.normal_rounder.pk,))}"
        f"?view=list&month={selected_month:%Y-%m}"
        if normal_week.normal_rounder
        else f"{reverse('normal-rounder-record')}?view=list&month={selected_month:%Y-%m}"
        f"&week={normal_week.week:%Y-%m-%d}"
    )
    casual_occurrences = item["casual_occurrences"]
    return DataTableRow(
        cells=(
            DataTableCell(
                f"{normal_week.week:%b} {normal_week.week.day} - {row.end_date:%b} {row.end_date.day}",
                kind="date",
                machine_value=normal_week.week.isoformat(),
            ),
            DataTableCell(item["normal_rounder_display"]),
            DataTableCell(
                "",
                kind="links",
                links=tuple(
                    DataTableLink(
                        f"{occurrence.physician_name} ({occurrence.date:%A})", occurrence.url
                    )
                    for occurrence in casual_occurrences
                ),
            )
            if casual_occurrences
            else DataTableCell("None", kind="metadata"),
            status,
            DataTableCell("Edit", kind="link", url=action_url),
        ),
        tone=item["presentation"].table_tone,
    )


def _rounding_history_table(changes):
    return DataTable(
        identity="rounding-history",
        label="Rounding Schedule history",
        width="compact",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Fact",
                "Physician",
                "Change",
                "Prior value",
                "Recorded value",
                "Note",
            )
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.get_subject_display(), kind="metadata"),
                    DataTableCell(change.physician.display_name, row_header=True),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No Rounding Schedule changes recorded.",
    )


def _rounding_calendar_lattice(month, projection, scope_ids=()):
    return CalendarLattice(
        label=f"{month:%B %Y} Rounding calendar",
        columns=("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"),
        variant="seven-column",
        has_gutter=True,
        holiday_header_labels=False,
        weeks=tuple(
            CalendarWeek(
                label=(
                    f"{week.start_date:%B} {week.start_date.day} through "
                    f"{week.end_date:%B} {week.end_date.day}, {week.end_date.year}"
                ),
                days=tuple(
                    CalendarDay(
                        day.date.isoformat(), f"{day.date:%b} {day.date.day}", day.in_selected_month
                    )
                    for day in week.days
                ),
                lanes=(
                    CalendarLane(
                        "Rounder",
                        tuple(
                            CalendarItem(
                                (segment.start_date - week.start_date).days + 1,
                                len(segment.displayed_dates),
                                "schedules/calendar_rounding_normal_item.html",
                                RoundingNormalPresentation(
                                    segment.physician.display_name
                                    if segment.source_id
                                    and (not scope_ids or segment.physician.pk in scope_ids)
                                    else (
                                        "Assigned outside focus"
                                        if segment.source_id
                                        else (
                                            "Normal rounder missing"
                                            if segment.normal_week.focused_membership
                                            else "No normal rounder recorded"
                                        )
                                    ),
                                    (
                                        f"Inconsistent: {segment.inconsistency_reason}"
                                        if segment.inconsistency_reason
                                        else "Normal rounder"
                                    )
                                    if segment.source_id
                                    else (
                                        f"Edit week of {segment.normal_week.week}"
                                        if segment.normal_week.focused_membership
                                        else "Historical record"
                                    ),
                                    (
                                        "inconsistent"
                                        if segment.inconsistency_reason
                                        else ("missing" if not segment.source_id else "")
                                    )
                                    if segment.normal_week.focused_membership
                                    else ("historical-gap" if not segment.source_id else ""),
                                    (
                                        f"{reverse('normal-rounder-correct', args=(segment.source_id,))}"
                                        f"?view=calendar&month={month:%Y-%m}"
                                        if segment.source_id
                                        else f"{reverse('normal-rounder-record')}?view=calendar&month={month:%Y-%m}&week={segment.normal_week.week:%Y-%m-%d}"
                                    ),
                                ),
                            )
                            for segment in week.normal_week_segments
                        ),
                        variant="range",
                        holiday_items=True,
                    ),
                    CalendarLane(
                        "Casual",
                        tuple(
                            CalendarItem(
                                index,
                                1,
                                "schedules/calendar_rounding_casual_item.html",
                                RoundingCasualPresentation(
                                    tuple(
                                        RoundingCasualOccurrencePresentation(
                                            rule.physician.display_name,
                                            f"{reverse('casual-rounder-rule-correct', args=(rule.pk,))}"
                                            f"?view=calendar&month={month:%Y-%m}",
                                        )
                                        for rule in day.casual_rounder_rules
                                        if not scope_ids or rule.physician_id in scope_ids
                                    )
                                ),
                            )
                            for index, day in enumerate(week.days, start=1)
                            if any(
                                not scope_ids or rule.physician_id in scope_ids
                                for rule in day.casual_rounder_rules
                            )
                        ),
                    ),
                ),
            )
            for week in projection.calendar_weeks
        ),
    )


def _rounding_form_context(request, **context):
    context["return_url"] = _rounding_redirect_url(request)
    context["return_label"] = "Back to Rounding Schedule"
    if context.get("normal_rounder"):
        context["remove_action"] = _rounding_action_url(
            "normal-rounder-remove", request, context["normal_rounder"].pk
        )
    if context.get("casual_rule"):
        context["remove_action"] = _rounding_action_url(
            "casual-rounder-rule-remove", request, context["casual_rule"].pk
        )
    return context
