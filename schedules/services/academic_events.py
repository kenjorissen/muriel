"""Academic Event definitions, occurrences, lifecycle mutations, history, and attention."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.urls import reverse

from ..academic_years import academic_year_for_date, expected_quarter_start
from ..models.academic_events import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDateRange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    AcademicEventMonthlyRuleChange,
    AcademicEventQuarter,
)
from ..models.physicians import EffectiveRole
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from ._recurrence import normalize_ordinals
from .lead_times import lead_time_months
from .operational_attention import organization_today
from .work_remaining import (
    GlobalWorkBucket,
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    shift_date_months,
)

_UNSET = object()


@dataclass(frozen=True)
class AcademicEventWorkRemaining:
    """Actionable annual resolutions for one target academic year."""

    schedulable_definitions: tuple[AcademicEventDefinition, ...]
    underfilled_events: tuple[AcademicEvent, ...]


def academic_events_overlapping(start_date, end_date, *, participant_ids=None):
    """Materialize and return shared Academic Event facts in an inclusive date range."""
    _validate_date_range(start_date, end_date)
    initialize_monthly_academic_event_occurrences(start_date=start_date, end_date=end_date)
    events = (
        AcademicEvent.objects.overlapping(start_date, end_date)
        .filter(
            Q(monthly_rule__isnull=True, monthly_rule_source_id__isnull=True)
            | Q(definition__is_active=True)
        )
        .select_related("definition", "monthly_rule")
        .prefetch_related("ranges")
    )
    if participant_ids is None:
        events = events.prefetch_related("participants")
    else:
        events = events.filter(
            Q(participants__pk__in=participant_ids) | Q(all_available_faculty=True)
        ).annotate(_participant_id=F("participants__pk"))
    return tuple(
        sorted(
            events,
            key=lambda event: (event.start_date, event.title.casefold(), event.pk),
        )
    )


def academic_events_for_physician(physician, start_date=None, end_date=None):
    """Return Academic Events involving a physician, optionally within an inclusive range."""
    if (start_date is None) != (end_date is None):
        raise ValidationError("Both range dates are required when filtering Academic Events.")
    if start_date is not None:
        _validate_date_range(start_date, end_date)
        return tuple(
            event
            for event in academic_events_overlapping(start_date, end_date)
            if event.all_available_faculty
            or any(participant.pk == physician.pk for participant in event.participants.all())
        )
    events = AcademicEvent.objects.for_physician(physician)
    yearly = tuple(
        events.select_related("definition", "monthly_rule").prefetch_related(
            "participants", "ranges"
        )
    )
    return yearly


def academic_events_affecting_physician(physician, start_date=None, end_date=None):
    """Alias with the source-fact language used by availability callers."""
    return academic_events_for_physician(physician, start_date, end_date)


def academic_events_covering(physician, on_date, period_of_day):
    """Return blocking Academic Event sources for one physician's AM/PM period."""
    events = academic_events_for_physician(physician, on_date, on_date)
    return tuple(
        event
        for event in events
        if event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
        and academic_event_covers(event, physician, on_date, period_of_day)
    )


def academic_event_sources_for(physician, on_date, period_of_day):
    """Expose the event identity and title for a physician's blocking source."""
    return academic_events_covering(physician, on_date, period_of_day)


def blocking_academic_events(physician, on_date, period_of_day):
    return academic_events_covering(physician, on_date, period_of_day)


def academic_event_covers(event, physician, on_date, period_of_day, *, effective_roles=None):
    """Answer whether a shared event covers a particular participant and period."""
    if event.all_available_faculty:
        if effective_roles is not None:
            return any(
                role.role == EffectiveRole.Role.FACULTY
                and role.effective_start <= on_date
                and (role.effective_end is None or role.effective_end >= on_date)
                for role in effective_roles
            ) and event.covers(on_date, period_of_day)
        return event.covers_physician(physician, on_date, period_of_day)
    participants = getattr(event, "_prefetched_objects_cache", {}).get("participants")
    if participants is not None:
        return any(participant.pk == physician.pk for participant in participants) and event.covers(
            on_date, period_of_day
        )
    return event.covers_physician(physician, on_date, period_of_day)


@scheduling_mutation
@transaction.atomic
def academic_event_definitions_for(academic_year):
    """Return active definitions that are expected for one caller-selected academic year."""
    return AcademicEventDefinition.objects.effective_for(academic_year)


def schedulable_academic_event_definitions_for(academic_year):
    """Return effective definitions without a live annual resolution."""
    return (
        academic_event_definitions_for(academic_year)
        .filter(scheduling_mode=AcademicEventDefinition.SchedulingMode.YEARLY)
        .exclude(annual_events__academic_year=academic_year, annual_events__is_active=True)
    )


def academic_event_attention_start(academic_year, expected_quarter):
    """Return the fixed two-quarter lead date for a target academic-year quarter."""
    starts = {
        AcademicEventQuarter.Q1: date(academic_year, 1, 1),
        AcademicEventQuarter.Q2: date(academic_year, 4, 1),
        AcademicEventQuarter.Q3: date(academic_year, 7, 1),
        AcademicEventQuarter.Q4: date(academic_year, 10, 1),
    }
    try:
        return starts[expected_quarter]
    except KeyError:
        raise ValueError("Expected quarter must be Q1, Q2, Q3, or Q4.") from None


def academic_event_quarter_end(academic_year, expected_quarter):
    """Return the last date of an expected quarter in its target academic year."""
    ends = {
        AcademicEventQuarter.Q1: date(academic_year, 9, 30),
        AcademicEventQuarter.Q2: date(academic_year, 12, 31),
        AcademicEventQuarter.Q3: date(academic_year + 1, 3, 31),
        AcademicEventQuarter.Q4: date(academic_year + 1, 6, 30),
    }
    try:
        return ends[expected_quarter]
    except KeyError:
        raise ValueError("Expected quarter must be Q1, Q2, Q3, or Q4.") from None


