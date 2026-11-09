"""Normal and casual Rounding sources, projections, lifecycle, and history."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse

from ..models.physicians import EffectiveRole, Physician
from ..models.preceptors import PreceptorSession
from ..models.rounding import CasualRounderRule, NormalRounder, RoundingScheduleChange
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from ._recurrence import normalize_ordinals
from .lead_times import lead_time_months
from .operational_attention import is_operational_preceptor_week
from .work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    focused_range_has_membership,
    shift_date_months,
)


@dataclass(frozen=True)
class NormalRounderStatus:
    week: date
    normal_rounder: NormalRounder | None
    normal_rounder_physician: Physician | None
    eligible: bool | None
    is_consistent: bool | None
    reason: str | None

    @property
    def inconsistent(self):
        return self.is_consistent is False

    @property
    def is_eligible(self):
        return self.eligible


@dataclass(frozen=True)
class RoundingEffect:
    normal_rounder: NormalRounder | None
    normal_rounder_physician: Physician | None
    casual_rounder_rules: tuple[CasualRounderRule, ...]
    casual_rounder_physicians: tuple[Physician, ...]
    availability: str | None
    is_applicable: bool
    normal_rounder_eligible: bool | None = None
    normal_rounder_consistency_reason: str | None = None


@dataclass(frozen=True)
class RoundingMonthNormalWeek:
    """One Monday-Sunday normal-rounder source week in a month projection."""

    week: date
    normal_rounder_status: NormalRounderStatus
    normal_rounder: NormalRounder | None
    physician: Physician | None
    missing: bool
    inconsistent: bool
    inconsistency_reason: str | None
    source_id: int | None
    operational_attention: bool = True
    focused_membership: bool = True


@dataclass(frozen=True)
class RoundingMonthDay:
    """The resolved rounding sources for one visible calendar date."""

    date: date
    in_selected_month: bool
    normal_week: RoundingMonthNormalWeek
    casual_rounder_rules: tuple[CasualRounderRule, ...]
    casual_rounder_physicians: tuple[Physician, ...]

    @property
    def normal_rounder(self):
        return self.normal_week.normal_rounder

    @property
    def normal_rounder_status(self):
        return self.normal_week.normal_rounder_status


@dataclass(frozen=True)
class RoundingMonthCasualOccurrence:
    """One derived casual-rounder occurrence in a normal rounding week."""

    date: date
    rule: CasualRounderRule


@dataclass(frozen=True)
class RoundingMonthListRow:
    """One Monday-Sunday normal-rounding week in the selected month list."""

    normal_week: RoundingMonthNormalWeek
    end_date: date
    casual_rounder_occurrences: tuple[RoundingMonthCasualOccurrence, ...]


@dataclass(frozen=True)
class RoundingMonthSegment:
    """The portion of one normal week displayed in a Sunday-Saturday grid row."""

    normal_week: RoundingMonthNormalWeek
    start_date: date
    end_date: date
    displayed_dates: tuple[date, ...]
    physician: Physician | None
    missing: bool
    inconsistency_reason: str | None
    source_id: int | None


@dataclass(frozen=True)
class RoundingMonthCalendarWeek:
    start_date: date
    end_date: date
    days: tuple[RoundingMonthDay, ...]
    normal_week_segments: tuple[RoundingMonthSegment, ...]


@dataclass(frozen=True)
class RoundingMonthWorkSummary:
    missing_normal_weeks: tuple[RoundingMonthNormalWeek, ...]
    inconsistent_normal_weeks: tuple[RoundingMonthNormalWeek, ...]

    @property
    def missing_normal_week_count(self):
        return len(self.missing_normal_weeks)

    @property
    def inconsistent_normal_week_count(self):
        return len(self.inconsistent_normal_weeks)


@dataclass(frozen=True)
class RoundingMonthProjection:
    selected_month: date
    calendar_weeks: tuple[RoundingMonthCalendarWeek, ...]
    normal_weeks: tuple[RoundingMonthNormalWeek, ...]
    list_rows: tuple[RoundingMonthListRow, ...]
    work_summary: RoundingMonthWorkSummary

    @property
    def grid_dates(self):
        return tuple(day for week in self.calendar_weeks for day in week.days)

    @property
    def normal_week_segments(self):
        return tuple(
            segment for week in self.calendar_weeks for segment in week.normal_week_segments
        )


def rounding_week_start(on_date):
    return on_date - timedelta(days=on_date.weekday())


def normal_rounder_for_week(week):
    if week.weekday() != 0:
        raise ValidationError({"week": "Rounding weeks must start on a Monday."})
    return NormalRounder.objects.filter(week=week).select_related("physician").first()


def normal_rounder_for_date(on_date):
    return normal_rounder_for_week(rounding_week_start(on_date))


def normal_rounder_missing_dates(physician, week):
    """Return dates in a Monday-Sunday week without Normal Rounder role coverage."""
    if week.weekday() != 0:
        raise ValidationError({"week": "Rounding weeks must start on a Monday."})
    physician_id = getattr(physician, "pk", physician)
    return tuple(
        on_date
        for offset in range(7)
        for on_date in (week + timedelta(days=offset),)
        if not EffectiveRole.objects.filter(
            physician_id=physician_id,
            role__in=(EffectiveRole.Role.FACULTY, EffectiveRole.Role.COMMUNITY_ROUNDER),
            effective_start__lte=on_date,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .exists()
    )


def normal_rounder_covers_week(physician, week):
    return not normal_rounder_missing_dates(physician, week)


def normal_rounders_for_week(week):
    """Return only physicians eligible to own a selected Monday-Sunday week."""
    return normal_rounders_for_weeks((week,))


def normal_rounders_for_weeks(weeks):
    """Return physicians eligible to own at least one selected rounding week."""
    weeks = tuple(weeks)
    if not weeks:
        return Physician.objects.none()
    if any(week.weekday() != 0 for week in weeks):
        raise ValidationError({"week": "Rounding weeks must start on a Monday."})
    roles_by_physician = {}
    for role in EffectiveRole.objects.filter(
        role__in=(EffectiveRole.Role.FACULTY, EffectiveRole.Role.COMMUNITY_ROUNDER),
        effective_start__lte=max(weeks) + timedelta(days=6),
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=min(weeks))):
        roles_by_physician.setdefault(role.physician_id, []).append(role)
    eligible_ids = {
        physician_id
        for physician_id, roles in roles_by_physician.items()
        if any(
            all(
                any(
                    role.effective_start <= week + timedelta(days=offset)
                    and (
                        role.effective_end is None
                        or role.effective_end >= week + timedelta(days=offset)
                    )
                    for role in roles
                )
                for offset in range(7)
            )
            for week in weeks
        )
    }
    return Physician.objects.filter(pk__in=eligible_ids)


def normal_rounder_eligibility_message(physician, week):
    missing_dates = normal_rounder_missing_dates(physician, week)
    return _inconsistency_reason(physician, week, missing_dates)


def _inconsistency_reason(physician, week, missing_dates):
    if not missing_dates:
        return None
    missing = ", ".join(on_date.isoformat() for on_date in missing_dates)
    week_end = week + timedelta(days=6)
    return (
        f"{physician} is not eligible to be the Normal Rounder for the full Monday-Sunday week "
        f"from {week.isoformat()} through {week_end.isoformat()}; missing {missing}."
    )


def normal_rounder_status_for_week(week):
    return normal_rounder_statuses_for_weeks((week,))[0]


def normal_rounder_statuses_for_weeks(weeks):
    """Resolve normal-rounder status for bounded Monday-Sunday weeks in three queries or fewer."""
    weeks = tuple(weeks)
    if any(week.weekday() != 0 for week in weeks):
        raise ValidationError({"week": "Rounding weeks must start on a Monday."})
    assignments = {
        assignment.week: assignment
        for assignment in NormalRounder.objects.filter(week__in=weeks).select_related("physician")
    }
    assigned_physician_ids = {assignment.physician_id for assignment in assignments.values()}
    roles_by_physician = {physician_id: [] for physician_id in assigned_physician_ids}
    if assigned_physician_ids:
        first_date = min(weeks)
        last_date = max(weeks) + timedelta(days=6)
        roles = EffectiveRole.objects.filter(
            physician_id__in=assigned_physician_ids,
            role__in=(EffectiveRole.Role.FACULTY, EffectiveRole.Role.COMMUNITY_ROUNDER),
            effective_start__lte=last_date,
        ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
        for role in roles:
            roles_by_physician[role.physician_id].append(role)
    statuses = []
    for week in weeks:
        assignment = assignments.get(week)
        if not assignment:
            statuses.append(NormalRounderStatus(week, None, None, None, None, None))
            continue
        missing_dates = tuple(
            on_date
            for offset in range(7)
            for on_date in (week + timedelta(days=offset),)
            if not any(
                role.effective_start <= on_date
                and (role.effective_end is None or role.effective_end >= on_date)
                for role in roles_by_physician[assignment.physician_id]
            )
        )
        eligible = not missing_dates
        statuses.append(
            NormalRounderStatus(
                week,
                assignment,
                assignment.physician,
                eligible,
                eligible,
                _inconsistency_reason(assignment.physician, week, missing_dates),
            )
        )
    return tuple(statuses)


def normal_rounding_projected_work_items(week_starts):
    """Project each operational Normal Rounding week with unresolved work once."""
    weeks = tuple(sorted({rounding_week_start(week_start) for week_start in week_starts}))
    weeks = tuple(week for week in weeks if is_operational_preceptor_week(week))
    if not weeks:
        return ()

    items = []
    for status in normal_rounder_statuses_for_weeks(weeks):
        if status.normal_rounder is not None and not status.inconsistent:
            continue
        week = status.week
        items.append(
            ProjectedWorkItem(
                key=f"rounding-week-{week:%Y-%m-%d}",
                area="rounding",
                label=f"Week of {week:%b} {week.day}",
                affected_range=InclusiveDateRange(week, week + timedelta(days=6)),
                matters_on=shift_date_months(week, -lead_time_months("normal_rounding")),
                target_url=(
                    f"{reverse('normal-rounder-workspace')}?start_date={week:%Y-%m-%d}"
                    f"&end_date={week:%Y-%m-%d}"
                ),
                reasons=(
                    ProjectedWorkReason(
                        "required",
                        "Normal rounder is missing"
                        if status.normal_rounder is None
                        else "Normal rounder is inconsistent",
                        1,
                    ),
                ),
            )
        )
    return tuple(items)


def normal_rounder_status_on(on_date):
    return normal_rounder_status_for_week(rounding_week_start(on_date))


normal_rounder_eligibility_on = normal_rounder_status_on
normal_rounder_consistency_on = normal_rounder_status_on


def rounding_weeks(start_date, end_date):
    _validate_date_range(start_date, end_date)
    week = rounding_week_start(start_date)
    last_week = rounding_week_start(end_date)
    result = []
    while week <= last_week:
        result.append(week)
        week += timedelta(days=7)
    return tuple(result)


def missing_normal_rounder_weeks(start_date, end_date):
    recorded = set(
        NormalRounder.objects.filter(
            week__range=(rounding_week_start(start_date), rounding_week_start(end_date))
        ).values_list("week", flat=True)
    )
    return tuple(week for week in rounding_weeks(start_date, end_date) if week not in recorded)


@scheduling_mutation
@transaction.atomic
def record_normal_rounder(*, week, physician, scheduler, reason=""):
    require_scheduler(scheduler)
    if week.weekday() != 0:
        raise ValidationError({"week": "Rounding weeks must start on a Monday."})
    if message := normal_rounder_eligibility_message(physician, week):
        raise ValidationError({"physician": message})
    capture_calendar_range(week, week + timedelta(days=4))
    normal_rounder = NormalRounder.objects.select_for_update().filter(week=week).first()
    if normal_rounder and normal_rounder.physician_id == physician.pk:
        return normal_rounder
    if normal_rounder:
        previous_value = _normal_rounder_value(normal_rounder)
        normal_rounder.physician = physician
        normal_rounder.full_clean()
        normal_rounder.save(update_fields=("physician",))
        _record_change(
            subject=RoundingScheduleChange.Subject.NORMAL_ROUNDER,
            action=RoundingScheduleChange.Action.CORRECTED,
            normal_rounder=normal_rounder,
            casual_rounder_rule=None,
            physician=physician,
            previous_value=previous_value,
            recorded_value=_normal_rounder_value(normal_rounder),
            scheduler=scheduler,
            reason=reason,
        )
        return normal_rounder
    normal_rounder = NormalRounder(week=week, physician=physician)
    normal_rounder.full_clean()
    normal_rounder.save()
    _record_change(
        subject=RoundingScheduleChange.Subject.NORMAL_ROUNDER,
        action=RoundingScheduleChange.Action.CREATED,
        normal_rounder=normal_rounder,
        casual_rounder_rule=None,
        physician=physician,
        previous_value=None,
        recorded_value=_normal_rounder_value(normal_rounder),
        scheduler=scheduler,
        reason=reason,
    )
    return normal_rounder


@scheduling_mutation
@transaction.atomic
def remove_normal_rounder(*, normal_rounder, scheduler, reason=""):
    require_scheduler(scheduler)
    normal_rounder = (
        NormalRounder.objects.select_for_update()
        .select_related("physician")
        .get(pk=normal_rounder.pk)
    )
    capture_calendar_range(normal_rounder.week, normal_rounder.week + timedelta(days=4))
    _record_change(
        subject=RoundingScheduleChange.Subject.NORMAL_ROUNDER,
        action=RoundingScheduleChange.Action.REMOVED,
        normal_rounder=normal_rounder,
        casual_rounder_rule=None,
        physician=normal_rounder.physician,
        previous_value=_normal_rounder_value(normal_rounder),
        recorded_value=None,
        scheduler=scheduler,
        reason=reason,
    )
    normal_rounder.delete()


@scheduling_mutation
@transaction.atomic
def create_casual_rounder_rule(
    *, physician, weekday, ordinals, effective_start=None, effective_end=None, scheduler, reason=""
):
    require_scheduler(scheduler)
    ordinals = normalize_ordinals(ordinals)
    existing = CasualRounderRule.objects.filter(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        effective_start=effective_start,
        effective_end=effective_end,
    ).first()
    if existing:
        return existing
    rule = CasualRounderRule(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    rule.full_clean()
    rule.save()
    _record_change(
        subject=RoundingScheduleChange.Subject.CASUAL_ROUNDER_RULE,
        action=RoundingScheduleChange.Action.CREATED,
        normal_rounder=None,
        casual_rounder_rule=rule,
        physician=physician,
        previous_value=None,
        recorded_value=_casual_rounder_rule_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def correct_casual_rounder_rule(
    *,
    rule,
    physician,
    weekday,
    ordinals,
    effective_start=None,
    effective_end=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    rule = CasualRounderRule.objects.select_for_update().select_related("physician").get(pk=rule.pk)
    ordinals = normalize_ordinals(ordinals)
    previous_value = _casual_rounder_rule_value(rule)
    recorded_value = _casual_rounder_rule_value_values(
        physician, weekday, ordinals, effective_start, effective_end
    )
    if previous_value == recorded_value:
        return rule
    rule.physician = physician
    rule.weekday = weekday
    rule.ordinals = ordinals
    rule.effective_start = effective_start
    rule.effective_end = effective_end
    rule.full_clean()
    rule.save(
        update_fields=("physician", "weekday", "ordinals", "effective_start", "effective_end")
    )
    _record_change(
        subject=RoundingScheduleChange.Subject.CASUAL_ROUNDER_RULE,
        action=RoundingScheduleChange.Action.CORRECTED,
        normal_rounder=None,
        casual_rounder_rule=rule,
        physician=physician,
        previous_value=previous_value,
        recorded_value=_casual_rounder_rule_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def remove_casual_rounder_rule(*, rule, scheduler, reason=""):
    require_scheduler(scheduler)
    rule = CasualRounderRule.objects.select_for_update().select_related("physician").get(pk=rule.pk)
    _record_change(
        subject=RoundingScheduleChange.Subject.CASUAL_ROUNDER_RULE,
        action=RoundingScheduleChange.Action.REMOVED,
        normal_rounder=None,
        casual_rounder_rule=rule,
        physician=rule.physician,
        previous_value=_casual_rounder_rule_value(rule),
        recorded_value=None,
        scheduler=scheduler,
        reason=reason,
    )
    rule.delete()


def casual_rounder_rules_on(on_date):
    rules = (
        CasualRounderRule.objects.filter(weekday=on_date.weekday())
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .select_related("physician")
    )
    return tuple(rule for rule in rules if rule.matches(on_date))


def rounding_effect_on(on_date, period_of_day):
    if period_of_day not in PreceptorSession.PeriodOfDay.values:
        raise ValidationError({"period_of_day": "Period must be AM or PM."})
    status = normal_rounder_status_on(on_date)
    normal_rounder = status.normal_rounder
    rules = casual_rounder_rules_on(on_date)
    applicable = on_date.weekday() < 5
    availability = "backup_only" if applicable and normal_rounder and rules else None
    if applicable and normal_rounder and not rules:
        availability = "not_available"
    return RoundingEffect(
        normal_rounder,
        normal_rounder.physician if normal_rounder else None,
        rules,
        tuple(rule.physician for rule in rules),
        availability,
        applicable,
        status.eligible,
        status.reason,
    )


rounding_effect_for_session = rounding_effect_on


def rounding_week_has_focused_membership(week, selected_range):
    """Whether a displayed normal-rounder week has current or future scope membership."""
    return focused_range_has_membership(
        InclusiveDateRange(week, week + timedelta(days=6)), selected_range
    )


def rounding_month_projection(month, *, focused_range=None):
    """Resolve one selected calendar month without materializing rounding occurrences."""
    selected_month = month.replace(day=1)
    displayed_weeks = tuple(
        tuple(week)
        for week in calendar.Calendar(firstweekday=calendar.SUNDAY).monthdatescalendar(
            selected_month.year, selected_month.month
        )
    )
    calendar_start = displayed_weeks[0][0]
    calendar_end = displayed_weeks[-1][-1]
    week_starts = rounding_weeks(
        selected_month,
        selected_month.replace(
            day=calendar.monthrange(selected_month.year, selected_month.month)[1]
        ),
    )
    displayed_week_starts = rounding_weeks(calendar_start, calendar_end)
    focused_by_week = {
        week: focused_range is None or rounding_week_has_focused_membership(week, focused_range)
        for week in displayed_week_starts
    }
    active_weeks = displayed_week_starts
    statuses_by_week = {
        status.week: status for status in normal_rounder_statuses_for_weeks(active_weeks)
    }
    displayed_normal_weeks = {
        week: RoundingMonthNormalWeek(
            week=week,
            normal_rounder_status=statuses_by_week[week],
            normal_rounder=statuses_by_week[week].normal_rounder,
            physician=statuses_by_week[week].normal_rounder_physician,
            missing=statuses_by_week[week].normal_rounder is None,
            inconsistent=statuses_by_week[week].inconsistent,
            inconsistency_reason=statuses_by_week[week].reason,
            source_id=statuses_by_week[week].normal_rounder.pk
            if statuses_by_week[week].normal_rounder
            else None,
            operational_attention=focused_by_week[week],
            focused_membership=focused_by_week[week],
        )
        for week in displayed_week_starts
    }
    normal_weeks = tuple(displayed_normal_weeks[week] for week in week_starts)
    calendar_dates = tuple(
        on_date for displayed_week in displayed_weeks for on_date in displayed_week
    )
    list_week_dates = tuple(
        normal_week.week + timedelta(days=offset)
        for normal_week in normal_weeks
        for offset in range(7)
    )
    resolved_dates = tuple(sorted({*calendar_dates, *list_week_dates}))
    casual_rules = tuple(
        CasualRounderRule.objects.filter(
            Q(effective_start__isnull=True) | Q(effective_start__lte=resolved_dates[-1])
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=resolved_dates[0]))
        .select_related("physician")
    )

    def matching_casual_rules(on_date):
        return tuple(
            sorted(
                (rule for rule in casual_rules if rule.matches(on_date)),
                key=lambda rule: (
                    rule.physician.display_name.casefold(),
                    rule.physician.display_name,
                    rule.pk,
                ),
            )
        )

    days_by_date = {}
    for on_date in resolved_dates:
        rules = matching_casual_rules(on_date)
        days_by_date[on_date] = RoundingMonthDay(
            date=on_date,
            in_selected_month=on_date.year == selected_month.year
            and on_date.month == selected_month.month,
            normal_week=displayed_normal_weeks[rounding_week_start(on_date)],
            casual_rounder_rules=rules,
            casual_rounder_physicians=tuple(rule.physician for rule in rules),
        )
    calendar_weeks = []
    for displayed_week in displayed_weeks:
        segments = []
        for normal_week_start in sorted({rounding_week_start(day) for day in displayed_week}):
            displayed_dates = tuple(
                day for day in displayed_week if rounding_week_start(day) == normal_week_start
            )
            normal_week = displayed_normal_weeks[normal_week_start]
            segments.append(
                RoundingMonthSegment(
                    normal_week,
                    displayed_dates[0],
                    displayed_dates[-1],
                    displayed_dates,
                    normal_week.physician,
                    normal_week.missing,
                    normal_week.inconsistency_reason,
                    normal_week.source_id,
                )
            )
        calendar_weeks.append(
            RoundingMonthCalendarWeek(
                displayed_week[0],
                displayed_week[-1],
                tuple(days_by_date[on_date] for on_date in displayed_week),
                tuple(segments),
            )
        )
    missing_normal_weeks = tuple(
        normal_week
        for normal_week in normal_weeks
        if normal_week.focused_membership and normal_week.missing
    )
    inconsistent_normal_weeks = tuple(
        normal_week
        for normal_week in normal_weeks
        if normal_week.focused_membership and normal_week.inconsistent
    )
    return RoundingMonthProjection(
        selected_month,
        tuple(calendar_weeks),
        normal_weeks,
        tuple(
            RoundingMonthListRow(
                normal_week,
                normal_week.week + timedelta(days=6),
                tuple(
                    RoundingMonthCasualOccurrence(date=on_date, rule=rule)
                    for offset in range(7)
                    for on_date in (normal_week.week + timedelta(days=offset),)
                    for rule in days_by_date[on_date].casual_rounder_rules
                ),
            )
            for normal_week in normal_weeks
        ),
        RoundingMonthWorkSummary(missing_normal_weeks, inconsistent_normal_weeks),
    )


def operational_rounding_month_projection(month):
    """Project retained rounding facts with the selected month as the focused range."""
    selected_month = month.replace(day=1)
    selected_range = InclusiveDateRange(
        selected_month,
        selected_month.replace(
            day=calendar.monthrange(selected_month.year, selected_month.month)[1]
        ),
    )
    return rounding_month_projection(selected_month, focused_range=selected_range)


def focused_rounding_month_projection(month):
    """Project one month with the same focused membership used by its workspace."""
    return operational_rounding_month_projection(month)


def _validate_date_range(start_date, end_date):
    if not start_date or not end_date:
        raise ValidationError("Start and end dates are required.")
    if end_date < start_date:
        raise ValidationError({"end_date": "End date cannot be before start date."})


def _normal_rounder_value(normal_rounder):
    return {
        "week": normal_rounder.week.isoformat(),
        "physician_id": normal_rounder.physician_id,
        "physician_display_name": normal_rounder.physician.display_name,
    }


def _casual_rounder_rule_value(rule):
    return _casual_rounder_rule_value_values(
        rule.physician, rule.weekday, rule.ordinals, rule.effective_start, rule.effective_end
    )


def _casual_rounder_rule_value_values(physician, weekday, ordinals, effective_start, effective_end):
    return {
        "physician_id": physician.pk,
        "physician_display_name": physician.display_name,
        "weekday": weekday,
        "ordinals": sorted(ordinals),
        "effective_start": effective_start.isoformat() if effective_start else None,
        "effective_end": effective_end.isoformat() if effective_end else None,
    }


def _record_change(
    *,
    subject,
    action,
    normal_rounder,
    casual_rounder_rule,
    physician,
    previous_value,
    recorded_value,
    scheduler,
    reason,
):
    source = normal_rounder or casual_rounder_rule
    return RoundingScheduleChange.objects.create(
        normal_rounder=normal_rounder,
        casual_rounder_rule=casual_rounder_rule,
        source_id_at_recording=source.pk,
        subject=subject,
        physician=physician,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
        reason=normalize_note(reason),
    )
