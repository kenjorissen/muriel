"""Community Preceptor recurrence, availability, lifecycle, history, and projections."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from ..models.community_preceptors import CommunityPreceptorRule, CommunityPreceptorScheduleChange
from ._mutation import normalize_note, require_scheduler, scheduling_mutation
from ._recurrence import normalize_ordinals
from .holidays import holiday_projections_for_dates


@dataclass(frozen=True)
class CommunityPreceptorAvailabilityStatus:
    base_availability: str
    is_expected_commitment: bool
    source: str | None
    supporting_rule: CommunityPreceptorRule | None

    @property
    def expected_commitment(self):
        return self.is_expected_commitment

    @property
    def is_expected(self):
        return self.is_expected_commitment

    @property
    def rule(self):
        return self.supporting_rule

    @property
    def community_preceptor_rule(self):
        return self.supporting_rule


CommunityPreceptorStatus = CommunityPreceptorAvailabilityStatus


@dataclass(frozen=True)
class CommunityPreceptorOccurrence:
    """One expected base commitment, ordered by date, AM/PM, then physician name and ID."""

    date: date
    period: str
    rule: CommunityPreceptorRule
    physician_id: int
    physician_display_name: str
    rule_id: int


@scheduling_mutation
@transaction.atomic
def create_community_preceptor_rule(
    *,
    physician,
    weekday,
    ordinals,
    periods=None,
    effective_start=None,
    effective_end=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    ordinals = normalize_ordinals(ordinals)
    periods = _normalize_periods(periods)
    existing = CommunityPreceptorRule.objects.filter(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        periods=periods,
        effective_start=effective_start,
        effective_end=effective_end,
    ).first()
    if existing:
        return existing
    rule = CommunityPreceptorRule(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        periods=periods,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    rule.full_clean()
    rule.save()
    _record_change(
        rule=rule,
        action=CommunityPreceptorScheduleChange.Action.CREATED,
        previous_value=None,
        recorded_value=_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def correct_community_preceptor_rule(
    *,
    rule,
    physician,
    weekday,
    ordinals,
    periods=None,
    effective_start=None,
    effective_end=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    rule = (
        CommunityPreceptorRule.objects.select_for_update()
        .select_related("physician")
        .get(pk=rule.pk)
    )
    ordinals = normalize_ordinals(ordinals)
    periods = _normalize_periods(periods)
    previous_value = _value(rule)
    recorded_value = _value_values(
        physician, weekday, ordinals, periods, effective_start, effective_end
    )
    if previous_value == recorded_value:
        return rule
    rule.physician = physician
    rule.weekday = weekday
    rule.ordinals = ordinals
    rule.periods = periods
    rule.effective_start = effective_start
    rule.effective_end = effective_end
    rule.full_clean()
    rule.save(
        update_fields=(
            "physician",
            "weekday",
            "ordinals",
            "periods",
            "effective_start",
            "effective_end",
        )
    )
    _record_change(
        rule=rule,
        action=CommunityPreceptorScheduleChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def remove_community_preceptor_rule(*, rule, scheduler, reason=""):
    require_scheduler(scheduler)
    rule = (
        CommunityPreceptorRule.objects.select_for_update()
        .select_related("physician")
        .get(pk=rule.pk)
    )
    _record_change(
        rule=rule,
        action=CommunityPreceptorScheduleChange.Action.REMOVED,
        previous_value=_value(rule),
        recorded_value=None,
        scheduler=scheduler,
        reason=reason,
    )
    rule.delete()


def community_preceptor_rules_on(on_date):
    rules = (
        CommunityPreceptorRule.objects.filter(weekday=on_date.weekday())
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .select_related("physician")
    )
    return tuple(rule for rule in rules if rule.matches(on_date))


def community_preceptor_rules_for(physician, on_date):
    return tuple(
        rule for rule in community_preceptor_rules_on(on_date) if rule.physician_id == physician.pk
    )


def community_preceptor_occurrences(start_date, end_date, *, holiday_projections=None):
    """Resolve expected commitments in an explicit inclusive date range without persisting them."""
    if not start_date or not end_date:
        raise ValidationError("Start and end dates are required.")
    if end_date < start_date:
        raise ValidationError({"end_date": "End date cannot be before start date."})
    rules = tuple(
        CommunityPreceptorRule.objects.filter(
            Q(effective_start__isnull=True) | Q(effective_start__lte=end_date)
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=start_date))
        .select_related("physician")
    )
    dates = tuple(
        start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)
    )
    holiday_projections = (
        holiday_projections
        if holiday_projections is not None
        else holiday_projections_for_dates(dates)
    )
    closed_coverage = {
        (holiday.observed_date, period)
        for holiday in holiday_projections
        for period in CommunityPreceptorRule.PeriodOfDay.values
        if holiday.closes(period)
    }
    occurrences = []
    on_date = start_date
    while on_date <= end_date:
        for rule in rules:
            if rule.matches(on_date):
                occurrences.extend(
                    CommunityPreceptorOccurrence(
                        date=on_date,
                        period=period,
                        rule=rule,
                        physician_id=rule.physician_id,
                        physician_display_name=rule.physician.display_name,
                        rule_id=rule.pk,
                    )
                    for period in CommunityPreceptorRule.PeriodOfDay.values
                    if period in rule.periods and (on_date, period) not in closed_coverage
                )
        on_date += timedelta(days=1)
    period_order = {
        period: index for index, period in enumerate(CommunityPreceptorRule.PeriodOfDay.values)
    }
    return tuple(
        sorted(
            occurrences,
            key=lambda occurrence: (
                occurrence.date,
                period_order[occurrence.period],
                occurrence.physician_display_name.casefold(),
                occurrence.physician_display_name,
                occurrence.physician_id,
                occurrence.rule_id,
            ),
        )
    )


def community_preceptor_month_projection(month):
    """Resolve only the real dates of one selected calendar month."""
    selected_month = month.replace(day=1)
    month_end = selected_month.replace(
        day=calendar.monthrange(selected_month.year, selected_month.month)[1]
    )
    return community_preceptor_occurrences(selected_month, month_end)


def community_preceptor_availability_on(physician, on_date, period_of_day):
    if period_of_day not in CommunityPreceptorRule.PeriodOfDay.values:
        raise ValidationError({"period_of_day": "Period must be AM or PM."})
    rule = next(
        (
            candidate
            for candidate in community_preceptor_rules_for(physician, on_date)
            if candidate.matches(on_date, period_of_day)
        ),
        None,
    )
    return CommunityPreceptorAvailabilityStatus(
        base_availability="available" if rule else "not_available",
        is_expected_commitment=rule is not None,
        source="manual" if rule else None,
        supporting_rule=rule,
    )


community_preceptor_status_on = community_preceptor_availability_on
community_preceptor_availability_for_session = community_preceptor_availability_on


def _normalize_periods(periods):
    if periods is None:
        return list(CommunityPreceptorRule.PeriodOfDay.values)
    if isinstance(periods, str):
        periods = [periods]
    try:
        values = set(periods)
    except TypeError, ValueError:
        raise ValidationError({"periods": "Select AM, PM, or both periods."}) from None
    ordered = [period for period in CommunityPreceptorRule.PeriodOfDay.values if period in values]
    if not ordered or values - set(CommunityPreceptorRule.PeriodOfDay.values):
        raise ValidationError({"periods": "Select AM, PM, or both periods."})
    return ordered


def _value(rule):
    return _value_values(
        rule.physician,
        rule.weekday,
        rule.ordinals,
        rule.periods,
        rule.effective_start,
        rule.effective_end,
    )


def _value_values(physician, weekday, ordinals, periods, effective_start, effective_end):
    return {
        "physician_id": physician.pk,
        "physician_display_name": physician.display_name,
        "weekday": weekday,
        "ordinals": sorted(ordinals),
        "periods": list(periods),
        "effective_start": effective_start.isoformat() if effective_start else None,
        "effective_end": effective_end.isoformat() if effective_end else None,
    }


def _record_change(*, rule, action, previous_value, recorded_value, scheduler, reason):
    return CommunityPreceptorScheduleChange.objects.create(
        community_preceptor_rule=rule,
        source_id_at_recording=rule.pk,
        physician=rule.physician,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
        reason=normalize_note(reason),
    )