def academic_event_definition_attention_active(definition, academic_year):
    """Whether an unresolved definition is actionable for its target year today."""
    today = organization_today()
    return (
        definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.YEARLY
        and definition.applies_to(academic_year)
        and academic_event_attention_start(academic_year, definition.expected_quarter)
        <= today
        <= academic_event_quarter_end(academic_year, definition.expected_quarter)
    )


def academic_event_attention_active(event):
    """Whether a dated occurrence's participant requirement is actionable today."""
    if event.resolution != AcademicEvent.Resolution.DATED:
        return False
    today = organization_today()
    if event.monthly_rule_source_id:
        return today <= event.end_date
    return (
        academic_event_attention_start(event.academic_year, event.expected_quarter)
        <= today
        <= event.end_date
    )


def academic_event_definition_work_key(definition_id, academic_year):
    """Return the stable work key for a definition's yearly resolution."""
    return f"academic-event-definition-{definition_id}-{academic_year}"


def academic_event_work_key(event):
    """Return the work key for one occurrence without conflating Monthly instances."""
    if event.monthly_rule_source_id:
        return f"academic-event-occurrence-{event.pk}"
    return academic_event_definition_work_key(event.definition_id, event.academic_year)


def academic_event_work_remaining(academic_year, events):
    """Project unresolved and underfilled work without reactivating historical facts."""
    active_events = tuple(event for event in events if event.is_active)
    events_by_definition = {event.definition_id: event for event in active_events}
    schedulable_definitions = tuple(
        definition
        for definition in academic_event_definitions_for(academic_year)
        if definition.pk not in events_by_definition
        and academic_event_definition_attention_active(definition, academic_year)
    )
    underfilled_events = tuple(
        event
        for event in active_events
        if academic_event_attention_active(event)
        and (
            event.monthly_rule_source_id
            or event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
        )
        and not event.all_available_faculty
        and len(event.participants.all()) < event.required_participant_count
    )
    return AcademicEventWorkRemaining(schedulable_definitions, underfilled_events)


def academic_event_projected_work_items(academic_year, events):
    """Project one unresolved item for each effective Academic Event definition."""
    events_by_definition = {event.definition_id: event for event in events if event.is_active}
    items = []
    for definition in academic_event_definitions_for(academic_year).filter(
        scheduling_mode=AcademicEventDefinition.SchedulingMode.YEARLY
    ):
        event = events_by_definition.get(definition.pk)
        if event is not None and event.resolution == AcademicEvent.Resolution.NOT_OCCURRING:
            continue

        expected_quarter = definition.expected_quarter if event is None else event.expected_quarter
        matters_on = shift_date_months(
            expected_quarter_start(academic_year, expected_quarter),
            -lead_time_months("academic_events"),
        )
        key = academic_event_definition_work_key(definition.pk, academic_year)
        if event is None:
            items.append(
                ProjectedWorkItem(
                    key=key,
                    area="academic-events",
                    label=f"{definition.base_name} is unscheduled",
                    affected_range=InclusiveDateRange(
                        expected_quarter_start(academic_year, expected_quarter),
                        academic_event_quarter_end(academic_year, expected_quarter),
                    ),
                    matters_on=matters_on,
                    target_url=(
                        f"{reverse('academic-event-create')}?academic_year={academic_year}"
                        f"&definition={definition.pk}"
                    ),
                    reasons=(ProjectedWorkReason("required", "Event needs scheduling", 1),),
                    global_bucket=_unscheduled_yearly_global_bucket(
                        academic_year, expected_quarter
                    ),
                )
            )
            continue

        participant_count = len(event.participants.all())
        if (
            event.precepting_impact == AcademicEvent.PreceptingImpact.NONE
            or event.all_available_faculty
        ):
            continue
        if participant_count < event.required_participant_count:
            missing_count = event.required_participant_count - participant_count
            items.append(
                ProjectedWorkItem(
                    key=key,
                    area="academic-events",
                    label=f"{event.title} needs participants",
                    affected_range=InclusiveDateRange(
                        event.date_ranges[0][0], event.date_ranges[-1][1]
                    ),
                    matters_on=matters_on,
                    target_url=reverse("academic-event-correct", args=(event.pk,)),
                    reasons=(
                        ProjectedWorkReason(
                            "required",
                            f"{missing_count} participant"
                            f"{'s' if missing_count != 1 else ''} needed",
                            missing_count,
                        ),
                    ),
                    global_bucket=_scheduled_and_monthly_global_bucket(
                        academic_year, event.date_ranges[0][0]
                    ),
                )
            )
    for event in events:
        if (
            not event.is_active
            or not event.monthly_rule_source_id
            or event.resolution != AcademicEvent.Resolution.DATED
            or event.all_available_faculty
        ):
            continue
        participant_count = len(event.participants.all())
        if participant_count >= event.required_participant_count:
            continue
        missing_count = event.required_participant_count - participant_count
        items.append(
            ProjectedWorkItem(
                key=academic_event_work_key(event),
                area="academic-events",
                label=f"{event.title} needs participants",
                affected_range=InclusiveDateRange(
                    event.date_ranges[0][0], event.date_ranges[-1][1]
                ),
                matters_on=shift_date_months(
                    event.start_date, -lead_time_months("academic_events")
                ),
                target_url=reverse("academic-event-monthly-assignment-correct", args=(event.pk,)),
                reasons=(
                    ProjectedWorkReason(
                        "required",
                        f"{missing_count} participant{'s' if missing_count != 1 else ''} needed",
                        missing_count,
                    ),
                ),
                global_bucket=_scheduled_and_monthly_global_bucket(
                    event.academic_year, event.date_ranges[0][0]
                ),
            )
        )
    return tuple(items)


def _unscheduled_yearly_global_bucket(academic_year, expected_quarter):
    return GlobalWorkBucket(
        key=f"academic-events-unscheduled-yearly-{academic_year}-{expected_quarter}",
        label="Unscheduled Yearly",
        affected_range=InclusiveDateRange(
            expected_quarter_start(academic_year, expected_quarter),
            academic_event_quarter_end(academic_year, expected_quarter),
        ),
        target_url=f"{reverse('academic-event-list')}?academic_year={academic_year}",
    )


def _scheduled_and_monthly_global_bucket(academic_year, first_scheduled_date):
    month_start = first_scheduled_date.replace(day=1)
    return GlobalWorkBucket(
        key=f"academic-events-scheduled-and-monthly-{academic_year}-{month_start:%Y-%m}",
        label="Scheduled and Monthly",
        affected_range=InclusiveDateRange(
            month_start,
            month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1]),
        ),
        target_url=f"{reverse('academic-event-list')}?academic_year={academic_year}",
    )


def actionable_academic_event_projected_work_items(academic_year, events):
    """Return projected items whose annual resolution needs attention today."""
    work_remaining = academic_event_work_remaining(academic_year, events)
    actionable_keys = {
        academic_event_definition_work_key(definition.pk, academic_year)
        for definition in work_remaining.schedulable_definitions
    } | {academic_event_work_key(event) for event in work_remaining.underfilled_events}
    return tuple(
        item
        for item in academic_event_projected_work_items(academic_year, events)
        if item.key in actionable_keys
    )


@scheduling_mutation
@transaction.atomic
def create_academic_event_definition(
    *,
    base_name,
    scheduling_mode=AcademicEventDefinition.SchedulingMode.YEARLY,
    start_academic_year,
    end_academic_year=None,
    expected_quarter=AcademicEventQuarter.Q1,
    default_duration_days=1,
    default_period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
    default_precepting_impact=AcademicEvent.PreceptingImpact.HARD,
    default_required_participant_count=1,
    all_available_faculty=False,
    default_participants=(),
    typically_scheduled="",
    scheduler,
):
    require_scheduler(scheduler)
    default_participants = _normalize_participants(default_participants)
    if all_available_faculty and default_participants:
        raise ValidationError(
            "All available Faculty definitions cannot name individual default physicians."
        )
    definition = AcademicEventDefinition(
        base_name=base_name.strip(),
        scheduling_mode=scheduling_mode,
        start_academic_year=start_academic_year,
        end_academic_year=end_academic_year,
        expected_quarter=None
        if scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
        else expected_quarter,
        default_duration_days=default_duration_days,
        default_period_of_day=default_period_of_day,
        default_precepting_impact=default_precepting_impact,
        default_required_participant_count=default_required_participant_count,
        all_available_faculty=all_available_faculty,
        typically_scheduled=_normalize_typically_scheduled(typically_scheduled),
    )
    definition.full_clean()
    definition.save()
    definition.default_participants.set(default_participants)
    _record_definition_change(
        definition=definition,
        action=AcademicEventDefinitionChange.Action.CREATED,
        previous_value=None,
        recorded_value=_definition_value(definition),
        scheduler=scheduler,
    )
    return definition


@scheduling_mutation
@transaction.atomic
def create_academic_event_definition_with_initial_monthly_rule(
    *, scheduler, initial_rule, **definition_values
):
    """Create a Monthly-rules definition and its required first rule together."""
    if (
        definition_values.get("scheduling_mode")
        != AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
    ):
        raise ValidationError("An initial monthly rule requires Monthly-rules scheduling.")
    definition = create_academic_event_definition(scheduler=scheduler, **definition_values)
    create_academic_event_monthly_rule(definition=definition, scheduler=scheduler, **initial_rule)
    return definition


@scheduling_mutation
@transaction.atomic
def correct_academic_event_definition(
    *,
    definition,
    base_name,
    scheduling_mode=None,
    start_academic_year,
    end_academic_year=None,
    expected_quarter=None,
    default_duration_days=None,
    default_period_of_day=None,
    default_precepting_impact=None,
    default_required_participant_count=None,
    all_available_faculty=None,
    default_participants=None,
    typically_scheduled=None,
    scheduler,
):
    require_scheduler(scheduler)
    definition = AcademicEventDefinition.objects.select_for_update().get(pk=definition.pk)
    current_default_participants = tuple(definition.default_participants.all())
    default_participants = (
        current_default_participants
        if default_participants is None
        else _normalize_participants(default_participants)
    )
    recorded_all_available_faculty = (
        definition.all_available_faculty if all_available_faculty is None else all_available_faculty
    )
    if recorded_all_available_faculty and default_participants:
        raise ValidationError(
            "All available Faculty definitions cannot name individual default physicians."
        )
    previous_value = _definition_value(definition)
    recorded_value = _definition_value(
        definition,
        base_name=base_name.strip(),
        scheduling_mode=definition.scheduling_mode if scheduling_mode is None else scheduling_mode,
        start_academic_year=start_academic_year,
        end_academic_year=end_academic_year,
        expected_quarter=(
            None
            if scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
            else definition.expected_quarter
            if expected_quarter is None
            else expected_quarter
        ),
        default_duration_days=(
            definition.default_duration_days
            if default_duration_days is None
            else default_duration_days
        ),
        default_period_of_day=(
            definition.default_period_of_day
            if default_period_of_day is None
            else default_period_of_day
        ),
        default_precepting_impact=(
            definition.default_precepting_impact
            if default_precepting_impact is None
            else default_precepting_impact
        ),
        default_required_participant_count=(
            definition.default_required_participant_count
            if default_required_participant_count is None
            else default_required_participant_count
        ),
        all_available_faculty=recorded_all_available_faculty,
        default_participants=default_participants,
        typically_scheduled=(
            definition.typically_scheduled
            if typically_scheduled is None
            else _normalize_typically_scheduled(typically_scheduled)
        ),
    )
    if previous_value == recorded_value:
        return definition

    definition.base_name = base_name.strip()
    if scheduling_mode is not None:
        definition.scheduling_mode = scheduling_mode
    definition.start_academic_year = start_academic_year
    definition.end_academic_year = end_academic_year
    if expected_quarter is not None:
        definition.expected_quarter = expected_quarter
    if definition.scheduling_mode == AcademicEventDefinition.SchedulingMode.MONTHLY_RULES:
        definition.expected_quarter = None
    if default_duration_days is not None:
        definition.default_duration_days = default_duration_days
    if default_period_of_day is not None:
        definition.default_period_of_day = default_period_of_day
    if default_precepting_impact is not None:
        definition.default_precepting_impact = default_precepting_impact
    if default_required_participant_count is not None:
        definition.default_required_participant_count = default_required_participant_count
    if all_available_faculty is not None:
        definition.all_available_faculty = all_available_faculty
    if typically_scheduled is not None:
        definition.typically_scheduled = _normalize_typically_scheduled(typically_scheduled)
    definition.full_clean()
    definition.save(
        update_fields=(
            "base_name",
            "scheduling_mode",
            "start_academic_year",
            "end_academic_year",
            "expected_quarter",
            "default_duration_days",
            "default_period_of_day",
            "default_precepting_impact",
            "default_required_participant_count",
            "all_available_faculty",
            "typically_scheduled",
        )
    )
    definition.default_participants.set(default_participants)
    _record_definition_change(
        definition=definition,
        action=AcademicEventDefinitionChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_definition_value(definition),
        scheduler=scheduler,
    )
    _reconcile_monthly_definition_assignments(definition=definition, scheduler=scheduler)
    return definition


@scheduling_mutation
@transaction.atomic
def deactivate_academic_event_definition(*, definition, scheduler):
    """Deactivate through the sole catalog mutation boundary."""
    require_scheduler(scheduler)
    definition = AcademicEventDefinition.objects.select_for_update().get(pk=definition.pk)
    if not definition.is_active:
        return definition
    previous_value = _definition_value(definition)
    definition.is_active = False
    definition.full_clean()
    definition.save(update_fields=("is_active",))
    _record_definition_change(
        definition=definition,
        action=AcademicEventDefinitionChange.Action.DEACTIVATED,
        previous_value=previous_value,
        recorded_value=_definition_value(definition),
        scheduler=scheduler,
    )
    return definition


@scheduling_mutation
@transaction.atomic
def create_academic_event_occurrence(
    *,
    definition,
    academic_year,
    start_date,
    end_date,
    date_ranges=None,
    participants=(),
    all_available_faculty=False,
    suffix="",
    note="",
    required_participant_count=None,
    period_of_day=None,
    precepting_impact=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    definition = AcademicEventDefinition.objects.select_for_update().get(pk=definition.pk)
    if definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.YEARLY:
        raise ValidationError("Monthly-rules definitions do not have annual occurrences.")
    if not definition.applies_to(academic_year):
        raise ValidationError(
            "This Event definition is not effective for the selected academic year."
        )
    date_ranges = _normalize_date_ranges(date_ranges or ((start_date, end_date),), academic_year)
    existing = AcademicEvent.objects.filter(
        definition=definition, academic_year=academic_year, is_active=True
    ).first()
    if existing:
        return existing
    for start, end in date_ranges:
        capture_calendar_range(start, end)
    participants = _normalize_participants(participants)
    event = AcademicEvent(
        definition=definition,
        academic_year=academic_year,
        base_name=definition.base_name,
        expected_quarter=definition.expected_quarter,
        suffix=_normalize_suffix(suffix),
        note=normalize_note(note),
        required_participant_count=(
            definition.default_required_participant_count
            if required_participant_count is None
            else required_participant_count
        ),
        resolution=AcademicEvent.Resolution.DATED,
        start_date=date_ranges[0][0],
        end_date=date_ranges[-1][1],
        period_of_day=definition.default_period_of_day if period_of_day is None else period_of_day,
        precepting_impact=(
            definition.default_precepting_impact if precepting_impact is None else precepting_impact
        ),
        all_available_faculty=all_available_faculty,
    )
    if all_available_faculty and participants:
        raise ValidationError("All available Faculty events cannot name individual participants.")
    event.full_clean()
    event.save()
    AcademicEventDateRange.objects.bulk_create(
        [
            AcademicEventDateRange(event=event, start_date=start, end_date=end)
            for start, end in date_ranges
        ]
    )
    event.participants.set(() if all_available_faculty else participants)
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.CREATED,
        previous_value=None,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


@scheduling_mutation
@transaction.atomic
def record_academic_event_not_occurring(*, definition, academic_year, scheduler, reason=""):
    require_scheduler(scheduler)
    definition = AcademicEventDefinition.objects.select_for_update().get(pk=definition.pk)
    if not definition.applies_to(academic_year):
        raise ValidationError(
            "This Event definition is not effective for the selected academic year."
        )
    if definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.YEARLY:
        raise ValidationError("Monthly-rules definitions always have dated rule-owned occurrences.")
    existing = AcademicEvent.objects.filter(
        definition=definition, academic_year=academic_year, is_active=True
    ).first()
    if existing:
        return existing
    event = AcademicEvent(
        definition=definition,
        academic_year=academic_year,
        base_name=definition.base_name,
        expected_quarter=definition.expected_quarter,
        required_participant_count=definition.default_required_participant_count,
        precepting_impact=definition.default_precepting_impact,
        resolution=AcademicEvent.Resolution.NOT_OCCURRING,
    )
    event.full_clean()
    event.save()
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.NOT_OCCURRING,
        previous_value=None,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


@scheduling_mutation
@transaction.atomic
def correct_academic_event_occurrence(
    *,
    event,
    start_date,
    end_date,
    date_ranges=None,
    participants=(),
    all_available_faculty=False,
    suffix="",
    note=None,
    required_participant_count=None,
    period_of_day=None,
    precepting_impact=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    event = AcademicEvent.objects.select_for_update().get(pk=event.pk)
    for start, end in event.date_ranges:
        capture_calendar_range(start, end)
    if not event.is_active or event.resolution != AcademicEvent.Resolution.DATED:
        raise ValidationError("Only a live dated occurrence can be edited.")
    for start, end in date_ranges or ((start_date, end_date),):
        capture_calendar_range(start, end)
    if event.monthly_rule_source_id:
        return _correct_monthly_academic_event_assignment(
            event=event,
            start_date=start_date,
            end_date=end_date,
            date_ranges=date_ranges,
            participants=participants,
            all_available_faculty=all_available_faculty,
            suffix=suffix,
            note=note,
            required_participant_count=required_participant_count,
            period_of_day=period_of_day,
            precepting_impact=precepting_impact,
            scheduler=scheduler,
            reason=reason,
        )
    participants = _normalize_participants(participants)
    date_ranges = _normalize_date_ranges(
        date_ranges or ((start_date, end_date),), event.academic_year
    )
    if all_available_faculty and participants:
        raise ValidationError("All available Faculty events cannot name individual participants.")
    required_participant_count = (
        event.required_participant_count
        if required_participant_count is None
        else required_participant_count
    )
    previous_value = _event_value(event)
    recorded_value = _event_value(
        event,
        participants=participants,
        suffix=_normalize_suffix(suffix),
        note=event.note if note is None else normalize_note(note),
        start_date=start_date,
        end_date=end_date,
        required_participant_count=required_participant_count,
        period_of_day=period_of_day,
        precepting_impact=precepting_impact,
        date_ranges=date_ranges,
        all_available_faculty=all_available_faculty,
    )
    if previous_value == recorded_value:
        return event
    event.suffix = _normalize_suffix(suffix)
    if note is not None:
        event.note = normalize_note(note)
    event.start_date = date_ranges[0][0]
    event.end_date = date_ranges[-1][1]
    event.required_participant_count = required_participant_count
    event.period_of_day = event.period_of_day if period_of_day is None else period_of_day
    event.precepting_impact = (
        event.precepting_impact if precepting_impact is None else precepting_impact
    )
    event.all_available_faculty = all_available_faculty
    event.full_clean()
    event.save(
        update_fields=(
            "suffix",
            "note",
            "start_date",
            "end_date",
            "required_participant_count",
            "period_of_day",
            "precepting_impact",
            "all_available_faculty",
        )
    )
    event.ranges.all().delete()
    AcademicEventDateRange.objects.bulk_create(
        [
            AcademicEventDateRange(event=event, start_date=start, end_date=end)
            for start, end in date_ranges
        ]
    )
    event.participants.set(() if all_available_faculty else participants)
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


@scheduling_mutation
@transaction.atomic
def remove_academic_event_occurrence(*, event, scheduler, reason=""):
    require_scheduler(scheduler)
    event = AcademicEvent.objects.select_for_update().get(pk=event.pk)
    for start, end in event.date_ranges:
        capture_calendar_range(start, end)
    if not event.is_active or event.resolution != AcademicEvent.Resolution.DATED:
        return event
    if event.monthly_rule_source_id:
        raise ValidationError("Monthly occurrences are retired by their recurrence rule.")
    previous_value = _event_value(event)
    event.is_active = False
    event.save(update_fields=("is_active",))
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.REMOVED,
        previous_value=previous_value,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


@scheduling_mutation
@transaction.atomic
def reverse_academic_event_not_occurring(*, event, scheduler, reason=""):
    require_scheduler(scheduler)
    event = AcademicEvent.objects.select_for_update().get(pk=event.pk)
    for start, end in event.date_ranges:
        capture_calendar_range(start, end)
    if not event.is_active or event.resolution != AcademicEvent.Resolution.NOT_OCCURRING:
        return event
    previous_value = _event_value(event)
    event.is_active = False
    event.save(update_fields=("is_active",))
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.REVERSED,
        previous_value=previous_value,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


def _validate_date_range(start_date, end_date):
    if not start_date or not end_date:
        raise ValidationError("Start and end dates are required.")
    if end_date < start_date:
        raise ValidationError({"end_date": "End date cannot be before start date."})


def _normalize_date_ranges(date_ranges, academic_year):
    normalized = []
    for start_date, end_date in date_ranges:
        _validate_date_range(start_date, end_date)
        if (
            academic_year_for_date(start_date) != academic_year
            or academic_year_for_date(end_date) != academic_year
        ):
            raise ValidationError(
                "Occurrence dates must both fall within the selected academic year."
            )
        normalized.append((start_date, end_date))
    if not normalized:
        raise ValidationError("At least one date range is required.")
    normalized.sort()
    for (_, prior_end), (start_date, _) in pairwise(normalized):
        if start_date <= prior_end:
            raise ValidationError("Academic Event date ranges cannot overlap.")
    return tuple(normalized)


def _normalize_participants(participants):
    unique = {}
    for participant in participants or ():
        if not getattr(participant, "pk", None):
            raise ValidationError({"participants": "Choose recorded participating physicians."})
        unique[participant.pk] = participant
    return tuple(unique[physician_id] for physician_id in sorted(unique))


def _normalize_suffix(suffix):
    return suffix.strip() if suffix else ""


def _normalize_typically_scheduled(typically_scheduled):
    return typically_scheduled.strip() if typically_scheduled else ""


def _event_value(
    event,
    *,
    participants=None,
    suffix=None,
    note=None,
    start_date=None,
    end_date=None,
    required_participant_count=None,
    period_of_day=None,
    precepting_impact=None,
    date_ranges=None,
    all_available_faculty=None,
):
    if participants is None:
        participants = tuple(event.participants.all())
    return {
        "definition_id": event.definition_id,
        "monthly_rule_id": event.monthly_rule_id,
        "monthly_rule_source_id": event.monthly_rule_source_id,
        "academic_year": event.academic_year,
        "base_name": event.base_name,
        "expected_quarter": event.expected_quarter,
        "suffix": event.suffix if suffix is None else suffix,
        "note": event.note if note is None else note,
        "title": event.base_name
        if (event.suffix if suffix is None else suffix) == ""
        else f"{event.base_name} - {event.suffix if suffix is None else suffix}",
        "start_date": (
            (event.start_date if start_date is None else start_date).isoformat()
            if (event.start_date if start_date is None else start_date)
            else None
        ),
        "end_date": event.end_date.isoformat()
        if end_date is None and event.end_date
        else end_date.isoformat()
        if end_date
        else None,
        "period_of_day": event.period_of_day if period_of_day is None else period_of_day,
        "precepting_impact": (
            event.precepting_impact if precepting_impact is None else precepting_impact
        ),
        "date_ranges": [
            {"start_date": start.isoformat(), "end_date": end.isoformat()}
            for start, end in (event.date_ranges if date_ranges is None else date_ranges)
        ],
        "all_available_faculty": (
            event.all_available_faculty if all_available_faculty is None else all_available_faculty
        ),
        "assignment_is_default": event.assignment_is_default,
        "required_participant_count": (
            event.required_participant_count
            if required_participant_count is None
            else required_participant_count
        ),
        "resolution": event.resolution,
        "is_active": event.is_active,
        "participants": [
            {"id": physician.pk, "display_name": physician.display_name}
            for physician in sorted(participants, key=lambda physician: physician.pk)
        ],
    }


def _definition_value(
    definition,
    *,
    base_name=None,
    scheduling_mode=None,
    start_academic_year=None,
    end_academic_year=_UNSET,
    expected_quarter=None,
    default_duration_days=None,
    default_period_of_day=None,
    default_precepting_impact=None,
    default_required_participant_count=None,
    all_available_faculty=None,
    default_participants=None,
    typically_scheduled=None,
):
    return {
        "base_name": base_name if base_name is not None else definition.base_name,
        "scheduling_mode": definition.scheduling_mode
        if scheduling_mode is None
        else scheduling_mode,
        "start_academic_year": (
            start_academic_year
            if start_academic_year is not None
            else definition.start_academic_year
        ),
        "end_academic_year": (
            end_academic_year if end_academic_year is not _UNSET else definition.end_academic_year
        ),
        "expected_quarter": definition.expected_quarter
        if expected_quarter is None
        else expected_quarter,
        "default_duration_days": (
            definition.default_duration_days
            if default_duration_days is None
            else default_duration_days
        ),
        "default_period_of_day": (
            definition.default_period_of_day
            if default_period_of_day is None
            else default_period_of_day
        ),
        "default_precepting_impact": (
            definition.default_precepting_impact
            if default_precepting_impact is None
            else default_precepting_impact
        ),
        "default_required_participant_count": (
            definition.default_required_participant_count
            if default_required_participant_count is None
            else default_required_participant_count
        ),
        "all_available_faculty": (
            definition.all_available_faculty
            if all_available_faculty is None
            else all_available_faculty
        ),
        "default_participant_ids": sorted(
            participant.pk
            for participant in (
                definition.default_participants.all()
                if default_participants is None
                else default_participants
            )
        ),
        "typically_scheduled": (
            definition.typically_scheduled if typically_scheduled is None else typically_scheduled
        ),
        "is_active": definition.is_active,
    }


def _record_event_change(*, event, action, previous_value, recorded_value, scheduler, reason):
    return AcademicEventChange.objects.create(
        event=event,
        event_id_at_recording=event.pk,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
        reason=normalize_note(reason),
    )


def _record_definition_change(*, definition, action, previous_value, recorded_value, scheduler):
    return AcademicEventDefinitionChange.objects.create(
        definition=definition,
        definition_id_at_recording=definition.pk,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
    )


def initialize_monthly_academic_event_occurrences(
    *,
    start_date,
    end_date,
    scheduler=None,
    reason="Materialized from the monthly recurrence rule.",
):
    """Materialize matching Monthly-rules occurrences in one inclusive operational range."""
    _validate_date_range(start_date, end_date)
    rules = (
        AcademicEventMonthlyRule.objects.select_related("definition")
        .filter(
            definition__is_active=True,
            definition__scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
        )
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=end_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=start_date))
    )
    occurrences = []
    for rule in rules:
        occurrences.extend(
            _materialize_monthly_rule_occurrences(
                rule=rule,
                start_date=start_date,
                end_date=end_date,
                scheduler=scheduler,
                reason=reason,
            )
        )
    return tuple(sorted(occurrences, key=lambda event: (event.start_date, event.pk)))


def _materialize_monthly_rule_occurrences(*, rule, start_date, end_date, scheduler, reason):
    matching_dates = []
    on_date = start_date
    while on_date <= end_date:
        if rule.definition.applies_to(academic_year_for_date(on_date)) and rule.matches(on_date):
            matching_dates.append(on_date)
        on_date += timedelta(days=1)
    existing_dates = set(
        AcademicEvent.objects.filter(
            monthly_rule=rule, start_date__in=matching_dates, is_active=True
        ).values_list("start_date", flat=True)
    )
    created_dates = set()
    missing_dates = set(matching_dates) - existing_dates
    while missing_dates:
        try:
            # Keep an outer rule correction transaction usable after a competing read wins.
            with transaction.atomic():
                AcademicEvent.objects.bulk_create(
                    [
                        AcademicEvent(
                            definition=rule.definition,
                            monthly_rule=rule,
                            monthly_rule_source_id=rule.pk,
                            academic_year=academic_year_for_date(on_date),
                            base_name=rule.definition.base_name,
                            expected_quarter=None,
                            required_participant_count=rule.definition.default_required_participant_count,
                            resolution=AcademicEvent.Resolution.DATED,
                            start_date=on_date,
                            end_date=on_date,
                            period_of_day=rule.definition.default_period_of_day,
                            precepting_impact=rule.definition.default_precepting_impact,
                            all_available_faculty=rule.definition.all_available_faculty,
                            assignment_is_default=True,
                        )
                        for on_date in missing_dates
                    ]
                )
        except IntegrityError:
            refreshed_dates = set(
                AcademicEvent.objects.filter(
                    monthly_rule=rule, start_date__in=matching_dates, is_active=True
                ).values_list("start_date", flat=True)
            )
            refreshed_missing_dates = set(matching_dates) - refreshed_dates
            if refreshed_missing_dates == missing_dates:
                raise
            missing_dates = refreshed_missing_dates
        else:
            created_dates = missing_dates
            break
    default_participants = (
        ()
        if rule.definition.all_available_faculty
        else tuple(rule.definition.default_participants.all())
    )
    created = list(
        AcademicEvent.objects.filter(
            monthly_rule=rule, start_date__in=created_dates, is_active=True
        )
    )
    if default_participants:
        for event in created:
            event.participants.set(default_participants)
    occurrence_query = AcademicEvent.objects.filter(
        monthly_rule=rule, start_date__in=matching_dates, is_active=True
    )
    if created_dates:
        occurrence_query = occurrence_query.prefetch_related("participants")
    occurrences = list(occurrence_query)
    created = [event for event in occurrences if event.start_date in created_dates]
    AcademicEventChange.objects.bulk_create(
        [
            AcademicEventChange(
                event=event,
                event_id_at_recording=event.pk,
                action=AcademicEventChange.Action.CREATED,
                previous_value=None,
                recorded_value=_monthly_occurrence_value(event),
                changed_by=scheduler,
                reason=reason,
            )
            for event in created
        ]
    )
    return occurrences


def _monthly_occurrence_value(event):
    participants = getattr(event, "_prefetched_objects_cache", {}).get("participants")
    return {
        "definition_id": event.definition_id,
        "monthly_rule_id": event.monthly_rule_id,
        "monthly_rule_source_id": event.monthly_rule_source_id,
        "academic_year": event.academic_year,
        "base_name": event.base_name,
        "expected_quarter": None,
        "suffix": "",
        "note": "",
        "title": event.title,
        "start_date": event.start_date.isoformat(),
        "end_date": event.end_date.isoformat(),
        "period_of_day": event.period_of_day,
        "precepting_impact": event.precepting_impact,
        "date_ranges": [
            {"start_date": event.start_date.isoformat(), "end_date": event.end_date.isoformat()}
        ],
        "all_available_faculty": event.all_available_faculty,
        "assignment_is_default": event.assignment_is_default,
        "required_participant_count": event.required_participant_count,
        "resolution": event.resolution,
        "is_active": event.is_active,
        "participant_ids": (
            sorted(participant.pk for participant in participants)
            if participants is not None
            else list(event.participants.order_by("pk").values_list("pk", flat=True))
        ),
    }


def _correct_monthly_academic_event_assignment(
    *,
    event,
    start_date,
    end_date,
    date_ranges,
    participants,
    all_available_faculty,
    suffix,
    note,
    required_participant_count,
    period_of_day,
    precepting_impact,
    scheduler,
    reason,
):
    participants = _normalize_participants(participants)
    date_ranges = _normalize_date_ranges(
        date_ranges or ((start_date, end_date),), event.academic_year
    )
    if all_available_faculty and participants:
        raise ValidationError("All available Faculty events cannot name individual participants.")
    if (
        date_ranges != event.date_ranges
        or _normalize_suffix(suffix) != event.suffix
        or (note is not None and normalize_note(note) != event.note)
        or (
            required_participant_count is not None
            and required_participant_count != event.required_participant_count
        )
        or (period_of_day is not None and period_of_day != event.period_of_day)
        or (precepting_impact is not None and precepting_impact != event.precepting_impact)
    ):
        raise ValidationError(
            "Monthly occurrence dates and scheduling details are owned by its rule."
        )
    previous_value = _event_value(event)
    current_participant_ids = set(event.participants.values_list("pk", flat=True))
    participant_ids = {participant.pk for participant in participants}
    if (
        current_participant_ids == participant_ids
        and event.all_available_faculty == all_available_faculty
        and not event.assignment_is_default
    ):
        return event
    event.all_available_faculty = all_available_faculty
    event.assignment_is_default = False
    event.full_clean()
    event.save(update_fields=("all_available_faculty", "assignment_is_default"))
    event.participants.set(() if all_available_faculty else participants)
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )
    return event


def _reconcile_monthly_definition_assignments(*, definition, scheduler):
    if definition.scheduling_mode != AcademicEventDefinition.SchedulingMode.MONTHLY_RULES:
        return
    default_participants = tuple(definition.default_participants.all())
    default_participant_ids = {participant.pk for participant in default_participants}
    for event in (
        AcademicEvent.objects.select_for_update()
        .filter(
            definition=definition,
            monthly_rule_source_id__isnull=False,
            is_active=True,
            start_date__gte=organization_today(),
            assignment_is_default=True,
        )
        .prefetch_related("participants")
    ):
        current_participant_ids = {participant.pk for participant in event.participants.all()}
        if (
            event.all_available_faculty == definition.all_available_faculty
            and current_participant_ids == default_participant_ids
        ):
            continue
        previous_value = _event_value(event)
        event.all_available_faculty = definition.all_available_faculty
        event.save(update_fields=("all_available_faculty",))
        event.participants.set(() if definition.all_available_faculty else default_participants)
        _record_event_change(
            event=event,
            action=AcademicEventChange.Action.CORRECTED,
            previous_value=previous_value,
            recorded_value=_event_value(event),
            scheduler=scheduler,
            reason="Updated from the definition's assignment defaults.",
        )


def _reconcile_monthly_rule_occurrences(*, rule, scheduler):
    """Reconcile the definition's materialized future horizon without rewriting the past."""
    today = organization_today()
    horizon = (
        AcademicEvent.objects.filter(
            definition=rule.definition,
            monthly_rule_source_id__isnull=False,
            start_date__gte=today,
        )
        .order_by("-start_date")
        .values_list("start_date", flat=True)
        .first()
    )
    if horizon is None:
        return
    horizon = date(academic_year_for_date(horizon) + 1, 6, 30)
    _materialize_monthly_rule_occurrences(
        rule=rule,
        start_date=today,
        end_date=horizon,
        scheduler=scheduler,
        reason="Materialized after monthly recurrence rule correction.",
    )
    for event in AcademicEvent.objects.select_for_update().filter(
        monthly_rule=rule, is_active=True, start_date__gte=today
    ):
        if not (rule.definition.applies_to(event.academic_year) and rule.matches(event.start_date)):
            _retire_monthly_occurrence(
                event=event,
                scheduler=scheduler,
                reason="Retired because the monthly recurrence rule no longer matches its date.",
            )


def _retire_monthly_occurrence(*, event, scheduler, reason):
    previous_value = _event_value(event)
    event.is_active = False
    event.save(update_fields=("is_active",))
    _record_event_change(
        event=event,
        action=AcademicEventChange.Action.REMOVED,
        previous_value=previous_value,
        recorded_value=_event_value(event),
        scheduler=scheduler,
        reason=reason,
    )


@scheduling_mutation
@transaction.atomic
def create_academic_event_monthly_rule(
    *, definition, weekday, ordinals, effective_start=None, effective_end=None, scheduler, reason=""
):
    require_scheduler(scheduler)
    rule = AcademicEventMonthlyRule(
        definition=definition,
        weekday=weekday,
        ordinals=normalize_ordinals(ordinals),
        effective_start=effective_start,
        effective_end=effective_end,
    )
    rule.full_clean()
    rule.save()
    _record_monthly_rule_change(
        rule,
        AcademicEventMonthlyRuleChange.Action.CREATED,
        None,
        _monthly_rule_value(rule),
        scheduler,
        reason,
    )
    _reconcile_monthly_rule_occurrences(rule=rule, scheduler=scheduler)
    return rule


@scheduling_mutation
@transaction.atomic
def correct_academic_event_monthly_rule(
    *, rule, weekday, ordinals, effective_start=None, effective_end=None, scheduler, reason=""
):
    require_scheduler(scheduler)
    rule = (
        AcademicEventMonthlyRule.objects.select_for_update()
        .select_related("definition")
        .get(pk=rule.pk)
    )
    previous_value = _monthly_rule_value(rule)
    ordinals = normalize_ordinals(ordinals)
    recorded_value = _monthly_rule_value_values(
        rule.definition, weekday, ordinals, effective_start, effective_end
    )
    if previous_value == recorded_value:
        return rule
    rule.weekday, rule.ordinals = weekday, ordinals
    rule.effective_start, rule.effective_end = effective_start, effective_end
    rule.full_clean()
    rule.save(update_fields=("weekday", "ordinals", "effective_start", "effective_end"))
    _record_monthly_rule_change(
        rule,
        AcademicEventMonthlyRuleChange.Action.CORRECTED,
        previous_value,
        _monthly_rule_value(rule),
        scheduler,
        reason,
    )
    _reconcile_monthly_rule_occurrences(rule=rule, scheduler=scheduler)
    return rule


@scheduling_mutation
@transaction.atomic
def remove_academic_event_monthly_rule(*, rule, scheduler, reason=""):
    require_scheduler(scheduler)
    rule = (
        AcademicEventMonthlyRule.objects.select_for_update()
        .select_related("definition")
        .get(pk=rule.pk)
    )
    today = organization_today()
    for event in AcademicEvent.objects.select_for_update().filter(
        monthly_rule=rule, is_active=True, start_date__gte=today
    ):
        _retire_monthly_occurrence(
            event=event,
            scheduler=scheduler,
            reason="Retired because its monthly recurrence rule was removed.",
        )
    _record_monthly_rule_change(
        rule,
        AcademicEventMonthlyRuleChange.Action.REMOVED,
        _monthly_rule_value(rule),
        None,
        scheduler,
        reason,
    )
    rule.delete()


def _monthly_rule_value(rule):
    return _monthly_rule_value_values(
        rule.definition, rule.weekday, rule.ordinals, rule.effective_start, rule.effective_end
    )


def _monthly_rule_value_values(definition, weekday, ordinals, effective_start, effective_end):
    return {
        "definition_id": definition.pk,
        "definition_name": definition.base_name,
        "weekday": weekday,
        "ordinals": sorted(ordinals),
        "effective_start": effective_start.isoformat() if effective_start else None,
        "effective_end": effective_end.isoformat() if effective_end else None,
    }


def _record_monthly_rule_change(rule, action, previous_value, recorded_value, scheduler, reason):
    return AcademicEventMonthlyRuleChange.objects.create(
        rule=rule,
        source_id_at_recording=rule.pk,
        definition=rule.definition,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
        reason=normalize_note(reason),
    )
